"""Phase 6 unit tests for the MCP query tools + server wiring.

Covers (spec `## Tests` → MCP bullet / playbook Phase 6 table):
- `search_incidents` / `search_logs` aggregate over fake sources (filtering by the
  signal kind each tool needs); `describe_health` rolls up per-source reachability.
- **`describe_health` surfaces an unreachable source** — a source whose `health()`
  returns `reachable=False` appears in the rollup **marked unreachable** (not
  omitted, not silent-empty).
- **`query_metric` against a `passthrough` store** surfaces the store's
  `CapabilityError` as a **structured MCP error** (never a crash / silent-empty);
  the `ResolvedConfig` is built with the real `PassthroughStore` directly.
- **Capability negotiation** — asking for TRACE → an explicit "no trace source"
  `CapabilityError`, never an empty list (no v0.1 source provides TRACE).
- **`env="all"` fan-out** including the **one-env-down → per-env partial result
  with an explicit per-env error marker** case (the call does not wholesale-fail).
- A **v0.2 stub tool** (`compare_envs` / `get_slo`) listed in config returns an
  explicit "not available in v0.1 (ships v0.2)" error **at call time** (NOT a
  config-resolve failure).
- **STRUCTURAL read-only assertion** — with the default v0.1 config (no v0.2
  stubs) the registered tool set is EXACTLY the known read-only set AND no
  registered tool NAME matches the mutation-verb regex, so a future write tool
  under ANY name fails.

All tests are synchronous and deterministic — the query functions take an explicit
context and the server exposes its registered tool names + raw callables for sync
introspection, so no FastMCP async transport is driven here.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core.config import (
    McpConfig,
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
)
from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.mcp.server import (
    KNOWN_READ_ONLY_TOOLS,
    PanoptesMcpServer,
    build_server,
)
from core.mcp.tools_query import (
    describe_health,
    escape_promql_value,
    query_metric,
    search_incidents,
    search_logs,
)
from core.model import (
    Alert,
    CanonicalSignal,
    IncidentLevel,
    IncidentSignal,
    LogLevel,
    LogSignal,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier
from core.planes.store import Store
from core.stores.passthrough import PassthroughStore

# The mutation-verb regex the structural read-only assertion enforces against every
# registered tool NAME — a write tool under ANY of these prefixes must fail the test.
_MUTATION_VERB_RE = re.compile(r"\b(create_|save_|update_|delete_|set_|post_|ack_|silence_)")


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    """A typed fake `Source` returning fixed signals + a configurable health."""

    def __init__(
        self,
        source_type: str,
        capabilities: set[SignalKind],
        *,
        signals: list[CanonicalSignal] | None = None,
        reachable: bool = True,
        detail: str = "ok",
    ) -> None:
        self.type = source_type
        self._capabilities = capabilities
        self._signals = signals if signals is not None else []
        self._reachable = reachable
        self._detail = detail

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return self._signals

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=self._reachable, detail=self._detail, checked_at=_now())


class _FakeStore:
    type = "fake"

    def __init__(self, series: list[MetricSeries] | None = None) -> None:
        self._series = series if series is not None else []

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return self._series


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _incident(env: str, level: IncidentLevel = IncidentLevel.ERROR) -> IncidentSignal:
    return IncidentSignal(
        id="i-1",
        title="boom",
        level=level,
        first_seen=_now(),
        last_seen=_now(),
        count=3,
        labels={"env": env, "level": level.value, "project": "p"},
    )


def _log(env: str, level: LogLevel = LogLevel.ERROR) -> LogSignal:
    return LogSignal(
        timestamp=_now(),
        message="error happened",
        level=level,
        labels={"env": env},
    )


def _resolved_source(
    source_type: str,
    capabilities: set[SignalKind],
    *,
    signals: list[CanonicalSignal] | None = None,
    reachable: bool = True,
    detail: str = "ok",
) -> ResolvedSource:
    return ResolvedSource(
        source=_FakeSource(
            source_type, capabilities, signals=signals, reachable=reachable, detail=detail
        ),
        fetch_timeout_seconds=30,
        poll_interval_seconds=60,
    )


def _config(
    environments: dict[str, ResolvedEnvironment],
    *,
    store: Store | None = None,
    mcp: McpConfig | None = None,
) -> ResolvedConfig:
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments=environments,
        store=store if store is not None else _FakeStore(),
        notifiers=notifiers,
        dashboard_packs=[],
        slos=[],
        mcp=mcp if mcp is not None else {},
    )


def _dev_only_config(
    *,
    store: Store | None = None,
    mcp: McpConfig | None = None,
    sentry_signals: list[CanonicalSignal] | None = None,
    cloudwatch_signals: list[CanonicalSignal] | None = None,
    cloudwatch_reachable: bool = True,
) -> ResolvedConfig:
    return _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved_source(
                        "cloudwatch",
                        {SignalKind.METRIC, SignalKind.LOG},
                        signals=cloudwatch_signals,
                        reachable=cloudwatch_reachable,
                        detail="ok" if cloudwatch_reachable else "connection refused",
                    ),
                    _resolved_source(
                        "sentry",
                        {SignalKind.INCIDENT, SignalKind.METRIC},
                        signals=sentry_signals,
                    ),
                    _resolved_source("http-health", {SignalKind.METRIC}),
                ],
            ),
        },
        store=store,
        mcp=mcp,
    )


# --- search_incidents / search_logs ----------------------------------------------


def test_search_incidents_aggregates_incident_signals() -> None:
    config = _dev_only_config(sentry_signals=[_incident("dev")])
    incidents = search_incidents(
        QueryContext(config), env="dev", window="15m", tag=None, level=None
    )
    assert isinstance(incidents, list)  # single-env returns a flat list, not a fan-out
    assert len(incidents) == 1
    assert incidents[0].title == "boom"


def test_search_incidents_no_source_for_kind_raises_capability_error() -> None:
    """An env with only metric sources can answer no incident query — fail explicit."""
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[_resolved_source("http-health", {SignalKind.METRIC})],
            )
        }
    )
    with pytest.raises(CapabilityError):
        search_incidents(QueryContext(config), env="dev", window="15m", tag=None, level=None)


def test_search_logs_aggregates_log_signals() -> None:
    config = _dev_only_config(cloudwatch_signals=[_log("dev")])
    logs = search_logs(QueryContext(config), env="dev", query="error", window="15m", level=None)
    assert isinstance(logs, list)  # single-env returns a flat list, not a fan-out
    assert len(logs) == 1
    assert logs[0].message == "error happened"


# --- describe_health -------------------------------------------------------------


def test_describe_health_marks_unreachable_source() -> None:
    config = _dev_only_config(cloudwatch_reachable=False)
    rollup = describe_health(QueryContext(config), env="dev")
    by_type = {s["type"]: s for s in rollup["sources"]}
    assert by_type["cloudwatch"]["reachable"] is False
    assert "connection refused" in by_type["cloudwatch"]["detail"]
    # The unreachable source is still present in the rollup, not omitted.
    assert set(by_type) == {"cloudwatch", "sentry", "http-health"}


def test_describe_health_surfaces_key_derived_metrics_value() -> None:
    """`describe_health` surfaces the spec's "key derived metrics" with their VALUE (F2g).

    The spec § MCP server defines `describe_health -> HealthRollup` as per-source
    reachability + key derived metrics + open incident count. The rollup must carry a
    `metrics` field reflecting `panoptes_health_up` read from the store — asserting the
    VALUE, not just key presence.
    """

    class _HealthMetricStore:
        type = "health-metric"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            # Answer the health-up probe with a 1.0 latest sample.
            if "panoptes_health_up" in query.expr:
                return [
                    MetricSeries(
                        metric="panoptes_health_up",
                        labels={"env": "dev"},
                        points=[(_now(), 1.0)],
                    )
                ]
            return []

    config = _dev_only_config(store=_HealthMetricStore())
    rollup = describe_health(QueryContext(config), env="dev")

    metrics_by_name = {m["name"]: m["value"] for m in rollup["metrics"]}
    assert metrics_by_name.get("panoptes_health_up") == 1.0


def test_describe_health_env_all_includes_metrics_field() -> None:
    """The `env="all"` aggregate rollup still carries a coherent `metrics` field (F2g)."""

    class _HealthMetricStore:
        type = "health-metric"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            if "panoptes_health_up" in query.expr:
                return [
                    MetricSeries(
                        metric="panoptes_health_up", labels={"env": "x"}, points=[(_now(), 1.0)]
                    )
                ]
            return []

    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[_resolved_source("http-health", {SignalKind.METRIC})],
            ),
        },
        store=_HealthMetricStore(),
    )
    rollup = describe_health(QueryContext(config), env="all")
    assert rollup["env"] == "all"
    # The metrics field is present and a list (coherent for the aggregate path).
    assert isinstance(rollup["metrics"], list)


def test_describe_health_counts_open_incidents() -> None:
    config = _dev_only_config(sentry_signals=[_incident("dev"), _incident("dev")])
    rollup = describe_health(QueryContext(config), env="dev")
    assert rollup["open_incident_count"] == 2


# --- query_metric ----------------------------------------------------------------


def test_query_metric_returns_series_from_store() -> None:
    series = [
        MetricSeries(metric="panoptes_health_up", labels={"env": "dev"}, points=[(_now(), 1.0)])
    ]
    config = _dev_only_config(store=_FakeStore(series))
    result = query_metric(
        QueryContext(config), env="dev", name="panoptes_health_up", window="15m", filters=None
    )
    assert result[0].metric == "panoptes_health_up"


def test_query_metric_passthrough_store_surfaces_capability_error() -> None:
    """A source-only (passthrough) misconfiguration fails explicitly, not silent."""
    passthrough = PassthroughStore({})
    config = _dev_only_config(store=passthrough)
    with pytest.raises(CapabilityError):
        query_metric(
            QueryContext(config), env="dev", name="panoptes_health_up", window="15m", filters=None
        )


# --- capability negotiation (TRACE) ----------------------------------------------


def test_no_trace_source_capability_negotiation() -> None:
    """No v0.1 source provides TRACE → an explicit 'no trace source' CapabilityError."""
    from core.mcp.tools_query import search_traces

    config = _dev_only_config()
    with pytest.raises(CapabilityError) as excinfo:
        search_traces(QueryContext(config), env="dev", window="15m")
    assert "trace" in str(excinfo.value).lower()


# --- env="all" fan-out -----------------------------------------------------------


def test_env_all_fan_out_aggregates_per_env() -> None:
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved_source("sentry", {SignalKind.INCIDENT}, signals=[_incident("dev")])
                ],
            ),
            "stage": ResolvedEnvironment(
                name="stage",
                enabled=True,
                sources=[
                    _resolved_source("sentry", {SignalKind.INCIDENT}, signals=[_incident("stage")])
                ],
            ),
        }
    )
    fan_out = search_incidents(QueryContext(config), env="all", window="15m", tag=None, level=None)
    # `env="all"` returns a per-env fan-out result (a TypedDict), not a flat list.
    assert not isinstance(fan_out, list)
    assert {r["env"] for r in fan_out["results"]} == {"dev", "stage"}
    for per_env in fan_out["results"]:
        assert per_env["error"] is None
        assert len(per_env["incidents"]) == 1


def test_env_all_fan_out_partial_result_marks_down_env() -> None:
    """One env without an incident source yields a per-env error marker, not a wholesale fail."""
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved_source("sentry", {SignalKind.INCIDENT}, signals=[_incident("dev")])
                ],
            ),
            "stage": ResolvedEnvironment(
                name="stage",
                enabled=True,
                # No incident source — stage cannot answer the incident query.
                sources=[_resolved_source("http-health", {SignalKind.METRIC})],
            ),
        }
    )
    fan_out = search_incidents(QueryContext(config), env="all", window="15m", tag=None, level=None)
    assert not isinstance(fan_out, list)
    by_env = {r["env"]: r for r in fan_out["results"]}
    assert by_env["dev"]["error"] is None
    assert len(by_env["dev"]["incidents"]) == 1
    # stage is included with an explicit per-env error marker — the call did not fail.
    assert by_env["stage"]["error"] is not None
    assert by_env["stage"]["incidents"] == []


# --- F1: query_metric / describe_health env="all" --------------------------------


def test_query_metric_env_all_omits_env_matcher_and_returns_across_env_series() -> None:
    """`query_metric(env="all")` builds a selector with NO `env=` matcher (F1).

    `name{env="all"}` would query a literal label value no signal carries → silent
    empty. Instead the across-env query drops the `env=` matcher entirely so it
    returns series across ALL envs (metrics already carry their own `env` label).
    """
    captured: list[str] = []

    class _RecordingStore:
        type = "recording"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            captured.append(query.expr)
            return [
                MetricSeries(
                    metric="panoptes_health_up",
                    labels={"env": "dev", "url": "https://dev/health"},
                    points=[(_now(), 1.0)],
                ),
                MetricSeries(
                    metric="panoptes_health_up",
                    labels={"env": "stage", "url": "https://stage/health"},
                    points=[(_now(), 0.0)],
                ),
            ]

    config = _dev_only_config(store=_RecordingStore())
    result = query_metric(
        QueryContext(config), env="all", name="panoptes_health_up", window="15m", filters=None
    )
    assert captured, "the store query was executed"
    expr = captured[0]
    # No `env=` matcher at all — the across-env query must NOT pin env to a literal.
    assert "env=" not in expr
    assert 'env="all"' not in expr
    # The across-env series come back (both dev + stage).
    assert {series.labels["env"] for series in result} == {"dev", "stage"}


def test_query_metric_env_all_with_filters_keeps_filters_drops_env() -> None:
    """`env="all"` still applies caller filters — only the `env=` matcher is dropped."""
    captured: list[str] = []

    class _RecordingStore:
        type = "recording"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            captured.append(query.expr)
            return []

    config = _dev_only_config(store=_RecordingStore())
    query_metric(
        QueryContext(config),
        env="all",
        name="panoptes_health_up",
        window="15m",
        filters={"url": "https://x/health"},
    )
    expr = captured[0]
    assert "env=" not in expr
    assert 'url="https://x/health"' in expr


def test_describe_health_env_all_aggregates_across_envs() -> None:
    """`describe_health(env="all")` aggregates per-env source health + incident counts (F1).

    The previous behavior fell through to `require_env("all")` → a misleading
    "unknown env" CapabilityError. Now it returns a HealthRollup with env="all", the
    UNION of per-env source-health entries (each carrying its env), and the SUM of
    open incident counts across envs.
    """
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved_source(
                        "sentry",
                        {SignalKind.INCIDENT, SignalKind.METRIC},
                        signals=[_incident("dev")],
                    ),
                ],
            ),
            "stage": ResolvedEnvironment(
                name="stage",
                enabled=True,
                sources=[
                    _resolved_source(
                        "sentry",
                        {SignalKind.INCIDENT, SignalKind.METRIC},
                        signals=[_incident("stage"), _incident("stage")],
                    ),
                ],
            ),
        }
    )
    rollup = describe_health(QueryContext(config), env="all")
    assert rollup["env"] == "all"
    # open incidents summed across envs (1 + 2 = 3).
    assert rollup["open_incident_count"] == 3
    # union of per-env source-health entries, each carrying its identifiable env.
    envs_seen = {source["env"] for source in rollup["sources"]}
    assert envs_seen == {"dev", "stage"}


# --- F7: PromQL injection hardening ----------------------------------------------


def _selector_recording_config() -> tuple[ResolvedConfig, list[str]]:
    """A dev-only config whose store records every executed PromQL expr."""
    captured: list[str] = []

    class _RecordingStore:
        type = "recording"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            captured.append(query.expr)
            return []

    return _dev_only_config(store=_RecordingStore()), captured


def test_query_metric_escapes_filter_value_quotes() -> None:
    """A filter value containing a `"` is ESCAPED, not allowed to break out (F7)."""
    config, captured = _selector_recording_config()
    query_metric(
        QueryContext(config),
        env="dev",
        name="panoptes_health_up",
        window="15m",
        filters={"url": 'a"b'},
    )
    expr = captured[0]
    # The embedded quote is backslash-escaped — the selector value stays a single closed
    # string instead of breaking out into an empty/garbage selector.
    assert r'url="a\"b"' in expr


def test_query_metric_escapes_backslash_in_filter_value() -> None:
    """A backslash in a filter value is escaped first (F7 — order matters)."""
    config, captured = _selector_recording_config()
    query_metric(
        QueryContext(config),
        env="dev",
        name="panoptes_health_up",
        window="15m",
        filters={"path": r"a\b"},
    )
    assert r'path="a\\b"' in captured[0]


def test_query_metric_rejects_unknown_env() -> None:
    """An env not in the config (and not the `all` sentinel) is rejected (F7)."""
    config, _ = _selector_recording_config()
    with pytest.raises(CapabilityError):
        query_metric(
            QueryContext(config),
            env="prod-typo",
            name="panoptes_health_up",
            window="15m",
            filters=None,
        )


def test_query_metric_rejects_bogus_metric_name() -> None:
    """A metric name with PromQL-breaking characters is rejected with a clear error (F7)."""
    config, _ = _selector_recording_config()
    with pytest.raises(CapabilityError):
        query_metric(
            QueryContext(config),
            env="dev",
            name="panoptes_health_up}",
            window="15m",
            filters=None,
        )


def test_query_metric_rejects_bogus_filter_label_key() -> None:
    """A filter label KEY that is not a valid PromQL identifier is rejected (F7)."""
    config, _ = _selector_recording_config()
    with pytest.raises(CapabilityError):
        query_metric(
            QueryContext(config),
            env="dev",
            name="panoptes_health_up",
            window="15m",
            filters={'url"=="': "x"},
        )


# --- F2f: _window_for parses the window arg; step is sub-window -------------------


def test_window_for_parses_1h() -> None:
    """`_window_for("1h")` spans 60 minutes (F2f — the arg is no longer ignored)."""
    from core.mcp.tools_query import _window_for

    window = _window_for("1h")
    span_minutes = round((window.end - window.start).total_seconds() / 60)
    assert span_minutes == 60


def test_window_for_parses_24h_and_1d_equivalently() -> None:
    """`_window_for("24h")` and `"1d"` both span 1440 minutes (F2f)."""
    from core.mcp.tools_query import _window_for

    for window_str in ("24h", "1d"):
        window = _window_for(window_str)
        span_minutes = round((window.end - window.start).total_seconds() / 60)
        assert span_minutes == 1440, f"{window_str!r} should span 1440 minutes"


def test_window_for_default_when_empty() -> None:
    """An empty/None window falls back to the default 15-minute trailing window (F2f)."""
    from core.mcp.tools_query import _DEFAULT_WINDOW_MINUTES, _window_for

    window = _window_for("")
    span_minutes = round((window.end - window.start).total_seconds() / 60)
    assert span_minutes == _DEFAULT_WINDOW_MINUTES


def test_window_for_unrecognized_falls_back_to_default_explicitly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrecognized window is handled EXPLICITLY (F2f) — fall back to default 15m AND
    surface the offending value in a warning (asserted behavior, not silent-15m)."""
    import logging

    from core.mcp.tools_query import _DEFAULT_WINDOW_MINUTES, _window_for

    with caplog.at_level(logging.WARNING):
        window = _window_for("not-a-window")
    span_minutes = round((window.end - window.start).total_seconds() / 60)
    assert span_minutes == _DEFAULT_WINDOW_MINUTES
    # The offending value is surfaced (explicit, not silent).
    assert any("not-a-window" in record.getMessage() for record in caplog.records)


