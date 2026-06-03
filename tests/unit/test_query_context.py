"""Unit tests for the `QueryContext` seam the MCP tools depend on.

`QueryContext` is the small read-only interface the MCP query/discovery tools were
refactored to consume instead of reaching into `ResolvedConfig`'s shape directly.
The point of the seam is that a test of one context behavior no longer needs to
build a whole `ResolvedConfig` with a store, notifiers, dashboards, SLOs, and an
MCP block — these tests drive each context method against a MINIMAL hand-built
config (just the env(s) a method actually reads), proving the interface IS the test
surface.

All tests are synchronous and deterministic.
"""

from datetime import UTC, datetime

import pytest
from core.config import (
    McpConfig,
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
)
from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.model import (
    Alert,
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier
from core.planes.store import Store


class _FakeSource:
    """A typed fake `Source` advertising a fixed capability set."""

    # Default outage-fetch opt-out (most sources skip fetch when unreachable — F3a).
    fetch_when_unreachable = False

    def __init__(self, source_type: str, capabilities: set[SignalKind]) -> None:
        self.type = source_type
        self._capabilities = capabilities

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=datetime.now(UTC))


class _FakeStore:
    type = "fake"

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []


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


def _config(
    environments: dict[str, ResolvedEnvironment],
    *,
    store: Store | None = None,
) -> ResolvedConfig:
    notifiers: list[Notifier] = [_NoopNotifier()]
    mcp: McpConfig = {}
    return ResolvedConfig(
        environments=environments,
        store=store if store is not None else _FakeStore(),
        notifiers=notifiers,
        dashboard_packs=[],
        slos=[],
        mcp=mcp,
    )


def test_enabled_envs_excludes_disabled_in_declaration_order() -> None:
    config = _config(
        {
            "dev": ResolvedEnvironment(name="dev", enabled=True, sources=[]),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[]),
            "prod": ResolvedEnvironment(name="prod", enabled=True, sources=[]),
        }
    )
    context = QueryContext(config)
    # A minimal config (no store needed for this method) drives the env walk; the
    # disabled env is inert and the order follows declaration.
    assert [env.name for env in context.enabled_envs()] == ["dev", "prod"]


def test_require_env_returns_enabled_environment() -> None:
    config = _config({"dev": ResolvedEnvironment(name="dev", enabled=True, sources=[])})
    context = QueryContext(config)
    assert context.require_env("dev").name == "dev"


def test_require_env_unknown_raises_capability_error_naming_available() -> None:
    config = _config({"dev": ResolvedEnvironment(name="dev", enabled=True, sources=[])})
    context = QueryContext(config)
    with pytest.raises(CapabilityError) as excinfo:
        context.require_env("nope")
    # The error names the unknown env and the available set (the existing contract).
    message = str(excinfo.value)
    assert "nope" in message
    assert "dev" in message


def test_require_env_disabled_raises_capability_error() -> None:
    config = _config({"stage": ResolvedEnvironment(name="stage", enabled=False, sources=[])})
    context = QueryContext(config)
    with pytest.raises(CapabilityError):
        context.require_env("stage")


def test_sources_for_returns_only_capability_providers() -> None:
    environment = ResolvedEnvironment(
        name="dev",
        enabled=True,
        sources=[
            _resolved_source("cloudwatch", {SignalKind.METRIC, SignalKind.LOG}),
            _resolved_source("sentry", {SignalKind.INCIDENT}),
        ],
    )
    config = _config({"dev": environment})
    context = QueryContext(config)
    log_sources = context.sources_for(environment, SignalKind.LOG)
    assert [s.type for s in log_sources] == ["cloudwatch"]
    incident_sources = context.sources_for(environment, SignalKind.INCIDENT)
    assert [s.type for s in incident_sources] == ["sentry"]
    # A kind no source provides yields an empty list (capability-negotiation input).
    assert context.sources_for(environment, SignalKind.TRACE) == []


def test_store_property_exposes_the_resolved_store() -> None:
    store = _FakeStore()
    config = _config(
        {"dev": ResolvedEnvironment(name="dev", enabled=True, sources=[])}, store=store
    )
    context = QueryContext(config)
    assert context.store is store


def test_dashboard_packs_property_exposes_the_catalog() -> None:
    config = _config({"dev": ResolvedEnvironment(name="dev", enabled=True, sources=[])})
    context = QueryContext(config)
    # The minimal config carries an empty pack catalog; the property surfaces it.
    assert context.dashboard_packs == []


def test_all_envs_and_env_names_include_disabled_for_the_catalog() -> None:
    config = _config(
        {
            "dev": ResolvedEnvironment(name="dev", enabled=True, sources=[]),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[]),
        }
    )
    context = QueryContext(config)
    # The signal catalog lists EVERY declared env (incl. disabled), unlike the
    # enabled-only fetch walk — both faces are exposed by the context.
    assert context.env_names() == ["dev", "stage"]
    assert [env.name for env in context.all_envs()] == ["dev", "stage"]
