"""Unit tests for the promoted `get_slo` + `compare_envs` MCP tools (v0.2).

Covers (spec § SLO + MCP return types / § promoted tools / plan Phase 4):
- `get_slo` returns a correct `SloResult` for a MET and an UNMET objective — asserting
  the `met` flag AND the exact `error_budget_remaining` math;
- an unknown SLO name fails clearly (a `CapabilityError`, not a silent-empty result);
- `compare_envs` fans across every ENABLED env (`per_env` carries each env's series);
- a one-env-down case puts that env in `errors` (a per-env marker) while the OTHER envs
  still return their data — never a wholesale fail.

The store is a tiny in-test fake; the SLO config + envs are hand-built so each tool is
driven only through the `QueryContext` seam (no whole ResolvedConfig assembly beyond what
the tool reads).
"""

from datetime import UTC, datetime

import pytest
from core.config import (
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
    SloConfig,
)
from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.mcp.tools_query import compare_envs, get_slo
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


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    """A typed fake `Source` (only its capabilities/health are read by these tools)."""

    fetch_when_unreachable = False

    def __init__(self, source_type: str, capabilities: set[SignalKind]) -> None:
        self.type = source_type
        self._capabilities = capabilities

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _ValueStore:
    """A fake store returning one series at `value` for the env carried in the selector.

    The series is labelled with `env` so a per-env query reads only that env's value. A
    fixed value per env is supplied via `value_by_env`; an env absent from the map gets no
    series (an empty answer).
    """

    type = "value"

    def __init__(self, value_by_env: dict[str, float]) -> None:
        self._value_by_env = value_by_env

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        # Return a one-point series for every env whose value is configured AND whose
        # `env="..."` matcher appears in the expr (the tool scopes per env).
        series: list[MetricSeries] = []
        for env, value in self._value_by_env.items():
            if f'env="{env}"' in query.expr:
                series.append(
                    MetricSeries(
                        metric="panoptes_health_up",
                        labels={"env": env},
                        points=[(_now(), value)],
                    )
                )
        return series


class _AllEnvStore:
    """A fake store returning a series for EVERY configured env regardless of the selector.

    Used by `compare_envs` (which queries `metric` per env via the fan-out); each env's
    query returns that env's single value.
    """

    type = "all-env"

    def __init__(self, value_by_env: dict[str, float]) -> None:
        self._value_by_env = value_by_env

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        series: list[MetricSeries] = []
        for env, value in self._value_by_env.items():
            if f'env="{env}"' in query.expr:
                series.append(
                    MetricSeries(
                        metric="panoptes_health_up",
                        labels={"env": env},
                        points=[(_now(), value)],
                    )
                )
        return series


class _DownEnvStore:
    """A store that raises `CapabilityError` for one env's query but answers others.

    Simulates a per-env outage: the `down_env`'s query cannot be answered, so `compare_envs`
    must mark THAT env in `errors` while the others still return data (partial result).
    """

    type = "down-env"

    def __init__(self, value_by_env: dict[str, float], down_env: str) -> None:
        self._value_by_env = value_by_env
        self._down_env = down_env

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        if f'env="{self._down_env}"' in query.expr:
            raise CapabilityError(f"env '{self._down_env}' is down: store cannot answer")
        series: list[MetricSeries] = []
        for env, value in self._value_by_env.items():
            if f'env="{env}"' in query.expr:
                series.append(
                    MetricSeries(
                        metric="panoptes_health_up",
                        labels={"env": env},
                        points=[(_now(), value)],
                    )
                )
        return series


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
    store: Store,
    slos: list[SloConfig] | None = None,
) -> ResolvedConfig:
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments=environments,
        store=store,
        notifiers=notifiers,
        dashboard_packs=[],
        slos=slos if slos is not None else [],
        mcp={},
    )


def _env(name: str) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        name=name,
        enabled=True,
        sources=[_resolved_source("http-health", {SignalKind.METRIC})],
    )


# --- get_slo ----------------------------------------------------------------------