def test_query_metric_step_is_strictly_sub_window_for_multiple_points() -> None:
    """`query_metric`'s step_seconds is strictly LESS than the window span (F2f).

    The old code used step == window (15m over a 15m window) → a single degenerate
    bucket. The step must be sub-window so a range yields multiple points.
    """
    captured: list[MetricQuery] = []

    class _RecordingStore:
        type = "recording"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            captured.append(query)
            return []

    config = _dev_only_config(store=_RecordingStore())
    query_metric(
        QueryContext(config), env="dev", name="panoptes_health_up", window="1h", filters=None
    )
    assert captured, "the store query was executed"
    query = captured[0]
    window_span_seconds = (query.window.end - query.window.start).total_seconds()
    assert query.step_seconds < window_span_seconds, (
        "step must be sub-window so the range yields multiple buckets, not one"
    )
    # And the step is a sane positive floor (never zero / sub-second).
    assert query.step_seconds >= 15


# --- F2d: public PromQL value-escape primitive -----------------------------------


def test_escape_promql_value_is_public_and_escapes_backslash_first() -> None:
    """`escape_promql_value` is the PUBLIC canonical primitive (F2d).

    It must be importable from `core.mcp.tools_query` (so consumers — incl. the demo
    pack — reuse one implementation) and must escape backslash FIRST, then the double
    quote, so a value like `a"b` becomes the single closed PromQL string `a\\"b` rather
    than breaking out of the selector.
    """
    assert escape_promql_value('a"b') == r"a\"b"
    # Backslash-first ordering: a lone backslash doubles, and a backslash-then-quote
    # value does not collapse the quote-escape's own backslash.
    assert escape_promql_value(r"a\b") == r"a\\b"
    assert escape_promql_value('a\\"b') == r"a\\\"b"


