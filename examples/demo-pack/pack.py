"""The brand-neutral demo consumer pack — the injection-path proof (Phase 7).

This module is the *fixture* that exercises Panoptes' consumer-pack injection end to
end WITHOUT `core` ever importing it. A real consumer keeps an equivalent `pack.py`
in ITS OWN repo and points `dashboards.consumer_pack.path` / `PANOPTES_CONSUMER_PACK`
at it; everything here is a generic synthetic example a consumer replaces wholesale.

It demonstrates both halves of the injection contract (spec § Consumer-pack loading):

1. **Custom adapter registration.** On import it registers a tiny synthetic `Store`
   under the unique type ``demo-synthetic`` via the *core* registry — proving a pack
   can extend a plane. The registration is purely ADDITIVE: it adds a new key to the
   `STORES` registry and changes nothing core registered (the additive-injection
   guard asserts this).
2. **MCP tool registration.** It exposes the ``register_tools(mcp_server)`` hook the
   Phase-6 server calls (``core/mcp/server.py::_load_consumer_pack``). The hook adds a
   single read-only ``get_demo_signal(env, window) -> DemoSignal`` tool that reads
   ``panoptes_*`` series from the shared store and returns a precise nested TypedDict.

The dependency arrow points ONE way: this pack imports from ``core``; ``core`` never
imports this pack (the import is dynamic + env-var-driven). That is the entire point —
injection, not bundling.

IMPORTANT (FastMCP / PEP-563): this module registers an MCP tool returning the nested
``DemoSignal`` TypedDict, so it must NOT add ``from __future__ import annotations`` —
deferred annotations break FastMCP's schema generation for nested-TypedDict returns.
FastMCP also rejects ``*args``/``**kwargs`` tool signatures, so the registered tool
presents the concrete ``env`` / ``window`` parameters.

No-write discipline: this module uses only READ paths (``store.query``); it issues no
boto3 mutation-verb calls (the no-write guard scans ``examples/`` too).
"""

from typing import Protocol, TypedDict

from core.mcp.tools_query import escape_promql_value
from core.model import CanonicalSignal, MetricQuery, MetricSeries, TimeWindow
from core.registry import STORES, ConfigBlock

# The trailing window the demo tool reads (v0.1 has no duration-string parser; the
# `window` argument is accepted + echoed back so the surface is forward-compatible).
_DEFAULT_WINDOW_MINUTES = 15

# The single brand-neutral derived metric the demo reads. A real consumer pack would
# query its OWN domain metrics here; the demo deliberately reuses a generic core gauge
# so it carries zero domain content.
_DEMO_METRIC = "panoptes_health_up"


class DemoMetricPoint(TypedDict):
    """One resolved `panoptes_*` series the demo signal surfaces (nested in DemoSignal)."""

    metric: str
    labels: dict[str, str]
    value: float


class DemoSignal(TypedDict):
    """The demo pack's synthetic read-only signal over `panoptes_*` store metrics.

    A precise nested TypedDict (R8) — `env` + the echoed `window`, the per-series
    latest values pulled from the store, and a roll-up `sample_count`. A real consumer
    tool returns its own domain-shaped TypedDict the same way; this one stays generic.
    """

    env: str
    window: str
    metrics: list[DemoMetricPoint]
    sample_count: int


class _StoreLike(Protocol):
    """The minimal read surface `get_demo_signal` needs from the shared store.

    Declared as a Protocol so the tool depends only on `query(...)` (the read path) —
    never on a concrete store class, and never on any write method.
    """

    def query(self, query: MetricQuery) -> list[MetricSeries]: ...


@STORES.register("demo-synthetic")
class DemoSyntheticStore:
    """A trivial synthetic store the demo pack contributes to the core registry.

    Proves a consumer pack can register a custom adapter on a core plane. It persists
    nothing and answers queries with an empty series list (read-only, no upstream): it
    exists to demonstrate ADDITIVE registration, not to back a real deployment. A real
    consumer would register a genuine adapter here.
    """

    type = "demo-synthetic"

    def __init__(self, config: ConfigBlock) -> None:
        # The single-positional-`ConfigBlock` constructor is the locked registry
        # construction convention; the synthetic store needs no config of its own.
        self._config = config

    def write(self, signals: list[CanonicalSignal]) -> None:
        """No-op: the synthetic store persists nothing (read-only demo)."""
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        """Answer with no series — the synthetic store backs no real data."""
        return []


