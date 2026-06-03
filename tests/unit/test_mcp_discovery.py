"""Phase 6 unit tests for the MCP discovery tools (`core/mcp/tools_discovery.py`).

Covers (spec `## Tests` → MCP bullet / playbook Phase 6 table):
- `describe_signal_catalog` lists the configured environments, each configured
  source + its `capabilities()`, the known derived-metric names, and the dashboard
  ids — over a fake `ResolvedConfig` built with the Phase-1 typed-fake style.
- `list_dashboards` returns the dashboard catalog (core + consumer packs) as
  `DashboardSummary`s.
- `get_dashboard_data` returns, per panel, the panel title + its PromQL target(s)
  + the **executed series from the store**, over an **inline/`tmp_path` dashboard
  JSON fixture** with known `panel.targets[].expr` (Risk R14 — NOT the Phase-5
  shipped JSON).
- An **unknown dashboard id** raises an explicit `CapabilityError` (the spec
  defines no separate `NotFoundError`; never silent/None).

All tests are synchronous and deterministic — no live network, no FastMCP
transport. The discovery functions take an explicit context (`ResolvedConfig` +
the resolved dashboard-pack catalog) so they are unit-testable without FastMCP.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core.config import (
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
)
from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.mcp.tools_discovery import (
    describe_signal_catalog,
    get_dashboard_data,
    list_dashboards,
)
from core.model import (
    Alert,
    CanonicalSignal,
    DashboardPack,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier

ConfigBlock = Mapping[str, str | int | bool | list[str]]


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    """A typed fake `Source` with a fixed capability set + a stamped `type`."""

    def __init__(self, source_type: str, capabilities: set[SignalKind]) -> None:
        self.type = source_type
        self._capabilities = capabilities

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _FakeStore:
    """A fake `Store` returning a fixed series for any query (records the last expr)."""

    type = "fake"

    def __init__(self, series: list[MetricSeries]) -> None:
        self._series = series
        self.queried_exprs: list[str] = []

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.queried_exprs.append(query.expr)
        return self._series


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _resolved_source(source_type: str, capabilities: set[SignalKind]) -> ResolvedSource:
    return ResolvedSource(
        source=_FakeSource(source_type, capabilities),
        fetch_timeout_seconds=30,
        poll_interval_seconds=60,
    )


def _build_config(
    store: _FakeStore,
    *,
    dashboard_packs: list[DashboardPack] | None = None,
) -> ResolvedConfig:
    """A `ResolvedConfig` with a `dev` env (3 sources) + a disabled `stage` env."""
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    _resolved_source("cloudwatch", {SignalKind.METRIC, SignalKind.LOG}),
                    _resolved_source("sentry", {SignalKind.INCIDENT, SignalKind.METRIC}),
                    _resolved_source("http-health", {SignalKind.METRIC}),
                ],
            ),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[]),
        },
        store=store,
        notifiers=notifiers,
        dashboard_packs=dashboard_packs if dashboard_packs is not None else [],
        slos=[],
        mcp={},
    )


def _write_inline_dashboard(tmp_path: Path) -> Path:
    """An inline dashboard JSON with two panels and known `targets[].expr` (R14)."""
    dashboard = {
        "title": "Inline Test",
        "panels": [
            {
                "id": 1,
                "title": "Health up",
                "targets": [
                    {"refId": "A", "expr": 'panoptes_health_up{env=~"$env"}'},
                ],
            },
            {
                "id": 2,
                "title": "Incident count",
                "targets": [
                    {"refId": "A", "expr": 'panoptes_sentry_incident_count{env=~"$env"}'},
                ],
            },
        ],
    }
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(dashboard))
    return path


def test_describe_signal_catalog_lists_envs_sources_and_capabilities() -> None:
    config = _build_config(_FakeStore([]))
    catalog = describe_signal_catalog(QueryContext(config))

    assert catalog["environments"] == ["dev", "stage"]
    # Each configured source of the (enabled) dev env appears with its capabilities.
    dev_sources = {s["type"]: set(s["capabilities"]) for s in catalog["sources"]}
    assert dev_sources["cloudwatch"] == {"metric", "log"}
    assert dev_sources["sentry"] == {"incident", "metric"}
    assert dev_sources["http-health"] == {"metric"}


def test_describe_signal_catalog_lists_known_metrics_and_dashboard_ids() -> None:
    packs = [
        DashboardPack(id="overview", tier="core", json_path=Path("core/dashboards/overview")),
        DashboardPack(id="consumer", tier="consumer", json_path=Path("/packs/consumer")),
    ]
    config = _build_config(_FakeStore([]), dashboard_packs=packs)
    catalog = describe_signal_catalog(QueryContext(config))

    # Derived metric names are surfaced so an LLM knows what it can query.
    assert "panoptes_health_up" in catalog["metrics"]
    assert "panoptes_sentry_incident_count" in catalog["metrics"]
    # Dashboard ids come from the resolved pack catalog.
    assert catalog["dashboards"] == ["overview", "consumer"]


def test_list_dashboards_returns_the_catalog() -> None:
    packs = [
        DashboardPack(id="overview", tier="core", json_path=Path("core/dashboards/overview")),
        DashboardPack(id="consumer", tier="consumer", json_path=Path("/packs/consumer")),
    ]
    summaries = list_dashboards(packs)

    assert [s["id"] for s in summaries] == ["overview", "consumer"]
    assert [s["tier"] for s in summaries] == ["core", "consumer"]


def test_get_dashboard_data_returns_titles_promql_and_executed_series(tmp_path: Path) -> None:
    dashboard_path = _write_inline_dashboard(tmp_path)
    series = [
        MetricSeries(
            metric="panoptes_health_up",
            labels={"env": "dev", "url": "https://x/health"},
            points=[(_now(), 1.0)],
        )
    ]
    store = _FakeStore(series)
    packs = [DashboardPack(id="inline", tier="core", json_path=dashboard_path)]
    config = _build_config(store, dashboard_packs=packs)

    data = get_dashboard_data("inline", "dev", QueryContext(config))

    assert data["id"] == "inline"
    assert data["env"] == "dev"
    assert [p["title"] for p in data["panels"]] == ["Health up", "Incident count"]
    # The `$env` template variable is substituted with the requested env in the
    # PromQL that is actually executed against the store.
    first_panel = data["panels"][0]
    assert first_panel["targets"][0]["expr"] == 'panoptes_health_up{env=~"dev"}'
    # The executed series from the store are attached to the panel.
    assert first_panel["targets"][0]["series"][0]["metric"] == "panoptes_health_up"
    # Both panels' exprs were executed against the store.
    assert any('panoptes_health_up{env=~"dev"}' in e for e in store.queried_exprs)
    assert any('panoptes_sentry_incident_count{env=~"dev"}' in e for e in store.queried_exprs)


def test_get_dashboard_data_unknown_id_raises_capability_error(tmp_path: Path) -> None:
    store = _FakeStore([])
    packs = [DashboardPack(id="inline", tier="core", json_path=_write_inline_dashboard(tmp_path))]
    config = _build_config(store, dashboard_packs=packs)

    with pytest.raises(CapabilityError) as excinfo:
        get_dashboard_data("does-not-exist", "dev", QueryContext(config))
    assert "does-not-exist" in str(excinfo.value)


def test_describe_signal_catalog_only_lists_enabled_env_sources() -> None:
    """A disabled env carries no sources, so it contributes nothing to `sources`."""
    config = _build_config(_FakeStore([]))
    catalog = describe_signal_catalog(QueryContext(config))
    # Only the dev env's three sources appear; the disabled stage env adds none.
    assert len(catalog["sources"]) == 3