# --- v0.2 stub tools -------------------------------------------------------------


def test_v0_2_stub_tool_errors_at_call_time() -> None:
    """A config listing a v0.2 tool registers a stub returning not-available AT CALL TIME."""
    config = _dev_only_config(mcp={"transport": "stdio", "tools": ["query_metric", "compare_envs"]})
    server = build_server(config)
    # Listing compare_envs is NOT a resolve failure — the server builds fine.
    assert "compare_envs" in server.tool_names()
    stub = server.tool_callable("compare_envs")
    with pytest.raises(CapabilityError) as excinfo:
        stub(env="dev")
    message = str(excinfo.value).lower()
    assert "not available in v0.1" in message
    assert "v0.2" in message


# --- structural read-only contract -----------------------------------------------


def test_default_config_registers_exactly_the_read_only_tool_set() -> None:
    config = _dev_only_config(
        mcp={
            "transport": "stdio",
            "tools": [
                "describe_signal_catalog",
                "list_dashboards",
                "get_dashboard_data",
                "query_metric",
                "search_incidents",
                "search_logs",
                "describe_health",
            ],
        }
    )
    server = build_server(config)
    assert set(server.tool_names()) == set(KNOWN_READ_ONLY_TOOLS)


def test_no_registered_tool_name_matches_mutation_verb_regex() -> None:
    """A future write tool under ANY name fails — the assertion is structural, not a denylist."""
    config = _dev_only_config(
        mcp={"transport": "stdio", "tools": ["query_metric", "compare_envs", "get_slo"]}
    )
    server = build_server(config)
    for name in server.tool_names():
        assert _MUTATION_VERB_RE.search(name) is None, f"mutation-shaped tool name: {name}"


