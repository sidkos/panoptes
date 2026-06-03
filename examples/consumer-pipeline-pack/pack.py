"""The brand-neutral pipeline consumer pack — the genericity proof's consumer #2 (v0.3 Phase 5).

This is the SECOND, deliberately UNRELATED consumer fixture (a generic data-pipeline
platform: job lag, queue depth, data freshness). It shares NOTHING domain-wise with the
game-server-fleet pack (consumer #1) — that disjointness is the whole point: if the SAME
core injects both with a byte-identical core baseline between them, the core provably makes
no per-consumer assumption (spec § "the genericity proof", Risk G2).

A real data-pipeline platform keeps an equivalent `pack.py` in ITS OWN repo and points
`dashboards.consumer_pack.path` / `PANOPTES_CONSUMER_PACK` at it; everything here is a
generic synthetic example a consumer replaces wholesale.

It demonstrates the injection contract, with a DIFFERENT source style than the fleet pack:

1. **Custom source registration.** On import it registers a `pipeline` source under the core
   `SOURCES` registry — purely ADDITIVE (it adds one key, changes nothing core registered).
2. **A STANDALONE source (the deliberate contrast with fleet).** Unlike the fleet source,
   which COMPOSES the core `prometheus` source, `PipelineSource` is a self-contained
   read-only HTTP source built directly on the shared `core.rest.RestClient` (httpx GET).
   This proves the core supports BOTH styles — wrapping a core source AND a fully
   independent adapter — so neither consumer's integration shape is privileged. It scrapes
   a generic pipeline-metrics JSON endpoint and normalizes job lag / queue depth / data
   freshness into `panoptes_pipeline_*` gauges, every signal `env`-stamped.
3. **MCP tool registration.** `register_tools(mcp_server)` adds a read-only
   `get_pipeline_lag(env) -> PipelineLag` tool reading the `panoptes_pipeline_*` gauges from
   the shared store and returning a precise TypedDict.

The dependency arrow points ONE way: this pack imports from `core`; `core` never imports it
(dynamic, env-var-driven). Injection, not bundling.

IMPORTANT (FastMCP / PEP-563): this module registers an MCP tool returning the nested
`PipelineLag` TypedDict, so it must NOT add `from __future__ import annotations` — deferred
annotations break FastMCP's schema generation for nested-TypedDict returns. FastMCP also
rejects `*args`/`**kwargs`, so the registered tool presents the concrete `env` parameter.

No-write discipline: READ paths only — the source issues httpx GETs via `RestClient`, and
the tool issues only `store.query`. No boto3 mutation-verb call exists (the no-write guard
scans `examples/` too). Consumer-domain tokens (`pipeline`/`queue`/`lag`/`freshness`) appear
here under `examples/` by design; no named consumer brand appears (the brand grep is 0).
"""

from datetime import UTC, datetime
from typing import Protocol, TypedDict

