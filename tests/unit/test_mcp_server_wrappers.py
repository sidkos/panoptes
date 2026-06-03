"""Unit coverage for the 7 real MCP tool wrappers registered by `build_server` (F3f).

The per-tool `_register_*` closures in `core/mcp/server.py` (and the uniform invokers
they bind) were previously exercised ONLY by the integration stdio suite — never by a
unit test. This module invokes each real v0.1 tool synchronously via the
`tool_callable(name)(...)` seam (the uniform `_ToolCallable` invoker stored alongside the
FastMCP-facing wrapper) against a fake-store `ResolvedConfig`, asserting the delegated
return shape. It lifts `core.mcp` coverage and guards the wiring (name -> bound context ->
core function) without Docker.

The invokers forward by keyword to the same core functions the FastMCP wrappers call, so
a green assertion here proves the registered tool's delegation is correct — independent of
the async transport. All fakes implement the plane Protocols directly (no YAML, no AWS).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.config import (
    McpConfig,
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
    SloConfig,
)
from core.mcp.server import build_server
from core.model import (
    Alert,
    CanonicalSignal,
    DashboardPack,
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
from core.planes.store import Store


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    """A typed fake `Source` returning fixed signals + a fixed health result."""

    # Default outage-fetch opt-out (most sources skip fetch when unreachable — F3a).
    fetch_when_unreachable = False

    def __init__(
        self,
        source_type: str,
        capabilities: set[SignalKind],
        *,
        signals: list[CanonicalSignal] | None = None,
    ) -> None:
        self.type = source_type
        self._capabilities = capabilities
        self._signals = signals if signals is not None else []

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return list(self._signals)

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _FakeStore:
    """A `Store` returning one fixed series for any query (so query_metric has data)."""

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


def _incident(env: str) -> IncidentSignal:
    return IncidentSignal(
        id="i-1",
        title="boom",
        level=IncidentLevel.ERROR,
        first_seen=_now(),
        last_seen=_now(),
        count=2,
        labels={"env": env, "level": IncidentLevel.ERROR.value, "project": "p"},
    )


def _log(env: str) -> LogSignal:
    return LogSignal(
        timestamp=_now(),
        message="error happened",
        level=LogLevel.ERROR,
        labels={"env": env},
    )


def _write_inline_dashboard(tmp_path: Path) -> Path:
    """A one-panel inline dashboard so get_dashboard_data has a real layout to execute."""
    dashboard = {
        "title": "Inline",
        "panels": [
            {
                "id": 1,
                "title": "Health up",
                "targets": [{"refId": "A", "expr": 'panoptes_health_up{env=~"$env"}'}],
            }
        ],
    }
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(dashboard))
    return path


def _build_config(
    *,
    store: Store,
    dashboard_packs: list[DashboardPack],
    sentry_signals: list[CanonicalSignal] | None = None,
    cloudwatch_signals: list[CanonicalSignal] | None = None,
    mcp: McpConfig | None = None,
    slos: list[SloConfig] | None = None,
) -> ResolvedConfig:
    """A single enabled `dev` env wiring the three core sources + one dashboard pack."""

    def _resolved(
        source_type: str, caps: set[SignalKind], sigs: list[CanonicalSignal] | None
    ) -> ResolvedSource:
        return ResolvedSource(
            source=_FakeSource(source_type, caps, signals=sigs),
            fetch_timeout_seconds=30,
            poll_interval_seconds=60,
        )

    return ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved(
                        "cloudwatch", {SignalKind.METRIC, SignalKind.LOG}, cloudwatch_signals
                    ),
                    _resolved("sentry", {SignalKind.INCIDENT, SignalKind.METRIC}, sentry_signals),
                    _resolved("http-health", {SignalKind.METRIC}, None),
                ],
            ),
        },
        store=store,
        notifiers=[_NoopNotifier()],
        dashboard_packs=dashboard_packs,
        slos=slos if slos is not None else [],
        mcp=mcp if mcp is not None else {},
    )


def _series(env: str) -> MetricSeries:
    return MetricSeries(
        metric="panoptes_health_up",
        labels={"env": env},
        points=[(_now(), 1.0)],
    )


def test_describe_signal_catalog_tool_invoker_returns_catalog(tmp_path: Path) -> None:
    config = _build_config(store=_FakeStore(), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    catalog = server.tool_callable("describe_signal_catalog")()

    assert isinstance(catalog, dict)
    # The catalog lists the dev env and its three configured sources.
    assert "dev" in catalog["environments"]


def test_list_dashboards_tool_invoker_returns_catalog(tmp_path: Path) -> None:
    config = _build_config(store=_FakeStore(), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    dashboards = server.tool_callable("list_dashboards")()

    assert isinstance(dashboards, list)
    assert any(entry["id"] == "inline" for entry in dashboards)


def test_get_dashboard_data_tool_invoker_executes_panels(tmp_path: Path) -> None:
    config = _build_config(store=_FakeStore([_series("dev")]), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    data = server.tool_callable("get_dashboard_data")(dashboard_id="inline", env="dev")

    assert isinstance(data, dict)
    # The single panel's $env was substituted to the requested env and executed.
    assert data["panels"][0]["targets"][0]["expr"] == 'panoptes_health_up{env=~"dev"}'


def test_query_metric_tool_invoker_returns_series(tmp_path: Path) -> None:
    config = _build_config(store=_FakeStore([_series("dev")]), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    result = server.tool_callable("query_metric")(
        env="dev", metric="panoptes_health_up", window="15m"
    )

    assert isinstance(result, list)
    assert result and result[0].metric == "panoptes_health_up"


def test_search_incidents_tool_invoker_returns_incidents(tmp_path: Path) -> None:
    config = _build_config(
        store=_FakeStore(),
        dashboard_packs=[_pack(tmp_path)],
        sentry_signals=[_incident("dev")],
    )
    server = build_server(config)

    result = server.tool_callable("search_incidents")(env="dev", window="15m")

    assert isinstance(result, list)
    assert result and isinstance(result[0], IncidentSignal)


def test_search_logs_tool_invoker_returns_logs(tmp_path: Path) -> None:
    config = _build_config(
        store=_FakeStore(),
        dashboard_packs=[_pack(tmp_path)],
        cloudwatch_signals=[_log("dev")],
    )
    server = build_server(config)

    result = server.tool_callable("search_logs")(env="dev", query="error", window="15m")

    assert isinstance(result, list)
    assert result and isinstance(result[0], LogSignal)


def test_describe_health_tool_invoker_returns_rollup(tmp_path: Path) -> None:
    config = _build_config(store=_FakeStore(), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    rollup = server.tool_callable("describe_health")(env="dev")

    assert isinstance(rollup, dict)
    # Every configured source is rolled up; all are reachable in this fake.
    assert rollup["env"] == "dev"
    assert all(source["reachable"] for source in rollup["sources"])


# --- v0.2/v0.3 real tools END-TO-END through the registered, signature-DERIVED invoker ------
#
# The `_make_invoker`-derived invokers for `get_slo`/`compare_envs`/`get_cluster_state` were
# only ever exercised by calling the core fns directly — never through the registered wrapper.
# The `get_slo` invoker carries the highest-risk derivation: the `slo_name` tool param is
# renamed to `name` INSIDE the tool fn, so a faithful derivation must forward `slo_name=...`.
# These three drive `server.tool_callable(name)(**kwargs)` end to end, proving the derived
# invoker forwards each tool's kwargs (incl. the rename) through the real registration path.


def test_get_slo_tool_invoker_forwards_slo_name_rename(tmp_path: Path) -> None:
    """`tool_callable('get_slo')(env, slo_name)` reaches `get_slo(..., name=slo_name)`.

    The `slo_name → name` rename is internal to the tool fn, so this proves the derived invoker
    forwards `slo_name` correctly through the registered wrapper (the highest-risk derivation).
    """
    slo: SloConfig = {"name": "uptime", "objective": 0.99}
    # The store answers any query with a single 1.0 sample → a measurable actual attainment.
    config = _build_config(
        store=_FakeStore([_series("dev")]), dashboard_packs=[_pack(tmp_path)], slos=[slo]
    )
    server = build_server(config)

    result = server.tool_callable("get_slo")(env="dev", slo_name="uptime")

    assert isinstance(result, dict)
    assert result["name"] == "uptime", "the slo_name kwarg must reach get_slo under name="
    assert result["env"] == "dev"
    assert result["objective"] == 0.99
    # The fake store's 1.0 actual meets the 0.99 objective.
    assert result["actual"] == 1.0
    assert result["met"] is True


def test_compare_envs_tool_invoker_returns_comparison(tmp_path: Path) -> None:
    """`tool_callable('compare_envs')(metric, window)` returns the per-env comparison."""
    config = _build_config(store=_FakeStore([_series("dev")]), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    result = server.tool_callable("compare_envs")(metric="panoptes_health_up", window="15m")

    assert isinstance(result, dict)
    assert result["metric"] == "panoptes_health_up"
    assert result["window"] == "15m"
    # The single enabled `dev` env answered (its series under per_env), no errors.
    assert "dev" in result["per_env"]
    assert result["errors"] == {}


def test_get_cluster_state_tool_invoker_returns_state(tmp_path: Path) -> None:
    """`tool_callable('get_cluster_state')(env)` returns the snapshot through the real wrapper."""
    config = _build_config(store=_FakeStore([_series("dev")]), dashboard_packs=[_pack(tmp_path)])
    server = build_server(config)

    state = server.tool_callable("get_cluster_state")(env="dev")

    assert isinstance(state, dict)
    assert state["env"] == "dev"
    # The fake store answers every k8s query with the same 1.0 series → reachable + counted.
    assert state["reachable"] is True
    assert state["node_count"] == 1.0


def _pack(tmp_path: Path) -> DashboardPack:
    return DashboardPack(id="inline", tier="core", json_path=_write_inline_dashboard(tmp_path))