def test_server_is_panoptes_mcp_server_with_stdio_transport() -> None:
    config = _dev_only_config(mcp={"transport": "stdio", "tools": ["query_metric"]})
    server = build_server(config)
    assert isinstance(server, PanoptesMcpServer)
    # The FastMCP server is constructed and exposed for the stdio entrypoint.
    assert server.mcp is not None


def test_build_server_with_no_tools_block_defaults_to_full_read_only_set() -> None:
    """An omitted `mcp.tools` still yields the full core read-only set (not tool-less)."""
    server = build_server(_dev_only_config())
    assert set(server.tool_names()) == set(KNOWN_READ_ONLY_TOOLS)


def test_build_server_ignores_unknown_tool_name() -> None:
    """A tool name that is neither a v0.1 core tool nor a known v0.2 stub is skipped."""
    config = _dev_only_config(
        mcp={"transport": "stdio", "tools": ["query_metric", "not_a_real_tool"]}
    )
    server = build_server(config)
    assert "query_metric" in server.tool_names()
    assert "not_a_real_tool" not in server.tool_names()


# --- additional query-tool behaviors ---------------------------------------------


def test_search_logs_no_source_for_kind_raises_capability_error() -> None:
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[_resolved_source("http-health", {SignalKind.METRIC})],
            )
        }
    )
    with pytest.raises(CapabilityError):
        search_logs(QueryContext(config), env="dev", query="x", window="15m", level=None)