from core.errors import PanoptesError
from core.mcp.tools_query import escape_promql_value
from core.model import (
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.rest import RestClient
from core.validation import require_str_field

# The trailing window the pipeline tool reads (no duration-string parser yet; the gauge read
# is a latest-value snapshot, so the window is a small fixed lookback).
_DEFAULT_WINDOW_MINUTES = 15

# The generic pipeline-metrics endpoint the standalone source scrapes. A real consumer would
# point this at its own pipeline-metrics exporter; the shape is `{job_lag_seconds,
# queue_depth, freshness_seconds}` (plain JSON, not a Prometheus envelope — the deliberate
# contrast with the fleet pack's prometheus composition).
_PIPELINE_METRICS_PATH = "/pipeline/metrics"

# The JSON field → canonical `panoptes_pipeline_*` gauge mapping. A real consumer maps its own
# pipeline-metrics field names here.
_PIPELINE_FIELD_TO_METRIC = {
    "job_lag_seconds": "panoptes_pipeline_lag_seconds",
    "queue_depth": "panoptes_pipeline_queue_depth",
    "freshness_seconds": "panoptes_pipeline_freshness_seconds",
}

# The canonical pipeline gauges `get_pipeline_lag` reads back, keyed by the PipelineLag field.
_PIPELINE_GAUGE_BY_FIELD = {
    "lag_seconds": "panoptes_pipeline_lag_seconds",
    "queue_depth": "panoptes_pipeline_queue_depth",
    "freshness_seconds": "panoptes_pipeline_freshness_seconds",
}


class PipelineLag(TypedDict):
    """The pipeline pack's read-only freshness snapshot over the `panoptes_pipeline_*` gauges.

    A precise TypedDict (R8) — `env` + the latest job-lag / queue-depth / data-freshness
    values pulled from the store. A real consumer tool returns its own domain-shaped TypedDict
    the same way; this one stays a generic data-pipeline example.
    """

    env: str
    lag_seconds: float
    queue_depth: float
    freshness_seconds: float


class _StoreLike(Protocol):
    """The minimal read surface `get_pipeline_lag` needs from the shared store.

    Declared as a Protocol so the tool depends only on `query(...)` (the read path) — never
    on a concrete store class, and never on any write method.
    """

    def query(self, query: MetricQuery) -> list[MetricSeries]: ...


@SOURCES.register("pipeline")
class PipelineSource:
    """A STANDALONE data-pipeline source (read-only httpx GET via the shared `RestClient`).

    Deliberately NOT built on the core `prometheus` source (the fleet pack already proves the
    composition style) — this is a self-contained adapter scraping a generic pipeline-metrics
    JSON endpoint and normalizing job lag / queue depth / data freshness into
    `panoptes_pipeline_*` gauges. It demonstrates that the core supports a fully independent
    consumer source adapter as cleanly as it supports one wrapping a core source.
    """

    type = "pipeline"

    # An unreachable upstream means the scrape is pointless and its signals must not reach the
    # store — inherit the collector's skip-on-unreachable behavior (same as the core sources).
    fetch_when_unreachable = False

    def __init__(self, config: ConfigBlock, client: object | None = None) -> None:
        """Read `url`/`env` from config; accept an injectable httpx client seam.

        The `client` seam mirrors the core HTTP sources: under `respx` the default
        `httpx.Client()` is intercepted globally, so production passes none and tests need not
        inject one. `client` is typed `object | None` so this pack never names `httpx` in its
        signature surface; it is narrowed to an `httpx.Client` for the `RestClient` only when
        supplied.
        """
        self._url = require_str_field(config, "url", self.type).rstrip("/")
        # `env` is mandatory: stamped on every emitted signal (the model invariant).
        self._env = require_str_field(config, "env", self.type)
        # Narrow the injected client for the RestClient seam (None -> RestClient builds one).
        import httpx

        rest_client = client if isinstance(client, httpx.Client) else None
        self._rest = RestClient(rest_client)

    def capabilities(self) -> set[SignalKind]:
        """The pipeline source emits metric samples only (no logs/incidents/traces)."""
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """GET the pipeline-metrics endpoint and normalize its fields into pipeline gauges.

        Issues a single read-only GET to the generic pipeline-metrics JSON endpoint; each
        recognized field (`job_lag_seconds`/`queue_depth`/`freshness_seconds`) becomes one
        `panoptes_pipeline_*` gauge stamped at the window end with `env`. An unrecognized or
        non-numeric field is skipped rather than aborting the fetch.
        """
        payload = self._rest.get_json(
            f"{self._url}{_PIPELINE_METRICS_PATH}",
            prefix="pipeline metrics scrape failed",
            identifier=self._url,
        )
        return self._normalize(payload, window.end)

    def _normalize(self, payload: object, sample_time: datetime) -> list[CanonicalSignal]:
        """Normalize the pipeline-metrics JSON object into `panoptes_pipeline_*` gauges."""
        if not isinstance(payload, dict):
            return []
        signals: list[CanonicalSignal] = []
        for field_name, metric_name in _PIPELINE_FIELD_TO_METRIC.items():
            raw_value = payload.get(field_name)
            if not isinstance(raw_value, int | float) or isinstance(raw_value, bool):
                # A missing/non-numeric field (or a bool, which is an int subclass) is skipped.
                continue
            signals.append(
                MetricSignal(
                    name=metric_name,
                    value=float(raw_value),
                    timestamp=sample_time,
                    labels={"env": self._env},
                )
            )
        return signals

    def health(self) -> SourceHealth:
        """Probe reachability via a cheap GET of the metrics endpoint, catching any error.

        An unreachable endpoint is CAUGHT and surfaced as `reachable=False` (it does NOT
        propagate, so the collector's per-source try/continue boundary keeps the cycle
        running). The `detail` is a GENERIC summary (exception class only), never a verbatim
        `str(exc)` that could echo a token/endpoint (the core sources' F4 discipline).
        """
        checked_at = datetime.now(UTC)
        try:
            self._rest.send(
                lambda http: http.get(f"{self._url}{_PIPELINE_METRICS_PATH}"),
                prefix="pipeline health probe failed",
                identifier=self._url,
            )
        except PanoptesError as exc:
            return SourceHealth(
                reachable=False,
                detail=(
                    f"pipeline endpoint unreachable "
                    f"(auth/transport error: {type(exc.__cause__ or exc).__name__})"
                ),
                checked_at=checked_at,
            )
        return SourceHealth(
            reachable=True,
            detail=f"pipeline endpoint reachable ({self._url})",
            checked_at=checked_at,
        )


def _latest_value(series_list: list[MetricSeries]) -> float:
    """The most recent sample value across the returned series (0.0 when none/empty)."""
    for series in series_list:
        if series.points:
            # points are (timestamp, value); the last point is the most recent sample.
            return series.points[-1][1]
    return 0.0


def get_pipeline_lag(store: _StoreLike, env: str) -> PipelineLag:
    """Read the `panoptes_pipeline_*` gauges for `env` from the store into a `PipelineLag`.

    Fully read-only: issues one PromQL passthrough `store.query` per canonical pipeline gauge,
    scoped to `env`, and folds the latest value of each into the `PipelineLag` shape. A real
    consumer's tool reads ITS own metrics the same way.

    Args:
        store: The shared store (only its read `query(...)` is used).
        env: The environment to scope the queries to (added as an `env=` matcher).

    Returns:
        A `PipelineLag` with the latest job-lag / queue-depth / data-freshness values.
    """
    # Reuse the canonical core escape primitive so an env containing a quote/backslash cannot
    # break out of the double-quoted PromQL selector (F7) — never a hand-copied escape.
    escaped_env = escape_promql_value(env)
    values: dict[str, float] = {}
    for field_name, metric_name in _PIPELINE_GAUGE_BY_FIELD.items():
        expr = f'{metric_name}{{env="{escaped_env}"}}'
        metric_query = MetricQuery(
            expr=expr,
            window=TimeWindow.last(minutes=_DEFAULT_WINDOW_MINUTES),
            step_seconds=_DEFAULT_WINDOW_MINUTES * 60,
        )
        values[field_name] = _latest_value(store.query(metric_query))
    return PipelineLag(
        env=env,
        lag_seconds=values["lag_seconds"],
        queue_depth=values["queue_depth"],
        freshness_seconds=values["freshness_seconds"],
    )


def register_tools(mcp_server: object) -> None:
    """The injection hook the server calls — adds the read-only `get_pipeline_lag` tool.

    `core/mcp/server.py::_load_consumer_pack` imports this module (named by
    `PANOPTES_CONSUMER_PACK`) and calls `register_tools(server)` with the live
    `PanoptesMcpServer`. The server's resolved config carries the shared store; this hook
    binds it into the tool wrapper so the FastMCP-facing signature carries only the
    caller-supplied `env` (a concrete param — FastMCP rejects `*args`/`**kwargs`) and returns
    the nested `PipelineLag`.

    `mcp_server` is typed `object` so this module never imports a `core.mcp.server` symbol at
    module scope (keeping the pack importable in isolation); the bound store + the
    `_register_tool` seam are resolved dynamically — mirroring how a real consumer pack,
    authored against the documented hook contract, registers tools without a hard import of
    the server's concrete class.
    """
    config = mcp_server._config  # type: ignore[attr-defined]
    store: _StoreLike = config.store

    def get_pipeline_lag_tool(env: str) -> PipelineLag:
        """Read the `panoptes_pipeline_*` gauges for `env` into the typed `PipelineLag`."""
        return get_pipeline_lag(store, env=env)

    mcp_server._register_tool("get_pipeline_lag", get_pipeline_lag_tool)  # type: ignore[attr-defined]
