"""Unit tests for the promoted `get_cost` MCP tool (v0.3).

`get_cost(context, env, window) -> CostBreakdown` renders from the STORE's `panoptes_cost_*`
gauges (the two-faces-one-store parity — the Cost dashboard renders the SAME series), NOT a
live Cost Explorer call. `total` = the sum of the per-service spend; `per_service` =
`{service: latest spend}`; `budget_burn` = the latest burn gauge. Covers (spec § Cost types /
plan Phase 3):
- a correct `CostBreakdown` over a fake store (total = sum of per_service; budget_burn from
  the gauge; the per_service map exact);
- an unknown env fails clearly (a `CapabilityError`);
- no-cost-data → zero total / empty per_service / 0.0 burn (never a crash / silent-empty).
"""

from datetime import UTC, datetime

import pytest
from core.config import (
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
)
from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.mcp.tools_query import get_cost
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

# The cost gauge names `get_cost` reads back from the store (mirrors the cloudwatch source).
_COST_SPEND = "panoptes_cost_spend"
_COST_BUDGET_BURN = "panoptes_cost_budget_burn"


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
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


class _CostGaugeStore:
    """A fake store answering each `panoptes_cost_*` query from a fixed series map.

    Keyed on the metric-name prefix of the query expr so `get_cost` reads the same store the
    cloudwatch cost path writes to (the two-faces-one-store parity).
    """

    type = "cost-gauge"

    def __init__(self, series_by_metric: dict[str, list[MetricSeries]]) -> None:
        self._series_by_metric = series_by_metric

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        for metric_name, series in self._series_by_metric.items():
            if metric_name in query.expr:
                return series
        return []


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _cost_series(
    metric: str, env: str, value: float, *, service: str | None = None
) -> MetricSeries:
    labels = {"env": env}
    if service is not None:
        labels["service"] = service
    return MetricSeries(metric=metric, labels=labels, points=[(_now(), value)])


def _config(store: Store) -> ResolvedConfig:
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    ResolvedSource(
                        source=_FakeSource("cloudwatch", {SignalKind.METRIC, SignalKind.LOG}),
                        fetch_timeout_seconds=30,
                        poll_interval_seconds=3600,
                    )
                ],
            )
        },
        store=store,
        notifiers=notifiers,
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


def test_get_cost_aggregates_per_service_spend_and_budget_burn() -> None:
    """`get_cost` renders a correct `CostBreakdown` from the stored cost gauges."""
    store = _CostGaugeStore(
        {
            _COST_SPEND: [
                _cost_series(_COST_SPEND, "dev", 120.50, service="AmazonEC2"),
                _cost_series(_COST_SPEND, "dev", 30.25, service="AmazonS3"),
            ],
            _COST_BUDGET_BURN: [_cost_series(_COST_BUDGET_BURN, "dev", 0.42)],
        }
    )
    breakdown = get_cost(QueryContext(_config(store)), env="dev", window="30d")

    assert breakdown["env"] == "dev"
    assert breakdown["window"] == "30d"
    # per_service maps each service to its latest spend.
    assert breakdown["per_service"] == {"AmazonEC2": 120.50, "AmazonS3": 30.25}
    # total is the SUM of the per-service spend.
    assert breakdown["total"] == pytest.approx(150.75)
    # budget_burn is the latest burn gauge.
    assert breakdown["budget_burn"] == pytest.approx(0.42)


def test_get_cost_unknown_env_fails_clearly() -> None:
    """An unknown env is rejected via `require_env` (a CapabilityError)."""
    store = _CostGaugeStore({_COST_SPEND: [_cost_series(_COST_SPEND, "dev", 1.0, service="X")]})
    with pytest.raises(CapabilityError):
        get_cost(QueryContext(_config(store)), env="not-an-env", window="30d")


def test_get_cost_no_data_yields_zero_total_empty_services() -> None:
    """No cost data → zero total / empty per_service / 0.0 burn — never a crash or silent-empty."""

    class _EmptyStore:
        type = "empty"

        def write(self, signals: list[CanonicalSignal]) -> None:
            return None

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            return []

    breakdown = get_cost(QueryContext(_config(_EmptyStore())), env="dev", window="30d")
    assert breakdown["total"] == 0.0
    assert breakdown["per_service"] == {}
    assert breakdown["budget_burn"] == 0.0


def test_get_cost_passthrough_store_is_zero_not_crash() -> None:
    """A passthrough store (cannot answer PromQL) → zeros, not a crash/silent-empty."""
    from core.stores.passthrough import PassthroughStore

    breakdown = get_cost(QueryContext(_config(PassthroughStore({}))), env="dev", window="30d")
    assert breakdown["total"] == 0.0
    assert breakdown["per_service"] == {}
    assert breakdown["budget_burn"] == 0.0


def test_get_cost_spend_with_no_service_label_is_skipped() -> None:
    """A spend series with no `service` label does not corrupt the per_service map.

    The per_service map keys on the `service` label; a series missing it contributes to the
    total but cannot key the map — it is bucketed under an empty/unknown key rather than
    crashing, and the total still sums every spend series.
    """
    store = _CostGaugeStore(
        {
            _COST_SPEND: [
                _cost_series(_COST_SPEND, "dev", 10.0, service="AmazonEC2"),
                _cost_series(_COST_SPEND, "dev", 5.0),  # no service label
            ],
        }
    )
    breakdown = get_cost(QueryContext(_config(store)), env="dev", window="30d")
    # The total includes both; the per_service map carries the labelled one.
    assert breakdown["total"] == pytest.approx(15.0)
    assert breakdown["per_service"].get("AmazonEC2") == 10.0