def test_search_logs_env_all_partial_result_marks_down_env() -> None:
    config = _config(
        {
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[_resolved_source("cloudwatch", {SignalKind.LOG}, signals=[_log("dev")])],
            ),
            "stage": ResolvedEnvironment(
                name="stage",
                enabled=True,
                sources=[_resolved_source("http-health", {SignalKind.METRIC})],
            ),
        }
    )
    fan_out = search_logs(QueryContext(config), env="all", query="error", window="15m", level=None)
    assert not isinstance(fan_out, list)
    by_env = {r["env"]: r for r in fan_out["results"]}
    assert len(by_env["dev"]["logs"]) == 1
    assert by_env["dev"]["error"] is None
    assert by_env["stage"]["error"] is not None


def test_search_incidents_filters_by_level_and_tag() -> None:
    config = _dev_only_config(
        sentry_signals=[
            _incident("dev", level=IncidentLevel.ERROR),
            _incident("dev", level=IncidentLevel.WARNING),
        ]
    )
    errors = search_incidents(
        QueryContext(config), env="dev", window="15m", tag=None, level="error"
    )
    assert isinstance(errors, list)
    assert all(i.level is IncidentLevel.ERROR for i in errors)
    # The `project` label value "p" is present on every fixture incident.
    tagged = search_incidents(QueryContext(config), env="dev", window="15m", tag="p", level=None)
    assert isinstance(tagged, list)
    assert len(tagged) == 2