def test_get_slo_met_objective_returns_correct_result() -> None:
    """A MET objective: actual >= objective → `met=True` and the exact budget math.

    objective o=0.99, actual a=0.995 →
        error_budget_remaining = (a - o) / (1 - o) = (0.995 - 0.99) / 0.01 = 0.5
    """
    slo: SloConfig = {
        "name": "health-uptime",
        "objective": 0.99,
        "query": "panoptes_health_up",
    }
    store = _ValueStore({"dev": 0.995})
    config = _config({"dev": _env("dev")}, store=store, slos=[slo])
    result = get_slo(QueryContext(config), env="dev", name="health-uptime")

    assert result["name"] == "health-uptime"
    assert result["env"] == "dev"
    assert result["objective"] == 0.99
    assert result["actual"] == 0.995
    assert result["met"] is True
    # (0.995 - 0.99) / (1 - 0.99) = 0.005 / 0.01 = 0.5 (half the budget unspent).
    assert result["error_budget_remaining"] == pytest.approx(0.5)


def test_get_slo_unmet_objective_returns_negative_budget() -> None:
    """An UNMET objective: actual < objective → `met=False` and a negative budget.

    objective o=0.99, actual a=0.98 →
        error_budget_remaining = (0.98 - 0.99) / (1 - 0.99) = -0.01 / 0.01 = -1.0
    (the budget is fully overspent — clamped at the -1.0 floor).
    """
    slo: SloConfig = {
        "name": "health-uptime",
        "objective": 0.99,
        "query": "panoptes_health_up",
    }
    store = _ValueStore({"dev": 0.98})
    config = _config({"dev": _env("dev")}, store=store, slos=[slo])
    result = get_slo(QueryContext(config), env="dev", name="health-uptime")

    assert result["actual"] == 0.98
    assert result["met"] is False
    # (0.98 - 0.99) / 0.01 = -1.0 (the whole budget is overspent).
    assert result["error_budget_remaining"] == pytest.approx(-1.0)


def test_get_slo_at_objective_boundary_is_met_with_zero_budget() -> None:
    """actual == objective → met (>=) and exactly zero budget remaining."""
    slo: SloConfig = {"name": "uptime", "objective": 0.99, "query": "panoptes_health_up"}
    store = _ValueStore({"dev": 0.99})
    config = _config({"dev": _env("dev")}, store=store, slos=[slo])
    result = get_slo(QueryContext(config), env="dev", name="uptime")
    assert result["met"] is True
    assert result["error_budget_remaining"] == pytest.approx(0.0)


def test_get_slo_rejects_a_non_identifier_query() -> None:
    """F7: an SLO whose `query` is not a bare metric name is rejected with a clear error.

    `_slo_actual` reads the query via `read_gauge`, which wraps it with an `env=` selector, so a
    non-identifier query (a full selector / breakout token) would corrupt the selector. The
    contract is pinned: the SLO query must be a PromQL identifier — a malformed one fails clearly.
    """
    slo: SloConfig = {"name": "bad", "objective": 0.99, "query": 'up"} or up{'}
    config = _config({"dev": _env("dev")}, store=_ValueStore({"dev": 1.0}), slos=[slo])
    with pytest.raises(CapabilityError) as excinfo:
        get_slo(QueryContext(config), env="dev", name="bad")
    assert "query" in str(excinfo.value).lower()


def test_get_slo_degenerate_objective_met_reports_full_budget() -> None:
    """A degenerate objective=1.0 (zero-width budget) WITH actual==1.0 → met + full budget 1.0.

    `_error_budget_remaining` cannot divide by a zero-width budget (`1 - o == 0`), so it reports
    `1.0` when the actual meets the objective — the no-division branch.
    """
    slo: SloConfig = {"name": "perfect", "objective": 1.0, "query": "panoptes_health_up"}
    store = _ValueStore({"dev": 1.0})
    config = _config({"dev": _env("dev")}, store=store, slos=[slo])
    result = get_slo(QueryContext(config), env="dev", name="perfect")
    assert result["met"] is True
    assert result["error_budget_remaining"] == pytest.approx(1.0)


def test_get_slo_degenerate_objective_unmet_reports_floor_budget() -> None:
    """A degenerate objective=1.0 WITH actual==0.99 → unmet + the overspent floor (-1.0).

    The other half of the no-division branch: when a 100%-objective is NOT met, the budget is
    the overspent floor (`-1.0`), never a divide-by-zero.
    """
    slo: SloConfig = {"name": "perfect", "objective": 1.0, "query": "panoptes_health_up"}
    store = _ValueStore({"dev": 0.99})
    config = _config({"dev": _env("dev")}, store=store, slos=[slo])
    result = get_slo(QueryContext(config), env="dev", name="perfect")
    assert result["met"] is False
    assert result["error_budget_remaining"] == pytest.approx(-1.0)


