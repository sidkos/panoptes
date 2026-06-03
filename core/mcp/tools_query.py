"""MCP query tools — the read-only "show me the signals" surface.

These tools read live sources + the shared store over an explicit `QueryContext`
seam — a small read-only view of the resolved config (spec `## API Surface` → MCP
server → Query), so a tool test drives only the context, never a whole config:

- `query_metric` — a thin PromQL passthrough to the store. It is the only tool
  that reads the store directly with caller-supplied parameters, so it is the
  natural surface for a `passthrough`-store `CapabilityError` (surfaced structured,
  never crash / silent-empty).
- `search_incidents` / `search_logs` — fetch the requested env's sources, filter to
  the relevant signal kind, and return the matching signals.
- `search_traces` — capability-negotiation surface: no v0.1 source provides TRACE,
  so this always raises an explicit "no trace source" `CapabilityError`.
- `describe_health` — the "one thing to look at" rollup: per-source reachability
  (an unreachable source is INCLUDED, marked unreachable, never omitted) + the open
  incident count.

**Capability negotiation:** a query for a kind no configured source provides raises
an explicit `CapabilityError("no source for X")` — never a silent-empty result.

**`env="all"` fan-out:** when `env == "all"`, iterate every enabled env and return a
per-env result; an env whose required source is down/unconfigured is included with
an explicit per-env error marker rather than failing the whole call (partial result).

IMPORTANT (FastMCP / PEP-563): this module must NOT add
`from __future__ import annotations` — deferred annotations break FastMCP's schema
generation for the nested-`TypedDict` return shapes defined here.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypedDict

from core.config import ResolvedEnvironment, SloConfig
from core.errors import CapabilityError, PanoptesError
from core.mcp._metric_helpers import (
    _DEFAULT_WINDOW_MINUTES,
    _latest_value,
    _step_seconds_for,
    _window_for,
    escape_promql_value,
)
from core.mcp.context import QueryContext
from core.model import (
    IncidentSignal,
    LogSignal,
    MetricQuery,
    MetricSeries,
    SignalKind,
    TimeWindow,
)
from core.planes.source import Source

# Re-export the canonical PromQL escape + window helpers so the PUBLIC import path
# `from core.mcp.tools_query import escape_promql_value` keeps working for consumer packs (the
# demo/fleet/pipeline packs depend on it), AND the back-compat private paths
# `from core.mcp.tools_query import _window_for / _DEFAULT_WINDOW_MINUTES` keep resolving for the
# existing tools_query tests. These symbols now LIVE in `core.mcp._metric_helpers` (the leaf
# module that breaks the context↔tools_query import cycle); listing them in `__all__` marks them
# as explicitly re-exported (so `mypy --strict` recognizes the re-export). New code imports from
# `_metric_helpers` directly.
__all__ = ["_DEFAULT_WINDOW_MINUTES", "_window_for", "escape_promql_value"]

_LOGGER = logging.getLogger(__name__)

# `env="all"` is the fan-out sentinel — iterate every enabled env (spec § MCP
# server contract — "Accept and respect an `env` argument (or `all`)").
_ALL_ENVS = "all"

# PromQL identifier pattern for a metric name or a label KEY (F7). Caller-supplied
# `name`/label-keys are spliced into the selector unquoted, so they MUST be validated as
# real PromQL identifiers; anything with a `"`/`{`/`}`/`\` (or other breakout char) is
# rejected rather than corrupting the query. Label VALUES are quoted, so they are ESCAPED
# (not identifier-validated) before interpolation.
_PROMQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:]*$")

# The window-parsing + step + default-window helpers now live in `core.mcp._metric_helpers`
# (the leaf module that breaks the context↔tools_query cycle); `_DEFAULT_WINDOW_MINUTES`,
# `_window_for`, and `_step_seconds_for` are imported from there above. They are also
# re-exported (back-compat) so the existing tools_query tests that import `_window_for` /
# `_DEFAULT_WINDOW_MINUTES` keep resolving.

# The key derived health metrics `describe_health` surfaces (F2g — spec § MCP server:
# `describe_health -> HealthRollup` = per-source reachability + "key derived metrics" +
# open incident count). `panoptes_health_up` is the overview pack's traffic-light backing;
# latency is included when the store has it. A metric absent from the store simply does
# not appear (best-effort enrichment — reachability remains the rollup's mandatory promise).
_KEY_HEALTH_METRICS = ("panoptes_health_up", "panoptes_health_latency_ms")

# The four derived kubernetes gauges `get_cluster_state` reads back from the store — the
# SAME series the kubernetes source writes (two-faces-one-store parity). The cluster-wide
# three carry exactly one series per env (one value); `pod_restarts_total` is per-namespace
# (summed across its series). Kept aligned with `core/sources/kubernetes.py` metric names.
_K8S_NODE_COUNT = "panoptes_k8s_node_count"
_K8S_PODS_PENDING = "panoptes_k8s_pods_pending"
_K8S_PODS_CRASHLOOP = "panoptes_k8s_pods_crashloop"
_K8S_POD_RESTARTS_TOTAL = "panoptes_k8s_pod_restarts_total"

# The cost gauges `get_cost` reads back from the store — the SAME series the cloudwatch
# CE/budgets cost path writes (two-faces-one-store parity; the Cost dashboard renders them).
# `panoptes_cost_spend` is per-service (one series per `{env, service}`); `budget_burn` is
# one series per env. Kept aligned with `core/sources/cloudwatch.py` metric names.
_COST_SPEND = "panoptes_cost_spend"
_COST_BUDGET_BURN = "panoptes_cost_budget_burn"

# The default PromQL the `get_slo` tool measures when a SLO declares no explicit `query`:
# the availability gauge the overview pack already backs (an uptime SLO is the common case).
_DEFAULT_SLO_QUERY = "panoptes_health_up"

# The lower clamp for `error_budget_remaining`: a fully-overspent budget reports `-1.0`
# rather than an unbounded negative, so a wildly-breaching actual stays interpretable.
_MIN_ERROR_BUDGET_REMAINING = -1.0


class SourceHealthInfo(TypedDict):
    """One source's reachability within a `describe_health` rollup.

    Carries `env` so an `env="all"` aggregate rollup (which unions per-env source
    health) keeps every entry's owning environment identifiable; for a single-env
    rollup it is simply that env.
    """

    env: str
    type: str
    reachable: bool
    detail: str


class HealthMetricInfo(TypedDict):
    """One key derived health metric in a `describe_health` rollup (F2g).

    Carries `env` so an `env="all"` aggregate keeps each metric's owning environment
    identifiable; `value` is the latest sample of that derived gauge for the env.
    """

    env: str
    name: str
    value: float


class HealthRollup(TypedDict):
    """The 'one thing to look at': per-source reachability + key derived metrics +
    open-incident count (spec § MCP server — `describe_health -> HealthRollup`)."""

    env: str
    sources: list[SourceHealthInfo]
    metrics: list[HealthMetricInfo]
    open_incident_count: int


class ClusterState(TypedDict):
    """A point-in-time snapshot of one env's kubernetes cluster, read from the STORE.

    Backs the `get_cluster_state` MCP tool (spec § Data Model — MCP return types). It is
    rendered from the stored `panoptes_k8s_*` gauges (the two-faces-one-store parity — the
    Grafana Kubernetes dashboard renders the SAME series), NOT from a live cluster call, so
    the tool needs no kubernetes client. `cluster` is the cluster name the gauges carry
    (distinguishing an observed cluster from Panoptes' own). `reachable` is `False` when no
    k8s gauge is present for the env (the cluster was never collected / is down) — never a
    silent-empty snapshot.
    """

    env: str
    cluster: str
    node_count: float
    pods_pending: float
    pods_crashloop: float
    pod_restarts_total: float
    reachable: bool


class CostBreakdown(TypedDict):
    """A cost snapshot for one env over a window, read from the STORE's cost gauges.

    Backs the `get_cost` MCP tool (spec § Cost types). Rendered from the stored
    `panoptes_cost_*` gauges (the two-faces-one-store parity — the Cost dashboard renders
    the SAME series), NOT a live Cost Explorer call. `total` is the sum of the per-service
    spend; `per_service` maps each service to its latest spend; `budget_burn` is the latest
    budget-burn fraction. No-data → zero total / empty map / 0.0 burn (never silent-empty).
    """

    env: str
    window: str
    total: float
    per_service: dict[str, float]
    budget_burn: float


class SloResult(TypedDict):
    """The result of evaluating one SLO for one env (spec § SLO + MCP return types).

    Backs the `get_slo` MCP tool + the SLO dashboard. `objective` is the configured target
    attainment (e.g. 0.99); `actual` is the measured attainment over the SLO's window;
    `met` is `actual >= objective`; `error_budget_remaining` is the fraction of the error
    budget still unspent (see `get_slo` for the exact formula): `1.0` = full budget, `0.0`
    = exactly at objective, negative = objective breached (overspent, clamped at `-1.0`).
    """

    name: str
    env: str
    objective: float
    actual: float
    met: bool
    error_budget_remaining: float


class EnvComparison(TypedDict):
    """One metric's series across every enabled env (spec § SLO + MCP return types).

    Backs the `compare_envs` MCP tool. `per_env` maps each ENABLED env to its resolved
    series for `metric` over `window`; `errors` carries a per-env marker for any env whose
    query could not be answered (a partial result — the answerable envs still appear in
    `per_env`, the down ones in `errors`, never a wholesale failure).
    """

    metric: str
    window: str
    per_env: dict[str, list[MetricSeries]]
    errors: dict[str, str]


class IncidentFanOutEntry(TypedDict):
    """One env's slice of an `env="all"` incident fan-out (data OR an error marker)."""

    env: str
    incidents: list[IncidentSignal]
    error: str | None


class IncidentFanOut(TypedDict):
    """The `env="all"` incident fan-out: a per-env partial result list."""

    results: list[IncidentFanOutEntry]


class LogFanOutEntry(TypedDict):
    """One env's slice of an `env="all"` log fan-out (data OR an error marker)."""

    env: str
    logs: list[LogSignal]
    error: str | None


class LogFanOut(TypedDict):
    """The `env="all"` log fan-out: a per-env partial result list."""

    results: list[LogFanOutEntry]


@dataclass(frozen=True)
class FanOutResult[ResultT]:
    """One env's slice of an `env="all"` fan-out: its data XOR an error marker.

    Generic over the per-env result type `ResultT` (PEP 695) so the iterate-and-mark
    contract is written once and reused by every env-aware tool. Exactly one of
    `data`/`error` is populated: a successful env carries `data` with `error=None`; an
    env whose fetch raised a `CapabilityError` carries `error` (its detail) with
    `data=None`. The tools project this into their own `IncidentFanOut`/`LogFanOut`
    TypedDicts (an unanswerable env's `None` data becomes the TypedDict's empty list).
    """

    env: str
    data: ResultT | None
    error: str | None


def fan_out_over_envs[ResultT](
    context: QueryContext, fetch_one: Callable[[ResolvedEnvironment], ResultT]
) -> list[FanOutResult[ResultT]]:
    """Run `fetch_one` for every enabled env, marking a per-env failure instead of failing.

    The single home for the `env="all"` fan-out contract (spec § MCP server contract):
    iterate every enabled env in declaration order, call `fetch_one(environment)`, and —
    when `fetch_one` raises ANY `PanoptesError` (the env cannot answer this query —
    whether a `CapabilityError` for a missing capability OR a bare `PanoptesError` for a
    configured-but-down live source, e.g. a Sentry 5xx) — capture an explicit per-env
    error marker rather than failing the whole call. The result is a partial result:
    answerable envs carry their data, unanswerable/down ones carry their error.

    F2: catching the `PanoptesError` BASE (not only the `CapabilityError` subclass) is
    deliberate — a live-source failure must mark just that env down, never wholesale-fail
    the multi-env call. A non-`PanoptesError` (a genuine bug) is intentionally NOT caught.

    Args:
        context: The query context (its enabled environments are iterated).
        fetch_one: The per-env fetch — given an environment, return its result. It may
            raise any `PanoptesError` to mark that env down without failing the fan-out.

    Returns:
        One `FanOutResult[ResultT]` per enabled env, each carrying data XOR an error.
    """
    results: list[FanOutResult[ResultT]] = []
    for environment in context.enabled_envs():
        try:
            results.append(
                FanOutResult(env=environment.name, data=fetch_one(environment), error=None)
            )
        except PanoptesError as exc:
            # The env cannot answer this query (missing capability) OR its source is down
            # (a live-source PanoptesError) — mark it down (partial result), do not let one
            # unanswerable/down env fail the whole fan-out. A CapabilityError carries a
            # `.detail`; a bare PanoptesError surfaces its message via `str(exc)`.
            detail = exc.detail if isinstance(exc, CapabilityError) else str(exc)
            results.append(FanOutResult(env=environment.name, data=None, error=detail))
    return results


def _fetch_incidents(
    context: QueryContext, environment: ResolvedEnvironment, window: TimeWindow
) -> list[IncidentSignal]:
    """Fetch + filter the env's incident signals, requiring an incident source.

    Raises:
        CapabilityError: no source in the env provides `INCIDENT` (capability
            negotiation — "no source for incidents", never a silent-empty list).
    """
    providers: list[Source] = context.sources_for(environment, SignalKind.INCIDENT)
    if not providers:
        raise CapabilityError(
            f"No source in environment '{environment.name}' provides incident signals; "
            f"cannot answer an incident query."
        )
    incidents: list[IncidentSignal] = []
    for source in providers:
        for signal in source.fetch(window):
            if isinstance(signal, IncidentSignal):
                incidents.append(signal)
    return incidents


def _fetch_logs(
    context: QueryContext, environment: ResolvedEnvironment, window: TimeWindow
) -> list[LogSignal]:
    """Fetch + filter the env's log signals, requiring a log source.

    Raises:
        CapabilityError: no source in the env provides `LOG`.
    """
    providers: list[Source] = context.sources_for(environment, SignalKind.LOG)
    if not providers:
        raise CapabilityError(
            f"No source in environment '{environment.name}' provides log signals; "
            f"cannot answer a log query."
        )
    logs: list[LogSignal] = []
    for source in providers:
        for signal in source.fetch(window):
            if isinstance(signal, LogSignal):
                logs.append(signal)
    return logs


def search_incidents(
    context: QueryContext,
    env: str,
    window: str,
    tag: str | None,
    level: str | None,
) -> list[IncidentSignal] | IncidentFanOut:
    """Search incident signals for `env` (or fan out across all enabled envs).

    Args:
        context: The query context (its envs / sources answer the query).
        env: A single environment name, or `"all"` to fan out.
        window: The query window string (v0.1: trailing default window).
        tag: Optional label-value filter (matched against any incident label value).
        level: Optional incident-level filter (matched against the incident level).

    Returns:
        For a single env: a `list[IncidentSignal]`. For `env="all"`: an
        `IncidentFanOut` with a per-env partial result (an unanswerable env carries
        an explicit error marker rather than failing the whole call).

    Raises:
        CapabilityError: a single-env query whose env provides no incident source.
    """
    time_window = _window_for(window)
    if env == _ALL_ENVS:
        # The generic helper owns the iterate-and-mark contract; this tool supplies only
        # its per-env fetch + filter and projects each result into its TypedDict entry.
        def _fetch_one(environment: ResolvedEnvironment) -> list[IncidentSignal]:
            return _filter_incidents(
                _fetch_incidents(context, environment, time_window), tag, level
            )

        entries = [
            IncidentFanOutEntry(
                env=result.env,
                incidents=result.data if result.data is not None else [],
                error=result.error,
            )
            for result in fan_out_over_envs(context, _fetch_one)
        ]
        return IncidentFanOut(results=entries)

    environment = context.require_env(env)
    return _filter_incidents(_fetch_incidents(context, environment, time_window), tag, level)


def _filter_incidents(
    incidents: list[IncidentSignal], tag: str | None, level: str | None
) -> list[IncidentSignal]:
    """Apply the optional `tag` (any label value) + `level` filters."""
    filtered = incidents
    if level is not None:
        filtered = [i for i in filtered if i.level.value == level]
    if tag is not None:
        filtered = [i for i in filtered if tag in i.labels.values()]
    return filtered


def search_logs(
    context: QueryContext,
    env: str,
    query: str,
    window: str,
    level: str | None,
) -> list[LogSignal] | LogFanOut:
    """Search log signals for `env` (or fan out across all enabled envs).

    Args:
        context: The query context (its envs / sources answer the query).
        env: A single environment name, or `"all"` to fan out.
        query: A substring filter matched against each log message.
        window: The query window string (v0.1: trailing default window).
        level: Optional log-level filter (matched against the log level).

    Returns:
        For a single env: a `list[LogSignal]`. For `env="all"`: a `LogFanOut` with a
        per-env partial result (an unanswerable env carries an explicit error marker).

    Raises:
        CapabilityError: a single-env query whose env provides no log source.
    """
    time_window = _window_for(window)
    if env == _ALL_ENVS:
        # Same generic fan-out, projected into the log TypedDict entry shape.
        def _fetch_one(environment: ResolvedEnvironment) -> list[LogSignal]:
            return _filter_logs(_fetch_logs(context, environment, time_window), query, level)

        entries = [
            LogFanOutEntry(
                env=result.env,
                logs=result.data if result.data is not None else [],
                error=result.error,
            )
            for result in fan_out_over_envs(context, _fetch_one)
        ]
        return LogFanOut(results=entries)

    environment = context.require_env(env)
    return _filter_logs(_fetch_logs(context, environment, time_window), query, level)


def _filter_logs(logs: list[LogSignal], query: str, level: str | None) -> list[LogSignal]:
    """Apply the substring `query` (message) + optional `level` filters."""
    filtered = [log for log in logs if query in log.message]
    if level is not None:
        filtered = [log for log in filtered if log.level.value == level]
    return filtered


def search_traces(context: QueryContext, env: str, window: str) -> list[object]:
    """Capability-negotiation surface for traces — always fails explicitly in v0.1.

    No v0.1 source provides TRACE (spec § Data Model), so this surfaces an explicit
    "no trace source" `CapabilityError` rather than returning an empty list (which
    would be indistinguishable from "no traces in window").

    Raises:
        CapabilityError: always — no configured source provides trace signals.
    """
    # Consult the per-env source capabilities exactly like the other tools, so the
    # negotiation is real (not a hardcoded raise): no source advertises TRACE.
    for environment in context.enabled_envs():
        trace_sources = context.sources_for(environment, SignalKind.TRACE)
        if trace_sources:
            # Defensive: if a future source ever adds TRACE, fetch from it instead of
            # falsely claiming none. v0.1 has none, so this branch is never taken.
            traces: list[object] = []
            for source in trace_sources:
                traces.extend(source.fetch(_window_for(window)))
            return traces
    raise CapabilityError(
        f"No configured source provides trace signals (requested env '{env}'); "
        f"no trace source is available in v0.1."
    )


def query_metric(
    context: QueryContext,
    env: str,
    name: str,
    window: str,
    filters: Mapping[str, str] | None,
) -> list[MetricSeries]:
    """Run a PromQL passthrough query for metric `name` against the store.

    This is the only tool that reads the store directly with caller-supplied query
    parameters, so a `passthrough`-store misconfiguration surfaces its
    `CapabilityError` here (structured, never crash / silent-empty).

    Args:
        context: The query context (its `store` answers the query).
        env: The environment to scope the query to (added as an `env=` label matcher),
            or `"all"` to query across EVERY env (the `env=` matcher is omitted so the
            selector returns series from all envs — metrics already carry an `env` label).
        name: The metric name to query.
        window: The query window string (v0.1: trailing default window).
        filters: Optional additional label matchers applied to the PromQL selector.

    Returns:
        The resolved `MetricSeries` list (possibly empty — empty is a legitimate
        "no data in window" answer, distinct from the passthrough `CapabilityError`).

    Raises:
        CapabilityError: the configured store cannot answer queries (e.g. passthrough),
            OR a caller-supplied `env`/`name`/filter-key fails validation (F7 — an unknown
            env, or a value carrying PromQL-breaking characters, is rejected explicitly
            rather than splicing it raw into the selector).
    """
    # F7 — validate every caller-controlled token that is spliced UNQUOTED into the
    # selector (env, metric name, filter label keys). A value that breaks out of the
    # selector (`"`/`{`/`}`/`\`) would otherwise read past the env filter (cross-env read)
    # or corrupt the query — a latent auth-bypass at v0.2's HTTP/SSO surface.
    if env != _ALL_ENVS and env not in context.env_names():
        available = ", ".join(context.env_names()) or "(none)"
        raise CapabilityError(
            f"Unknown environment '{env}' for query_metric. Available environments: "
            f"{available} (or 'all' to query across every env)."
        )
    if not _PROMQL_IDENTIFIER_RE.match(name):
        raise CapabilityError(
            f"Invalid metric name '{name}': a metric name must be a PromQL identifier "
            f"([A-Za-z_][A-Za-z0-9_:]*)."
        )

    selectors: list[str] = []
    # `env="all"` is the across-env query: omit the `env=` matcher entirely (F1). Pinning
    # `env="all"` would select a literal label value no signal carries → silent-empty,
    # which the spec forbids. The metrics already carry their own `env` label, so a
    # matcher-free selector returns series across every env. (`env` is a validated env
    # name here, so it needs no value escaping.)
    if env != _ALL_ENVS:
        selectors.append(f'env="{env}"')
    if filters:
        for key, value in sorted(filters.items()):
            if not _PROMQL_IDENTIFIER_RE.match(key):
                raise CapabilityError(
                    f"Invalid filter label key '{key}': a label key must be a PromQL "
                    f"identifier ([A-Za-z_][A-Za-z0-9_:]*)."
                )
            # The label VALUE is interpolated inside a double-quoted PromQL string, so it
            # is ESCAPED (not identifier-validated): backslash FIRST (so the quote-escape's
            # own backslash is not doubled), then the double quote.
            selectors.append(f'{key}="{escape_promql_value(value)}"')
    expr = f"{name}{{{','.join(selectors)}}}"
    # F2f — the step is now strictly sub-window (was `step == window`, a single degenerate
    # bucket), so a range query returns multiple points over the requested window.
    metric_query = MetricQuery(
        expr=expr, window=_window_for(window), step_seconds=_step_seconds_for(window)
    )
    # A passthrough store raises CapabilityError here — it propagates as the
    # structured MCP error the read-only contract requires (never swallowed).
    return context.store.query(metric_query)


def get_cluster_state(context: QueryContext, env: str) -> ClusterState:
    """Render `env`'s kubernetes cluster snapshot from the stored `panoptes_k8s_*` gauges.

    This reads the STORE (two-faces-one-store parity — the Grafana Kubernetes dashboard
    renders the same series), NOT a live cluster call, so the tool needs no kubernetes
    client. The cluster-wide gauges (node count / pending / crashloop) contribute their
    latest single value; `pod_restarts_total` is summed across its per-namespace series.

    Reachability is explicit: when NO k8s gauge is present for the env (the cluster was
    never collected, or its source is down so nothing reached the store), `reachable` is
    `False` and the counts default to `0.0` — a clear "nothing observed" snapshot, never a
    silent-empty result the caller could mistake for a healthy empty cluster. A passthrough
    store (which cannot answer PromQL at all) is likewise surfaced as unreachable rather
    than crashing the tool.

    Args:
        context: The query context (its `store` answers the gauge queries).
        env: The environment whose cluster snapshot to render.

    Returns:
        A `ClusterState` with the four derived metrics + the `cluster` name + `reachable`.
    """
    # Validate the env up front (same discipline as query_metric) so an unknown env fails
    # with a clear error rather than silently returning an unreachable snapshot.
    environment = context.require_env(env)

    # The three cluster-wide gauges are single-value scalars → `read_gauge` (which owns the
    # F7 escape, swallows a passthrough store's CapabilityError to None, and folds to the
    # latest value). `None` means "no data for this gauge".
    node_count = context.read_gauge(_K8S_NODE_COUNT, environment.name)
    pods_pending = context.read_gauge(_K8S_PODS_PENDING, environment.name)
    pods_crashloop = context.read_gauge(_K8S_PODS_CRASHLOOP, environment.name)
    # `pod_restarts_total` is PER-NAMESPACE — the per-namespace sum (+ the cluster label) need
    # the full series, so read it via `read_series`. `read_series` PROPAGATES a CapabilityError,
    # but this tool must stay answerable on a passthrough store, so swallow it locally (the same
    # "report unreachable, never crash" contract the cluster-wide gauges get for free).
    try:
        restarts_series = context.read_series(_K8S_POD_RESTARTS_TOTAL, environment.name)
    except CapabilityError:
        restarts_series = []

    # The cluster is reachable in the store's eyes iff ANY k8s gauge produced data for the env:
    # a non-None cluster-wide gauge value, OR a non-empty per-namespace restart series. A
    # passthrough store yields None for the three (swallowed) + an empty restarts list, so it
    # reads as unreachable — never a crash.
    reachable = (
        node_count is not None
        or pods_pending is not None
        or pods_crashloop is not None
        or bool(restarts_series)
    )

    return ClusterState(
        env=environment.name,
        # The cluster name is carried on every k8s gauge's labels; read it from the per-
        # namespace restart series (empty string when nothing was collected).
        cluster=_cluster_label(restarts_series),
        node_count=node_count or 0.0,
        pods_pending=pods_pending or 0.0,
        pods_crashloop=pods_crashloop or 0.0,
        # pod_restarts_total is per-namespace: sum the latest value of EACH namespace series
        # so the snapshot reports the cluster-wide restart total.
        pod_restarts_total=_sum_latest_per_series(restarts_series),
        reachable=reachable,
    )


def _cluster_label(series: list[MetricSeries]) -> str:
    """The `cluster` label value carried on the k8s gauges, or empty when none present."""
    for one_series in series:
        cluster = one_series.labels.get("cluster")
        if cluster:
            return cluster
    return ""


def _sum_latest_per_series(series: list[MetricSeries]) -> float:
    """Sum the latest point value across EACH series (the per-namespace restart total).

    `pod_restarts_total` emits one series per namespace, so the cluster-wide total is the
    sum of each namespace series' latest value (not a single `_latest_value` across all,
    which would pick only one namespace's number).
    """
    total = 0.0
    for one_series in series:
        if one_series.points:
            # The latest point of this namespace's series (points are time-ordered by the
            # store; take the max-timestamp value to be order-independent).
            latest = max(one_series.points, key=lambda point: point[0])
            total += latest[1]
    return total


def get_cost(context: QueryContext, env: str, window: str) -> CostBreakdown:
    """Render `env`'s cost snapshot over `window` from the stored `panoptes_cost_*` gauges.

    This reads the STORE (two-faces-one-store parity — the Cost dashboard renders the same
    series), NOT a live Cost Explorer call, so the tool needs no CE/budgets client. The
    per-service spend gauges (`panoptes_cost_spend{env, service}`) contribute their latest
    value per service; `total` is the sum across services; `budget_burn` is the latest
    `panoptes_cost_budget_burn{env}` gauge.

    No cost data (the env was never collected, or the store is a passthrough that cannot
    answer PromQL) yields a zero/empty breakdown — never a crash or a silent-empty result a
    caller could mistake for "$0 spend".

    Args:
        context: The query context (its `store` answers the cost-gauge queries).
        env: The environment whose cost snapshot to render.
        window: The cost window string (echoed back; the gauges are the latest collected).

    Returns:
        A `CostBreakdown` with `total`, `per_service`, and `budget_burn`.

    Raises:
        CapabilityError: the env is unknown/disabled.
    """
    # Validate the env up front (same discipline as get_cluster_state) so an unknown env
    # fails with a clear error rather than silently returning a zero snapshot.
    environment = context.require_env(env)

    # COST ASYMMETRY (load-bearing — do NOT regress): the per-service SPEND needs the FULL
    # series (one per `{env, service}`) to build the per-service map, so it reads via
    # `read_series`; the BUDGET BURN is a single env-scoped scalar, so it reads via
    # `read_gauge`. `read_series` propagates a CapabilityError, but `get_cost` must report
    # zeros (never crash) on a passthrough store, so swallow it locally for the spend read —
    # `read_gauge` already swallows it to None for the burn.
    try:
        spend_series = context.read_series(_COST_SPEND, environment.name)
    except CapabilityError:
        spend_series = []
    budget_burn = context.read_gauge(_COST_BUDGET_BURN, environment.name)

    per_service, total = _per_service_spend(spend_series)
    return CostBreakdown(
        env=environment.name,
        window=window,
        total=total,
        per_service=per_service,
        budget_burn=budget_burn or 0.0,
    )


def _per_service_spend(series: list[MetricSeries]) -> tuple[dict[str, float], float]:
    """Build the per-service spend map + the total from the spend gauges.

    Each series is one `{env, service}` spend gauge; the map keys on the `service` label
    (a series missing it contributes to the total but cannot key the map). The total is the
    sum of every series' latest value.
    """
    per_service: dict[str, float] = {}
    total = 0.0
    for one_series in series:
        latest = _latest_value([one_series])
        if latest is None:
            continue
        total += latest
        service = one_series.labels.get("service")
        if service:
            per_service[service] = latest
    return per_service, total


def get_slo(context: QueryContext, env: str, name: str) -> SloResult:
    """Evaluate the named SLO for `env` against the store (spec § SLO + MCP return types).

    Looks up the `SloConfig` by `name` (failing clearly if unknown), runs its `query`
    (the optional v0.2 field — falling back to the `panoptes_health_up` availability gauge
    when absent) over its `window` (default trailing window when absent) scoped to `env`,
    takes the latest value as the `actual` attainment, and computes `met` + the error
    budget.

    **Error-budget formula (pinned).** For an availability-style objective `o` (e.g. 0.99)
    and a measured actual `a`:

        error_budget_remaining = (a - o) / (1 - o)

    This is the fraction of the error budget still UNSPENT: the budget is the allowance
    below 100% (`1 - o`); the consumed portion is `1 - a`; so the remaining fraction is
    `((1 - o) - (1 - a)) / (1 - o) = (a - o) / (1 - o)`. It reads `1.0` at a perfect
    `a == 1.0`, `0.0` exactly at the objective (`a == o`), and negative when the objective
    is breached — clamped at `-1.0` (a fully-overspent budget) so a wildly-low actual stays
    interpretable, and capped at `1.0` above. A degenerate objective of `o >= 1.0` (a 100%
    target with a zero-width budget) reports `1.0` when `a >= o` else `-1.0` (no division).

    Args:
        context: The query context (its `store` answers the SLO query; its `slos` carry
            the SLO definitions).
        env: The environment to evaluate the SLO for (validated via `require_env`).
        name: The SLO name to look up.

    Returns:
        A `SloResult` with the objective, the measured actual, `met`, and the remaining
        error budget.

    Raises:
        CapabilityError: the env is unknown/disabled, OR no SLO is named `name`.
    """
    # Validate the env (an unknown/disabled env is a clear CapabilityError, not silent).
    environment = context.require_env(env)
    slo = _find_slo(context, name)

    objective = float(slo.get("objective", 0.0))
    query_expr = slo.get("query") or _DEFAULT_SLO_QUERY
    window_str = slo.get("window") or ""
    actual = _slo_actual(context, query_expr, window_str, environment.name)

    met = actual >= objective
    return SloResult(
        name=name,
        env=environment.name,
        objective=objective,
        actual=actual,
        met=met,
        error_budget_remaining=_error_budget_remaining(objective, actual),
    )


def _find_slo(context: QueryContext, name: str) -> SloConfig:
    """Return the SLO config named `name`, raising a clear `CapabilityError` if unknown."""
    for slo in context.slos:
        if slo.get("name") == name:
            return slo
    available = ", ".join(str(slo.get("name")) for slo in context.slos) or "(none)"
    raise CapabilityError(f"No SLO named '{name}'. Available SLOs: {available}.")


def _slo_actual(context: QueryContext, query_expr: str, window_str: str, env: str) -> float:
    """Run the SLO's query scoped to `env` and return the latest value (0.0 when no data).

    The configured `query` is the metric selector; `read_gauge` wraps it with the `env=`
    matcher (owning the F7 escape), runs it over `window_str`, swallows a passthrough store's
    `CapabilityError` to None, and folds to the latest value. A missing measurement (None) is
    coerced to `0.0` — the worst-case attainment — never a crash into the MCP surface.
    """
    return context.read_gauge(query_expr, env, window_str) or 0.0


def _error_budget_remaining(objective: float, actual: float) -> float:
    """Compute the fraction of the error budget still unspent (see `get_slo` for the math).

    `(actual - objective) / (1 - objective)`, clamped to `[-1.0, 1.0]`. A degenerate
    objective `>= 1.0` (zero-width budget) cannot divide, so it reports `1.0` when the
    actual meets it, else the `-1.0` floor.
    """
    budget = 1.0 - objective
    if budget <= 0.0:
        # A 100%-or-impossible objective: no budget to spend. Met → full (1.0), else floor.
        return 1.0 if actual >= objective else _MIN_ERROR_BUDGET_REMAINING
    remaining = (actual - objective) / budget
    # Clamp: never more than a full budget (1.0); never below the overspent floor (-1.0).
    return max(_MIN_ERROR_BUDGET_REMAINING, min(1.0, remaining))


def compare_envs(context: QueryContext, metric: str, window: str) -> EnvComparison:
    """Compare one `metric` across every ENABLED env (spec § promoted tools).

    Reuses the `env="all"` fan-out (`fan_out_over_envs`): queries `metric` over `window`
    scoped to each enabled env, collecting that env's series under `per_env`. An env whose
    query cannot be answered (a `CapabilityError`/`PanoptesError` — a passthrough store or a
    per-env outage) is recorded in `errors` instead — a partial result, never a wholesale
    failure (the answerable envs still appear in `per_env`).

    Args:
        context: The query context (its enabled envs + `store` answer the query).
        metric: The metric name to compare (a PromQL identifier).
        window: The trailing window string (e.g. "1h", "24h").

    Returns:
        An `EnvComparison` mapping each enabled env to its series (`per_env`) + a per-env
        error map (`errors`) for any env that could not answer.

    Raises:
        CapabilityError: the `metric` is not a valid PromQL identifier (rejected before any
            query, so a breakout token never reaches the store — the F7 discipline).
    """
    # F7 — validate the metric name before splicing it into the selector (a `"`/`{`/`}`/`\`
    # token would otherwise break out of the per-env selector).
    if not _PROMQL_IDENTIFIER_RE.match(metric):
        raise CapabilityError(
            f"Invalid metric name '{metric}': a metric name must be a PromQL identifier "
            f"([A-Za-z_][A-Za-z0-9_:]*)."
        )

    def _fetch_one(environment: ResolvedEnvironment) -> list[MetricSeries]:
        # `read_series` scopes the metric to this env (owning the F7 escape) and PROPAGATES a
        # CapabilityError for a per-env outage — which the fan-out catches + marks per-env (the
        # propagate-not-swallow contract `read_series` provides exactly for this site).
        return context.read_series(metric, environment.name, window)

    per_env: dict[str, list[MetricSeries]] = {}
    errors: dict[str, str] = {}
    for result in fan_out_over_envs(context, _fetch_one):
        if result.error is not None:
            errors[result.env] = result.error
        else:
            # A successful env carries its (possibly empty) series list.
            per_env[result.env] = result.data if result.data is not None else []
    return EnvComparison(metric=metric, window=window, per_env=per_env, errors=errors)


def describe_health(context: QueryContext, env: str) -> HealthRollup:
    """Roll up per-source reachability + open-incident count for `env` (or all envs).

    Every configured source is INCLUDED in the rollup with its reachability — an
    unreachable source is marked `reachable: False`, never omitted, so "the one
    thing to look at" actually shows what is down (the tool's core promise).

    When `env == "all"` (F1), the rollup AGGREGATES across every enabled env: the
    `sources` list is the union of per-env source-health entries (each carrying its
    owning env), and `open_incident_count` is the sum across envs. This replaces the
    previous misleading fall-through to `require_env("all")` (an "unknown env" error).

    Args:
        context: The query context.
        env: The environment to roll up, or `"all"` to aggregate across enabled envs.

    Returns:
        A `HealthRollup` with per-source health + the open-incident count (0 when no
        source provides incidents — health is still answerable from reachability). For
        `env="all"`, the env field is `"all"` and the rollup is the across-env aggregate.
    """
    if env == _ALL_ENVS:
        # Aggregate the union of per-env source health + metrics + the sum of incidents.
        aggregated_sources: list[SourceHealthInfo] = []
        aggregated_metrics: list[HealthMetricInfo] = []
        aggregated_count = 0
        for environment in context.enabled_envs():
            env_sources, env_metrics, env_count = _health_for_env(context, environment)
            aggregated_sources.extend(env_sources)
            aggregated_metrics.extend(env_metrics)
            aggregated_count += env_count
        return HealthRollup(
            env=_ALL_ENVS,
            sources=aggregated_sources,
            metrics=aggregated_metrics,
            open_incident_count=aggregated_count,
        )

    environment = context.require_env(env)
    sources, metrics, open_incident_count = _health_for_env(context, environment)
    return HealthRollup(
        env=env, sources=sources, metrics=metrics, open_incident_count=open_incident_count
    )


def _health_for_env(
    context: QueryContext, environment: ResolvedEnvironment
) -> tuple[list[SourceHealthInfo], list[HealthMetricInfo], int]:
    """Probe one env's per-source reachability + key derived metrics + open-incident count.

    Extracted so a single-env rollup and the `env="all"` aggregate share one
    implementation. Each `SourceHealthInfo`/`HealthMetricInfo` carries the env so an
    aggregate rollup keeps every entry's owning environment identifiable.
    """
    sources: list[SourceHealthInfo] = []
    for resolved in environment.sources:
        health = resolved.source.health()
        sources.append(
            SourceHealthInfo(
                env=environment.name,
                type=resolved.source.type,
                reachable=health.reachable,
                detail=health.detail,
            )
        )

    metrics = _health_metrics_for_env(context, environment)

    # Open incidents are a best-effort enrichment: if the env has an incident source,
    # count its incidents; if not, health is still answerable (count stays 0) — we do
    # NOT raise here, because reachability is the rollup's mandatory promise.
    open_incident_count = 0
    if context.sources_for(environment, SignalKind.INCIDENT):
        open_incident_count = len(
            _fetch_incidents(context, environment, TimeWindow.last(minutes=_DEFAULT_WINDOW_MINUTES))
        )
    return sources, metrics, open_incident_count


def _health_metrics_for_env(
    context: QueryContext, environment: ResolvedEnvironment
) -> list[HealthMetricInfo]:
    """Read the key derived health metrics for one env from the store (F2g).

    Queries each `_KEY_HEALTH_METRICS` gauge (`panoptes_health_up`, and latency when
    present) scoped to this env and surfaces the LATEST sample value — the overview pack's
    traffic-light backing. This is best-effort enrichment: a metric the store has no data
    for (or a store that returns nothing) simply does not appear; a passthrough store that
    raises `CapabilityError` is swallowed here so health stays answerable from
    reachability alone (the rollup's mandatory promise must not depend on a metric store).
    """
    metrics: list[HealthMetricInfo] = []
    for metric_name in _KEY_HEALTH_METRICS:
        # `read_series` scopes each metric to this env (owning the F7 escape). It PROPAGATES a
        # CapabilityError, but this enrichment is best-effort — a passthrough store that cannot
        # answer must leave health answerable from reachability alone — so swallow it here and
        # return whatever metrics were collected so far (the prior stop-the-loop semantic).
        try:
            series = context.read_series(metric_name, environment.name)
        except CapabilityError:
            return metrics
        latest = _latest_value(series)
        if latest is not None:
            metrics.append(HealthMetricInfo(env=environment.name, name=metric_name, value=latest))
    return metrics