def test_search_logs_filters_by_message_and_level() -> None:
    config = _dev_only_config(
        cloudwatch_signals=[
            _log("dev", level=LogLevel.ERROR),
            _log("dev", level=LogLevel.WARNING),
        ]
    )
    only_errors = search_logs(
        QueryContext(config), env="dev", query="error", window="15m", level="error"
    )
    assert isinstance(only_errors, list)
    assert all(log.level is LogLevel.ERROR for log in only_errors)
    no_match = search_logs(
        QueryContext(config), env="dev", query="no-such-text", window="15m", level=None
    )
    assert isinstance(no_match, list)
    assert no_match == []


def test_query_metric_applies_filters_to_selector() -> None:
    captured: list[str] = []

    class _RecordingStore:
        type = "recording"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            captured.append(query.expr)
            return []

    config = _dev_only_config(store=_RecordingStore())
    query_metric(
        QueryContext(config),
        env="dev",
        name="panoptes_health_up",
        window="15m",
        filters={"url": "https://x/health"},
    )
    assert captured, "the store query was executed"
    expr = captured[0]
    assert 'env="dev"' in expr
    assert 'url="https://x/health"' in expr


def test_require_env_unknown_env_raises_capability_error() -> None:
    config = _dev_only_config()
    with pytest.raises(CapabilityError):
        describe_health(QueryContext(config), env="does-not-exist")


