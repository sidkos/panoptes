"""The `prometheus` core source — read-only PromQL scrape into `MetricSignal`s.

`fetch` GETs `<url>/api/v1/query_range` (window-aware) for each configured PromQL `query`
and normalizes the Prometheus JSON envelope into one `MetricSignal` per sample, stamping the
mandatory `env`. Each series' `__name__` label becomes the metric name (falling back to the
query expr when absent); the remaining series labels are carried, with `env` ALWAYS the
source's authoritative value (never an upstream `env` label silently overwriting it — the
model invariant).

Capability set: `{METRIC}` — prometheus is a metric-only source (no LOG/INCIDENT/TRACE).

The envelope shapes the normalizer handles (both `query` and `query_range` return the same
envelope, only the per-result key differs):

- `{"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {...},
  "values": [[ts, "v"], ...]}]}}` — the range/time-series shape (one sample per `values` pair);
- `{"status": "success", "data": {"resultType": "vector", "result": [{"metric": {...},
  "value": [ts, "v"]}]}}` — the instant shape (one sample per `value`).

A non-`success` envelope status (a Prometheus query error returned with HTTP 200) yields no
signals for that query rather than crashing the cycle — the error is the upstream's, not a
transport failure to surface.

**Read-only (documented known-miss).** The source issues only httpx `GET`s. The no-write
guard is boto3-shaped (snake_case mutation verbs) and does not see httpx GETs — the same
documented read-only known-miss as the v0.1 sentry/http-health sources. The authoritative
control is the read-only endpoint/token, not the boto3 grep; no guard change is needed.

The transport plumbing (the injectable `httpx.Client` seam, `raise_for_status`, and the
body-surfacing failure formatting) lives once in `core.rest`; this source delegates its
query GETs to `RestClient.get_json` (so an HTTP 4xx/5xx surfaces the upstream response BODY
via `_format_failure`) and keeps only its URL building + envelope normalization.

httpx is mocked in tests with `respx`, which patches the transport globally, so the
`RestClient`'s default `httpx.Client()` is intercepted without an injected client; the
client is still threaded as a constructor seam for explicit control.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import httpx

from core.model import (
    CanonicalSignal,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.rest import RestClient
from core.sources.probe import probe_health
from core.validation import require_str_field, require_str_list_field

# The mandatory env label every signal carries; the source's value is authoritative.
_ENV_LABEL = "env"
# The Prometheus convention for the series' metric name lives in this label.
_METRIC_NAME_LABEL = "__name__"

# A sane range step (seconds) so `query_range` returns multiple points over the window
# rather than one degenerate bucket. Mirrors the MCP tools' sub-window step discipline.
_RANGE_STEP_SECONDS = 60


@SOURCES.register("prometheus")
class PrometheusSource:
    """Reads PromQL query results from a Prometheus endpoint into `MetricSignal`s."""

    type = "prometheus"

    # An unreachable prometheus source means the endpoint/transport is unusable, so a fetch
    # is pointless and its signals must NOT reach the store — keep the collector's default
    # skip-on-unreachable behavior (F3a, same as sentry/cloudwatch).
    fetch_when_unreachable = False

    def __init__(self, config: ConfigBlock, client: httpx.Client | None = None) -> None:
        """Read `url`/`queries`/`env` from config; accept an injectable httpx client seam.

        The `client` seam mirrors the sentry/http-health pattern: under `respx` the default
        `httpx.Client()` is intercepted globally, so production passes none and tests need
        not inject one — the seam exists for explicit control.
        """
        # The Prometheus base URL (e.g. `http://prometheus:9090`); a trailing slash is
        # stripped so endpoint paths join cleanly.
        self._url = require_str_field(config, "url", self.type).rstrip("/")
        # The PromQL queries to scrape — each is fetched over the window and normalized.
        self._queries = require_str_list_field(config, "queries", self.type)
        # `env` is mandatory: stamped on every emitted signal (the model invariant).
        self._env = require_str_field(config, "env", self.type)
        self._rest = RestClient(client)

    def capabilities(self) -> set[SignalKind]:
        """prometheus emits metric samples only (no logs/incidents/traces)."""
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Scrape each configured query over `window` and normalize every sample.

        For each query a single `query_range` GET is issued over the window; the returned
        series are normalized into `MetricSignal`s. An HTTP 4xx/5xx surfaces the upstream
        body via the shared `RestClient.get_json`; a query whose envelope status is not
        `success` (a Prometheus-side query error returned with HTTP 200) contributes no
        signals rather than aborting the whole fetch.
        """
        signals: list[CanonicalSignal] = []
        for query in self._queries:
            payload = self._scrape(query, window)
            signals.extend(self._normalize(query, payload))
        return signals

    def health(self) -> SourceHealth:
        """Probe reachability via a cheap `GET /-/healthy`, delegating to the probe seam.

        Prometheus exposes `/-/healthy` as a trivial liveness endpoint. The no-raise +
        no-`str(exc)`-leak discipline lives in `core.sources.probe.probe_health`: any failure
        becomes `reachable=False` with a generic class-name-only detail (never a verbatim
        `str(exc)` that could echo a token/endpoint through the MCP-visible `describe_health`
        rollup), so this method is just the label + the probe lambda.
        """

        # `/-/healthy` returns plain text ("Prometheus is Healthy."), not JSON — so use the raw
        # `send` (raise_for_status + body-surfacing) rather than `get_json` (which would try to
        # `.json()`-decode the text body and fail spuriously). A failure raises a PanoptesError
        # wrapping the transport cause; probe_health names that underlying cause.
        def _probe() -> object:
            return self._rest.send(
                lambda http: http.get(f"{self._url}/-/healthy"),
                prefix="prometheus health probe failed",
                identifier=self._url,
            )

        return probe_health(
            "prometheus endpoint",
            _probe,
            success_detail_factory=lambda _result: f"prometheus endpoint reachable ({self._url})",
        )

    def _scrape(self, query: str, window: TimeWindow) -> object:
        """GET `query_range` for one query over the window, returning the decoded JSON.

        Delegates to `RestClient.get_json`, so a 4xx/5xx raises a `PanoptesError` carrying
        the surfaced upstream body (the slack-notifier/sentry body-surfacing discipline).
        """
        params = {
            "query": query,
            "start": str(int(window.start.timestamp())),
            "end": str(int(window.end.timestamp())),
            "step": str(_RANGE_STEP_SECONDS),
        }
        return self._rest.get_json(
            f"{self._url}/api/v1/query_range",
            prefix="prometheus query failed",
            identifier=query,
            params=params,
        )

    def _normalize(self, query: str, payload: object) -> list[MetricSignal]:
        """Normalize a Prometheus envelope into `MetricSignal`s (matrix OR vector).

        Returns no signals when the envelope status is not `success` (a Prometheus-side
        query error returned with HTTP 200) — the cycle continues with the next query.
        """
        if not isinstance(payload, dict):
            return []
        if payload.get("status") != "success":
            # A Prometheus query error (e.g. bad PromQL) returned with HTTP 200 — no samples.
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        results = data.get("result")
        if not isinstance(results, list):
            return []
        signals: list[MetricSignal] = []
        for result in results:
            signals.extend(self._normalize_result(query, result))
        return signals

    def _normalize_result(self, query: str, result: object) -> list[MetricSignal]:
        """Normalize one series (a `result` entry) into one `MetricSignal` per sample."""
        if not isinstance(result, dict):
            return []
        metric_labels = result.get("metric")
        labels = metric_labels if isinstance(metric_labels, dict) else {}
        name = self._metric_name(labels, query)
        signal_labels = self._build_labels(labels)
        samples = self._extract_samples(result)
        signals: list[MetricSignal] = []
        for raw_timestamp, raw_value in samples:
            parsed = self._parse_sample(raw_timestamp, raw_value)
            if parsed is None:
                continue
            timestamp, value = parsed
            signals.append(
                MetricSignal(
                    name=name,
                    value=value,
                    timestamp=timestamp,
                    labels=dict(signal_labels),
                )
            )
        return signals

    @staticmethod
    def _extract_samples(result: dict[str, object]) -> list[Sequence[object]]:
        """Return a series' samples as `[timestamp, value]` pairs (matrix OR vector).

        A `matrix` result carries `values: [[ts, "v"], ...]` (multiple samples); a `vector`
        result carries `value: [ts, "v"]` (a single sample). Either is normalized to a list
        of pairs the caller iterates.
        """
        values = result.get("values")
        if isinstance(values, list):
            # matrix: a list of [ts, "v"] pairs.
            return [pair for pair in values if isinstance(pair, list | tuple)]
        single = result.get("value")
        if isinstance(single, list | tuple):
            # vector: a single [ts, "v"] pair.
            return [single]
        return []

    @staticmethod
    def _metric_name(labels: Mapping[str, object], query: str) -> str:
        """The metric name: the series `__name__`, falling back to the query expr."""
        name = labels.get(_METRIC_NAME_LABEL)
        if isinstance(name, str) and name:
            return name
        return query

    def _build_labels(self, labels: Mapping[str, object]) -> dict[str, str]:
        """Build the signal's labels: the series labels (minus `__name__`) + the stamped env.

        The source's `env` is AUTHORITATIVE — it is set LAST, so an upstream series carrying
        its own `env` label can never silently overwrite it (the model invariant: every
        signal carries the source's env, not the scraped target's).
        """
        built: dict[str, str] = {}
        for key, value in labels.items():
            if key == _METRIC_NAME_LABEL:
                # `__name__` becomes the metric NAME, not a label.
                continue
            if isinstance(value, str):
                built[key] = value
        # env stamped LAST so it wins over any clashing upstream `env` label.
        built[_ENV_LABEL] = self._env
        return built

    @staticmethod
    def _parse_sample(raw_timestamp: object, raw_value: object) -> tuple[datetime, float] | None:
        """Parse a `[unix_seconds, "value"]` Prometheus sample into `(datetime, float)`.

        Prometheus serializes the timestamp as a unix-seconds number and the value as a
        STRING. A malformed sample (non-numeric ts/value) is skipped rather than aborting the
        series (the sentry timestamp-parse discipline).
        """
        if not isinstance(raw_timestamp, int | float):
            return None
        try:
            value = float(raw_value) if isinstance(raw_value, str | int | float) else None
        except ValueError:
            return None
        if value is None:
            return None
        timestamp = datetime.fromtimestamp(float(raw_timestamp), tz=UTC)
        return timestamp, value