def test_get_slo_unknown_name_fails_clearly() -> None:
    """An unknown SLO name raises a clear CapabilityError (not a silent-empty result)."""
    slo: SloConfig = {"name": "uptime", "objective": 0.99}
    config = _config({"dev": _env("dev")}, store=_ValueStore({"dev": 1.0}), slos=[slo])
    with pytest.raises(CapabilityError) as excinfo:
        get_slo(QueryContext(config), env="dev", name="does-not-exist")
    assert "does-not-exist" in str(excinfo.value)


def test_get_slo_unknown_env_fails_clearly() -> None:
    """An unknown env is rejected via `require_env` (a CapabilityError)."""
    slo: SloConfig = {"name": "uptime", "objective": 0.99, "query": "panoptes_health_up"}
    config = _config({"dev": _env("dev")}, store=_ValueStore({"dev": 1.0}), slos=[slo])
    with pytest.raises(CapabilityError):
        get_slo(QueryContext(config), env="not-an-env", name="uptime")


# --- compare_envs -----------------------------------------------------------------


def test_compare_envs_fans_across_enabled_envs() -> None:
    """`compare_envs` returns each enabled env's series under `per_env`, no errors."""
    store = _AllEnvStore({"dev": 1.0, "stage": 0.0})
    config = _config({"dev": _env("dev"), "stage": _env("stage")}, store=store)
    comparison = compare_envs(QueryContext(config), metric="panoptes_health_up", window="15m")
    assert comparison["metric"] == "panoptes_health_up"
    assert comparison["window"] == "15m"
    # Each enabled env carries its series.
    assert set(comparison["per_env"]) == {"dev", "stage"}
    assert comparison["per_env"]["dev"][0].labels["env"] == "dev"
    assert comparison["per_env"]["stage"][0].labels["env"] == "stage"
    # No env errored.
    assert comparison["errors"] == {}


def test_compare_envs_one_env_down_is_a_per_env_error_not_wholesale_fail() -> None:
    """A down env appears in `errors`; the others still return data (partial result)."""
    store = _DownEnvStore({"dev": 1.0, "stage": 0.5}, down_env="stage")
    config = _config({"dev": _env("dev"), "stage": _env("stage")}, store=store)
    comparison = compare_envs(QueryContext(config), metric="panoptes_health_up", window="15m")
    # dev still returned its data.
    assert "dev" in comparison["per_env"]
    assert comparison["per_env"]["dev"][0].labels["env"] == "dev"
    # stage is marked in errors (a per-env marker), NOT a wholesale failure.
    assert "stage" in comparison["errors"]
    assert "down" in comparison["errors"]["stage"]
    # The down env carries no series under per_env (its data is absent, not invented).
    assert comparison["per_env"].get("stage", []) == []


def test_compare_envs_disabled_env_is_not_queried() -> None:
    """A disabled env is excluded from the fan-out (only enabled envs are compared)."""
    store = _AllEnvStore({"dev": 1.0, "stage": 0.0})
    config = _config(
        {
            "dev": _env("dev"),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[]),
        },
        store=store,
    )
    comparison = compare_envs(QueryContext(config), metric="panoptes_health_up", window="15m")
    # Only the enabled env appears.
    assert set(comparison["per_env"]) == {"dev"}
    assert "stage" not in comparison["errors"]


def test_compare_envs_rejects_a_breakout_metric_name() -> None:
    """MAJOR-2 (F7): a breakout metric name is REJECTED before ANY store query runs.

    `compare_envs` splices the metric name UNQUOTED into each env's selector, so a name
    carrying PromQL-breaking chars (`"`/`{`/`}`/`\\`) must be rejected by the
    `_PROMQL_IDENTIFIER_RE` guard with a `CapabilityError` — and crucially BEFORE the store is
    touched (a breakout token must never reach the store). The recording store proves it was
    never queried.
    """

    class _RecordingStore:
        type = "recording"

        def __init__(self) -> None:
            self.exprs: list[str] = []

        def write(self, signals: list[CanonicalSignal]) -> None:  # pragma: no cover - unused
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            self.exprs.append(query.expr)
            return []

    store = _RecordingStore()
    config = _config({"dev": _env("dev")}, store=store)
    with pytest.raises(CapabilityError):
        compare_envs(QueryContext(config), metric='up"} or up{', window="15m")
    # The breakout name was rejected BEFORE any query — the store was never touched.
    assert store.exprs == [], "a breakout metric must be rejected before any store query"
