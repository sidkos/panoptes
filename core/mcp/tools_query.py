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
from datetime import datetime
from typing import TypedDict

from core.config import ResolvedEnvironment
from core.errors import CapabilityError, PanoptesError
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

# Default query window for the live-source fetch tools. The MCP `window` argument is
# a human string (e.g. "15m"); an empty/None/unrecognized window resolves to this
# trailing default so a tool always has a bounded fetch.
_DEFAULT_WINDOW_MINUTES = 15

# The known human window strings → trailing minutes (F2f). The previous `_window_for`
# hard-returned 15m for EVERY input, so `window="24h"` silently gave 15m. These cover the
# common cadences; a bare integer is interpreted as minutes; anything else falls back to
# the default WITH the value surfaced (explicit, never silent).
_WINDOW_STRING_TO_MINUTES: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "6h": 360,
    "24h": 1440,
    "1d": 1440,
    "7d": 10080,
}

# The step floor (seconds) for a range query (F2f). The step is computed as
# window_seconds / _STEP_TARGET_BUCKETS so a range yields multiple points (never one
# degenerate bucket), then clamped to at least this floor so a tiny window cannot ask the
# store for an absurdly fine grid.
_STEP_TARGET_BUCKETS = 60
_MIN_STEP_SECONDS = 15

# The key derived health metrics `describe_health` surfaces (F2g — spec § MCP server:
# `describe_health -> HealthRollup` = per-source reachability + "key derived metrics" +
# open incident count). `panoptes_health_up` is the overview pack's traffic-light backing;
# latency is included when the store has it. A metric absent from the store simply does
# not appear (best-effort enrichment — reachability remains the rollup's mandatory promise).
_KEY_HEALTH_METRICS = ("panoptes_health_up", "panoptes_health_latency_ms")


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


def escape_promql_value(value: str) -> str:
    """Escape a value for a double-quoted PromQL label-matcher string (F7 / F2d).

    Backslash is escaped FIRST so the quote-escape's own backslash is not re-doubled, then
    the double quote. This keeps a value like `a"b` a single closed string (`"a\\"b"`)
    instead of breaking out of the selector (cross-env read / corrupted query).

    This is the canonical PromQL value-escape primitive (F2d): it is PUBLIC so every
    caller that interpolates a value into a quoted PromQL string — `query_metric` here,
    `get_dashboard_data`'s `$env` substitution, and the demo consumer pack — reuses ONE
    implementation rather than hand-copying the two `.replace(...)` calls (a copy that can
    drift and miss the backslash-first ordering).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _window_minutes(window: str) -> int:
    """Parse the MCP `window` string into trailing minutes (F2f).

    Recognizes the common cadence strings (`"15m"`, `"30m"`, `"1h"`, `"6h"`, `"24h"`/
    `"1d"`, `"7d"`) and a bare integer (interpreted as minutes). An empty/None window
    resolves to the default. An UNRECOGNIZED window is handled EXPLICITLY: it falls back
    to the default AND logs a warning surfacing the offending value — never the old
    silent-15m-for-everything behavior that made `window="24h"` quietly mean 15m.
    """
    normalized = window.strip().lower() if window else ""
    if not normalized:
        return _DEFAULT_WINDOW_MINUTES
    known = _WINDOW_STRING_TO_MINUTES.get(normalized)
    if known is not None:
        return known
    # A bare integer is accepted as minutes (forward-compatible with a fuller parser).
    if normalized.isdigit():
        parsed = int(normalized)
        if parsed > 0:
            return parsed
    # Unrecognized — fall back to the default, but surface the value so the operator
    # knows their window was not honored (explicit, not silent).
    _LOGGER.warning(
        f"Unrecognized MCP window {window!r}; falling back to the default "
        f"{_DEFAULT_WINDOW_MINUTES}-minute window."
    )
    return _DEFAULT_WINDOW_MINUTES


def _window_for(window: str) -> TimeWindow:
    """Resolve the MCP `window` string to a trailing `TimeWindow` (F2f).

    Delegates to `_window_minutes` (which now genuinely PARSES the window rather than
    ignoring it) and returns a trailing window of that many minutes ending 'now'.
    """
    return TimeWindow.last(minutes=_window_minutes(window))


def _step_seconds_for(window: str) -> int:
    """Compute a sane range step (seconds) that is strictly sub-window (F2f).

    The old `query_metric` used `step == window`, producing a single degenerate bucket.
    This divides the window into `_STEP_TARGET_BUCKETS` points, then clamps the step to a
    `_MIN_STEP_SECONDS` floor so a small window cannot request an absurdly fine grid. The
    result is always strictly less than the window span, so a range yields multiple points.
    """
    window_seconds = _window_minutes(window) * 60
    step = window_seconds // _STEP_TARGET_BUCKETS
    return max(_MIN_STEP_SECONDS, step)


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
    window = TimeWindow.last(minutes=_DEFAULT_WINDOW_MINUTES)
    for metric_name in _KEY_HEALTH_METRICS:
        # The env name is a declared, validated env (require_env already ran for the
        # single-env path; enabled_envs for the aggregate) — but escape defensively so a
        # future label-bearing env can never corrupt the selector (F7 discipline).
        expr = f'{metric_name}{{env="{escape_promql_value(environment.name)}"}}'
        try:
            series = context.store.query(
                MetricQuery(expr=expr, window=window, step_seconds=_step_seconds_for("15m"))
            )
        except CapabilityError:
            # A non-metric (passthrough) store cannot answer — health is still answerable
            # from reachability, so drop the metric enrichment rather than failing.
            return metrics
        latest = _latest_value(series)
        if latest is not None:
            metrics.append(HealthMetricInfo(env=environment.name, name=metric_name, value=latest))
    return metrics


def _latest_value(series: list[MetricSeries]) -> float | None:
    """The most recent sample value across a metric's series, or None if there is none.

    `describe_health` surfaces a single scalar per key metric, so this picks the latest
    point across the returned series (each series is one label-set; for a single-env
    `env`-scoped health gauge there is typically one). Returns None when there is no data,
    so the caller omits the metric rather than inventing a 0.0.
    """
    latest_timestamp: datetime | None = None
    latest_value: float | None = None
    for one_series in series:
        for timestamp, value in one_series.points:
            if latest_timestamp is None or timestamp >= latest_timestamp:
                latest_timestamp = timestamp
                latest_value = value
    return latest_value