def test_require_env_disabled_env_raises_capability_error() -> None:
    config = _config({"stage": ResolvedEnvironment(name="stage", enabled=False, sources=[])})
    with pytest.raises(CapabilityError):
        describe_health(QueryContext(config), env="stage")


# --- consumer-pack injection hook -------------------------------------------------


def test_consumer_pack_hook_registers_injected_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PANOPTES_CONSUMER_PACK` imports a pack module + calls its `register_tools`.

    The pack is injected (never imported by `core` statically). It registers a
    brand-neutral synthetic tool, proving the additive injection path: the tool
    appears ONLY as an addition to the core set.
    """
    pack_dir = tmp_path / "injected_pack_dir"
    pack_dir.mkdir()
    (pack_dir / "injected_pack.py").write_text(
        "def get_demo_signal(env: str, window: str) -> dict[str, str]:\n"
        "    return {'env': env}\n"
        "\n"
        "def register_tools(server: object) -> None:\n"
        "    register = getattr(server, '_register_tool')\n"
        "    register('get_demo_signal', get_demo_signal)\n"
    )
    monkeypatch.syspath_prepend(str(pack_dir))
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", "injected_pack")

    config = _dev_only_config(mcp={"transport": "stdio", "tools": ["query_metric"]})
    server = build_server(config)

    names = set(server.tool_names())
    assert "get_demo_signal" in names  # the injected tool appears as an addition
    assert "query_metric" in names  # core registration is unchanged


def test_no_consumer_pack_env_yields_core_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default unset = core-only: no pack import, no consumer tools."""
    monkeypatch.delenv("PANOPTES_CONSUMER_PACK", raising=False)
    server = build_server(_dev_only_config(mcp={"transport": "stdio", "tools": ["query_metric"]}))
    assert server.tool_names() == ["query_metric"]
