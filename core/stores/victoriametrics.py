"""The `victoriametrics` store — the default metric store.

Writes derived gauge metrics via the VictoriaMetrics **JSON-line import** endpoint
(`/api/v1/import`) and reads them back via the Prometheus-compatible PromQL range
endpoint (`/api/v1/query_range`), parsing the matrix response into `MetricSeries`.

Two deliberate design points:

- **Only `MetricSignal`s are persisted.** Logs/incidents/traces are queried live
  from their source in v0.1 (spec `## Data Model`); the store only ever receives
  derived gauge metrics, so `write` filters the batch to `MetricSignal` and silently
  ignores other kinds rather than trying to encode them.
- **Failures surface the upstream response body.** A 4xx/5xx from VictoriaMetrics
  carries *which field/value it rejected* in the response text; `str(HTTPStatusError)`
  only has URL + status code. The shared `core.rest.RestClient` appends the trimmed
  body so a write rejection is diagnosable in one cycle (spec `## Performance
  Constraints` — the REST-client convention). It also copes with a connection error,
  where there is no response to read — it must not crash on the missing `.response`.

The transport plumbing (the injectable `httpx.Client` seam, `raise_for_status`, and
the both-branch failure surfacing) lives once in `core.rest`; this store keeps only
its own URL building, the VM import-line serialization, and the matrix parse. httpx
mocking in tests is done with `respx`, which patches the transport globally, so the
`RestClient`'s default `httpx.Client()` is intercepted without an injected client.
The client is still threaded through as a constructor seam for explicit injection.
"""

import json
from datetime import UTC, datetime

import httpx

from core.errors import PanoptesError
from core.model import CanonicalSignal, MetricQuery, MetricSeries, MetricSignal
from core.registry import STORES, ConfigBlock
from core.rest import RestClient
from core.validation import require_str_field

# VictoriaMetrics endpoints (Risk R9: JSON-line import for writes, PromQL range for
# reads — chosen because the import line shape is byte-exact-assertable in tests).
_IMPORT_PATH = "/api/v1/import"
_QUERY_RANGE_PATH = "/api/v1/query_range"