def _latest_value(series: MetricSeries) -> float:
    """The most recent sample value in a series (0.0 when the series is empty)."""
    if not series.points:
        return 0.0
    # points are (timestamp, value); the last point is the most recent sample.
    return series.points[-1][1]


def get_demo_signal(store: _StoreLike, env: str, window: str) -> DemoSignal:
    """Read a generic `panoptes_*` metric from the store into a typed `DemoSignal`.

    This is the brand-neutral synthetic tool the demo pack registers. It is fully
    read-only: it issues a single PromQL passthrough `store.query` for a generic core
    gauge scoped to `env`, then folds the returned series into the nested `DemoSignal`
    shape. A real consumer's tool reads its OWN metrics the same way.

    Args:
        store: The shared store (only its read `query(...)` is used).
        env: The environment to scope the query to (added as an `env=` matcher).
        window: The query window string (echoed back; v0.1 maps it to a default window).

    Returns:
        A `DemoSignal` with the per-series latest values + a roll-up sample count.
    """
    # Escape the env for the double-quoted PromQL string so a value containing a quote /
    # backslash cannot break out of the selector (F7). Reuse the canonical core primitive
    # (F2d) rather than hand-copying the two `.replace(...)` calls — a copy could drift and
    # miss the load-bearing backslash-first ordering. This is exactly how a real consumer
    # pack should reuse the escape: import it from `core.mcp.tools_query`.
    escaped_env = escape_promql_value(env)
    expr = f'{_DEMO_METRIC}{{env="{escaped_env}"}}'
    metric_query = MetricQuery(
        expr=expr,
        window=TimeWindow.last(minutes=_DEFAULT_WINDOW_MINUTES),
        step_seconds=_DEFAULT_WINDOW_MINUTES * 60,
    )
    series_list = store.query(metric_query)

    metrics: list[DemoMetricPoint] = [
        DemoMetricPoint(
            metric=series.metric, labels=dict(series.labels), value=_latest_value(series)
        )
        for series in series_list
    ]
    sample_count = sum(len(series.points) for series in series_list)
    return DemoSignal(env=env, window=window, metrics=metrics, sample_count=sample_count)


def register_tools(mcp_server: object) -> None:
    """The injection hook the Phase-6 server calls — adds `get_demo_signal` read-only.

    `core/mcp/server.py::_load_consumer_pack` imports this module (named by
    `PANOPTES_CONSUMER_PACK`) and calls `register_tools(server)` with the live
    `PanoptesMcpServer`. The server binds its resolved `store` into the tool wrapper so
    the FastMCP-facing signature carries only the caller-supplied `env` / `window`
    (concrete params — FastMCP rejects `*args`/`**kwargs`) and returns the nested
    `DemoSignal`.

    The argument is typed `object` so this module never imports a `core.mcp.server`
    symbol at module scope (keeping the pack importable in isolation); the two
    attributes the server exposes — its bound store and the `_register_tool` seam — are
    resolved dynamically. This mirrors how a real consumer pack, authored against the
    documented hook contract, registers its own tools without a hard import of the
    server's concrete class.
    """
    # The server exposes its resolved config (carrying the shared store) and the
    # `_register_tool(name, fn)` registration seam. Resolve both dynamically so the
    # pack depends on the documented hook CONTRACT, not the server's concrete type.
    config = mcp_server._config  # type: ignore[attr-defined]
    store: _StoreLike = config.store

    def get_demo_signal_tool(env: str, window: str) -> DemoSignal:
        """Read `panoptes_*` metrics for `env` into the synthetic `DemoSignal`."""
        return get_demo_signal(store, env=env, window=window)

    mcp_server._register_tool("get_demo_signal", get_demo_signal_tool)  # type: ignore[attr-defined]
