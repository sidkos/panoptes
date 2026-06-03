"""The brand-neutral fleet consumer pack — the genericity proof's consumer #1 (v0.3 Phase 4).

This module is a *fixture* exercising Panoptes' consumer-pack injection for a SECOND,
unrelated consumer (a generic game-server-fleet platform) WITHOUT any core change between it
and the demo pack. A real platform keeps an equivalent `pack.py` in ITS OWN repo and points
`dashboards.consumer_pack.path` / `PANOPTES_CONSUMER_PACK` at it; everything here is a
generic synthetic example a consumer replaces wholesale.

It demonstrates THREE halves of the injection contract — and one thing the demo pack does
not: a consumer source that BUILDS ON a core source.

1. **Custom source registration.** On import it registers a `fleet` source under the core
   `SOURCES` registry. Purely ADDITIVE — it adds one key and changes nothing core registered
   (the additive-injection guard asserts this).
2. **Building on a core source (the genericity proof's crux).** `FleetSource` does NOT
   re-implement Prometheus scraping: it COMPOSES the core `core.sources.prometheus`
   `PrometheusSource`, delegates `fetch` to it, then RELABELS the scraped Agones-style
   `agones_fleets_replicas_count{type=...}` series into the canonical
   `panoptes_fleet_ready`/`_allocated`/`_reserved` gauges. This proves the consumer→core
   dependency direction (Risk G1/G6): the consumer depends on core, never the reverse (the
   v0.1 structural import guard enforces `core/`↛`examples/`).
3. **MCP tool registration.** It exposes `register_tools(mcp_server)`; the hook adds a
   read-only `get_fleet_health(env) -> FleetHealth` tool that reads the `panoptes_fleet_*`
   gauges from the shared store and returns a precise TypedDict.

The dependency arrow points ONE way: this pack imports from `core`; `core` never imports it
(dynamic, env-var-driven). That is the whole point — injection, not bundling.

IMPORTANT (FastMCP / PEP-563): this module registers an MCP tool returning the nested
`FleetHealth` TypedDict, so it must NOT add `from __future__ import annotations` — deferred
annotations break FastMCP's schema generation for nested-TypedDict returns. FastMCP also
rejects `*args`/`**kwargs` tool signatures, so the registered tool presents the concrete
`env` parameter.

No-write discipline: this module uses only READ paths — the source delegates to the core
prometheus source's httpx GET scrape, and the tool issues only `store.query`. No boto3
mutation-verb call exists (the no-write guard scans `examples/` too).

Consumer-domain tokens (`agones`/`fleet`/`allocated`) appear here under `examples/` by
design (the core-purity guard scans core/+modules/+deploy/+charts, NOT examples/). No named
consumer brand appears anywhere, including this fixture (the brand-neutrality grep is 0).
"""

from typing import Protocol, TypedDict

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
from core.sources.prometheus import PrometheusSource

# The trailing window the fleet tool reads (no duration-string parser yet; the gauge read is
# a latest-value snapshot, so the window is a small fixed lookback).
_DEFAULT_WINDOW_MINUTES = 15

# The Agones metric the source scrapes from the consumer's Prometheus. Its series carry a
# `type` label (ready/allocated/reserved) that the source maps into the canonical gauges.
_FLEET_REPLICAS_METRIC = "agones_fleets_replicas_count"
# The series label whose value selects the canonical fleet gauge.
_FLEET_TYPE_LABEL = "type"

# Maps each Agones replica `type` to its canonical `panoptes_fleet_*` gauge name. A real
# consumer would map its own domain metric→canonical-name relationships here.
_FLEET_TYPE_TO_METRIC = {
    "ready": "panoptes_fleet_ready",
    "allocated": "panoptes_fleet_allocated",
    "reserved": "panoptes_fleet_reserved",
}

# The canonical fleet gauges `get_fleet_health` reads back from the store, keyed by the
# FleetHealth field each populates.
_FLEET_GAUGE_BY_FIELD = {
    "ready": "panoptes_fleet_ready",
    "allocated": "panoptes_fleet_allocated",
    "reserved": "panoptes_fleet_reserved",
}