@STORES.register("victoriametrics")
class VictoriaMetricsStore:
    """Persists derived metrics to VictoriaMetrics and answers PromQL range queries."""

    type = "victoriametrics"

    def __init__(self, config: ConfigBlock, client: httpx.Client | None = None) -> None:
        """Read the base `url` from config; accept an optional injected httpx client.

        The `client` seam keeps the store unit-testable without monkeypatching; under
        `respx` a default `httpx.Client()` is intercepted globally, so production code
        passes no client and tests need not inject one — the seam exists for explicit
        control where wanted. The client is threaded into the shared `RestClient`, which
        owns the transport + failure surfacing.
        """
        # Plane-neutral field check (shared with the sources via `core.validation`) —
        # replaces what was an inline reimplementation here, which existed only to
        # avoid a store→`core.sources` cross-plane import.
        url = require_str_field(config, "url", self.type)
        # Normalize away a trailing slash so endpoint joins produce a single slash.
        self._base_url = url.rstrip("/")
        self._rest = RestClient(client)

    def write(self, signals: list[CanonicalSignal]) -> None:
        """Serialize metric signals to VM import lines and POST them.

        Only `MetricSignal`s are persisted (v0.1 stores derived gauges only); other
        kinds in the batch are ignored. An empty resulting body is skipped — there is
        nothing to import and no reason to round-trip to the server.
        """
        lines = [
            self._to_import_line(signal) for signal in signals if isinstance(signal, MetricSignal)
        ]
        if not lines:
            return
        body = "\n".join(lines)
        endpoint = f"{self._base_url}{_IMPORT_PATH}"
        # Delegate the POST + raise_for_status + both-branch failure surfacing to the
        # shared RestClient; only the URL + the import-line body are VM-specific here.
        self._rest.send(
            lambda http: http.post(endpoint, content=body.encode("utf-8")),
            prefix="VM import failed",
            identifier=endpoint,
        )

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        """Run a PromQL range query and parse the matrix result into `MetricSeries`.

        A 200 response with an empty `result` returns an empty list (not an error):
        "no data for this window" is a legitimate answer, distinct from a transport
        or query failure (which raise).
        """
        endpoint = f"{self._base_url}{_QUERY_RANGE_PATH}"
        params = {
            "query": query.expr,
            "start": str(int(query.window.start.timestamp())),
            "end": str(int(query.window.end.timestamp())),
            "step": str(query.step_seconds),
        }
        # The shared RestClient GETs + raises + parses JSON; the matrix shape is VM's.
        payload = self._rest.get_json(
            endpoint, prefix="VM query failed", identifier=endpoint, params=params
        )
        return self._parse_matrix(payload)

    @staticmethod
    def _to_import_line(signal: MetricSignal) -> str:
        """Render one `MetricSignal` as a VictoriaMetrics JSON import line.

        Shape: `{"metric": {"__name__": <name>, <labels...>}, "values": [<value>],
        "timestamps": [<epoch_millis>]}`. Every label (incl. the mandatory `env`) is
        carried into the `metric` object faithfully; the timestamp is milliseconds.
        """
        metric: dict[str, str] = {"__name__": signal.name}
        # Labels carried verbatim; `env` is always present (model invariant).
        metric.update(signal.labels)
        epoch_millis = int(signal.timestamp.timestamp() * 1000)
        line: dict[str, object] = {
            "metric": metric,
            "values": [signal.value],
            "timestamps": [epoch_millis],
        }
        # Compact separators keep the asserted line bytes deterministic.
        return json.dumps(line, separators=(",", ":"))

    @staticmethod
    def _parse_matrix(payload: object) -> list[MetricSeries]:
        """Parse a Prometheus matrix response into `MetricSeries`.

        Expected shape: `{"data": {"result": [{"metric": {"__name__": ...,
        <labels>}, "values": [[<ts_seconds>, "<val>"], ...]}, ...]}}`. The metric's
        `__name__` becomes `MetricSeries.metric`; the remaining metric entries are the
        labels; each `[ts, val]` pair becomes a `(datetime, float)` point.
        """
        if not isinstance(payload, dict):
            raise PanoptesError(f"VM query returned a non-object response: {payload!r}.")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PanoptesError(f"VM query response missing a 'data' object: {payload!r}.")
        result = data.get("result")
        if not isinstance(result, list):
            raise PanoptesError(f"VM query 'data.result' is not a list: {result!r}.")

        series: list[MetricSeries] = []
        for entry in result:
            if not isinstance(entry, dict):
                raise PanoptesError(f"VM query result entry is not an object: {entry!r}.")
            raw_metric = entry.get("metric")
            if not isinstance(raw_metric, dict):
                raise PanoptesError(f"VM query result entry missing 'metric': {entry!r}.")
            # Split __name__ off from the labels; values are strings in the response.
            name = ""
            labels: dict[str, str] = {}
            for key, value in raw_metric.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise PanoptesError(f"VM query metric has non-string label: {raw_metric!r}.")
                if key == "__name__":
                    name = value
                else:
                    labels[key] = value
            points = VictoriaMetricsStore._parse_points(entry.get("values"))
            series.append(MetricSeries(metric=name, labels=labels, points=points))
        return series

    @staticmethod
    def _parse_points(raw_values: object) -> list[tuple[datetime, float]]:
        """Parse the `values` array of `[epoch_seconds, "value"]` pairs into points."""
        if not isinstance(raw_values, list):
            raise PanoptesError(f"VM query 'values' is not a list: {raw_values!r}.")
        points: list[tuple[datetime, float]] = []
        for pair in raw_values:
            if not isinstance(pair, list) or len(pair) != 2:
                raise PanoptesError(f"VM query value pair is malformed: {pair!r}.")
            epoch_seconds, value_text = pair[0], pair[1]
            if not isinstance(epoch_seconds, int | float):
                raise PanoptesError(f"VM query timestamp is not numeric: {epoch_seconds!r}.")
            if not isinstance(value_text, str):
                raise PanoptesError(f"VM query sample value is not a string: {value_text!r}.")
            timestamp = datetime.fromtimestamp(float(epoch_seconds), tz=UTC)
            points.append((timestamp, float(value_text)))
        return points