class FleetHealth(TypedDict):
    """The fleet pack's read-only fleet snapshot over the `panoptes_fleet_*` store gauges.

    A precise TypedDict (R8) — `env` + the latest ready/allocated/reserved replica counts
    pulled from the store. A real consumer tool returns its own domain-shaped TypedDict the
    same way; this one stays a generic game-server-fleet example.
    """

    env: str
    ready: float
    allocated: float
    reserved: float


class _StoreLike(Protocol):
    """The minimal read surface `get_fleet_health` needs from the shared store.

    Declared as a Protocol so the tool depends only on `query(...)` (the read path) — never
    on a concrete store class, and never on any write method.
    """

    def query(self, query: MetricQuery) -> list[MetricSeries]: ...


@SOURCES.register("fleet")
class FleetSource:
    """A consumer fleet source that BUILDS ON the core `prometheus` source.

    It composes a `core.sources.prometheus.PrometheusSource` (configured to scrape the
    Agones `agones_fleets_replicas_count` series from the consumer's Prometheus), delegates
    `fetch` to it, then relabels each scraped series — by its `type` label — into the
    canonical `panoptes_fleet_ready`/`_allocated`/`_reserved` gauges, dropping the raw `type`
    label and keeping the source's authoritative `env`. This is the genericity proof: a
    consumer extends a core plane by REUSING a core source, never re-implementing it.
    """

    type = "fleet"

    # An unreachable upstream means the scrape is pointless and its signals must not reach the
    # store — inherit the collector's skip-on-unreachable behavior (same as the core sources).
    fetch_when_unreachable = False

    def __init__(self, config: ConfigBlock) -> None:
        """Build the composed core prometheus source from the fleet config.

        The fleet config carries the same `url`/`env` the prometheus source needs; the
        `queries` are FIXED to the Agones fleet-replicas metric here (the consumer's source
        owns which upstream series it scrapes), so a consumer config need only supply the
        endpoint + env. The composed `PrometheusSource` is instantiated via its normal
        constructor — proving the reuse is genuine composition, not a copy of its logic.
        """
        prometheus_config: dict[str, str | int | bool | list[str]] = {
            "url": config["url"],
            "env": config["env"],
            # The fleet source decides which upstream series to scrape; the consumer config
            # supplies only the endpoint + env, keeping the fleet contract narrow.
            "queries": [_FLEET_REPLICAS_METRIC],
        }
        self._prometheus = PrometheusSource(prometheus_config)
        # Keep the env for the relabel (the authoritative value stamped on every signal).
        self._env = str(config["env"])

    def prometheus_source(self) -> PrometheusSource:
        """Return the composed core `PrometheusSource` (the composition seam the test asserts)."""
        return self._prometheus

    def capabilities(self) -> set[SignalKind]:
        """The fleet source emits metric samples only (it builds on a metric-only source)."""
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Delegate the scrape to the core prometheus source, then relabel into fleet gauges.

        The composed prometheus source does the actual httpx GET + envelope normalization
        (read-only); this method only maps each returned `agones_fleets_replicas_count`
        series — by its `type` label — into the canonical `panoptes_fleet_*` gauge, dropping
        the `type` label. A series whose `type` is unknown is skipped (not a fleet replica
        gauge we model).
        """
        scraped = self._prometheus.fetch(window)
        relabeled: list[CanonicalSignal] = []
        for signal in scraped:
            # Only metric samples from the fleet-replicas series are fleet gauges.
            if not isinstance(signal, MetricSignal):
                continue
            canonical = self._relabel(signal)
            if canonical is not None:
                relabeled.append(canonical)
        return relabeled

    def _relabel(self, signal: MetricSignal) -> MetricSignal | None:
        """Map one scraped replica-count sample into its canonical `panoptes_fleet_*` gauge.

        Reads the `type` label to pick the canonical gauge name, drops `type` from the label
        set (it is now encoded in the metric name), and re-stamps `env`. Returns `None` for a
        series whose `type` is missing/unmodeled so an unrelated series cannot masquerade as a
        fleet gauge. `MetricSignal` is frozen, so this constructs a NEW signal.
        """
        fleet_type = signal.labels.get(_FLEET_TYPE_LABEL)
        canonical_name = _FLEET_TYPE_TO_METRIC.get(fleet_type) if fleet_type is not None else None
        if canonical_name is None:
            return None
        # Drop the raw `type` label (now encoded in the metric name); keep the rest + env.
        labels = {key: value for key, value in signal.labels.items() if key != _FLEET_TYPE_LABEL}
        labels["env"] = self._env
        return MetricSignal(
            name=canonical_name,
            value=signal.value,
            timestamp=signal.timestamp,
            labels=labels,
        )

    def health(self) -> SourceHealth:
        """Delegate the reachability probe to the composed prometheus source.

        The fleet source's reachability IS the upstream Prometheus's reachability, so reuse
        the core source's `health()` rather than re-probing — another facet of building on the
        core source.
        """
        return self._prometheus.health()


def _latest_value(series_list: list[MetricSeries]) -> float:
    """The most recent sample value across the returned series (0.0 when none/empty)."""
    for series in series_list:
        if series.points:
            # points are (timestamp, value); the last point is the most recent sample.
            return series.points[-1][1]
    return 0.0


def get_fleet_health(store: _StoreLike, env: str) -> FleetHealth:
    """Read the `panoptes_fleet_*` gauges for `env` from the store into a typed `FleetHealth`.

    Fully read-only: issues one PromQL passthrough `store.query` per canonical fleet gauge,
    scoped to `env`, and folds the latest value of each into the `FleetHealth` shape. A real
    consumer's tool reads ITS own metrics the same way.

    Args:
        store: The shared store (only its read `query(...)` is used).
        env: The environment to scope the queries to (added as an `env=` matcher).

    Returns:
        A `FleetHealth` with the latest ready/allocated/reserved replica counts.
    """
    # Escape the env for the double-quoted PromQL string so a value containing a quote /
    # backslash cannot break out of the selector (F7) — reuse the canonical core primitive
    # rather than hand-copying the escape (it could drift and miss the backslash-first order).
    escaped_env = escape_promql_value(env)
    counts: dict[str, float] = {}
    for field_name, metric_name in _FLEET_GAUGE_BY_FIELD.items():
        expr = f'{metric_name}{{env="{escaped_env}"}}'
        metric_query = MetricQuery(
            expr=expr,
            window=TimeWindow.last(minutes=_DEFAULT_WINDOW_MINUTES),
            step_seconds=_DEFAULT_WINDOW_MINUTES * 60,
        )
        counts[field_name] = _latest_value(store.query(metric_query))
    return FleetHealth(
        env=env,
        ready=counts["ready"],
        allocated=counts["allocated"],
        reserved=counts["reserved"],
    )


def register_tools(mcp_server: object) -> None:
    """The injection hook the server calls — adds the read-only `get_fleet_health` tool.

    `core/mcp/server.py::_load_consumer_pack` imports this module (named by
    `PANOPTES_CONSUMER_PACK`) and calls `register_tools(server)` with the live
    `PanoptesMcpServer`. The server's resolved config carries the shared store; this hook
    binds it into the tool wrapper so the FastMCP-facing signature carries only the
    caller-supplied `env` (a concrete param — FastMCP rejects `*args`/`**kwargs`) and returns
    the nested `FleetHealth`.

    `mcp_server` is typed `object` so this module never imports a `core.mcp.server` symbol at
    module scope (keeping the pack importable in isolation); the bound store + the
    `_register_tool` seam are resolved dynamically, mirroring how a real consumer pack —
    authored against the documented hook contract — registers tools without a hard import of
    the server's concrete class.
    """
    config = mcp_server._config  # type: ignore[attr-defined]
    store: _StoreLike = config.store

    def get_fleet_health_tool(env: str) -> FleetHealth:
        """Read the `panoptes_fleet_*` gauges for `env` into the typed `FleetHealth`."""
        return get_fleet_health(store, env=env)

    mcp_server._register_tool("get_fleet_health", get_fleet_health_tool)  # type: ignore[attr-defined]
