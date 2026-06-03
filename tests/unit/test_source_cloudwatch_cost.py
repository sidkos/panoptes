"""v0.3 Phase 3 unit tests for the `cloudwatch` source's COST read path.

The cost path is opt-in (active only when `cost_budget_name` is configured) and emits
`panoptes_cost_spend{env,service}` (one per service, from CE `GetCostAndUsage`) + one
`panoptes_cost_budget_burn{env}` gauge (actual/limit, from budgets `DescribeBudget`). It is
a SEPARATE concern from the metric/log feeds, so it lives in its own module.

Covers (plan Phase 3 → cost source + cost-burn discipline G3):
- cost OFF by default — no `cost_budget_name` ⇒ NO CE/budgets call, NO cost gauges;
- cost ON — recorded CE + budgets payloads → exact per-service spend gauges + a burn gauge,
  every gauge `env`-stamped with the exact `{env,service}` / `{env}` label set;
- **cadence (G3)** — the CE/budgets calls fire at most ONCE per `cost_poll_interval_seconds`:
  a second `fetch` within the interval (driven by an INJECTED fake clock, no wall-clock
  `sleep`) issues NO CE/budgets call and emits no fresh cost gauges, while the cheap
  metric/log feeds still run every cycle; once the interval elapses the read fires again;
- a CE/budgets transport error is swallowed (non-fatal) and does NOT advance the cadence
  marker (so the next cycle retries) — it must never crash the poll cycle.

boto3 is exercised entirely through `botocore.stub.Stubber` against injected clients (NO live
AWS). The clock is an injected fake so the cadence is asserted deterministically.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import boto3
from botocore.stub import Stubber
from core.model import MetricSignal, TimeWindow
from core.registry import ConfigValue
from core.sources.cloudwatch import CloudWatchSource

if TYPE_CHECKING:
    # Type-stub-only imports (boto3-stubs is a dev dep): present at type-check time, never at
    # slim-CI runtime — guarded so a bare runtime import can't crash.
    from mypy_boto3_budgets import BudgetsClient
    from mypy_boto3_ce import CostExplorerClient
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_sts import STSClient

_REGION = "us-east-1"
_NAMESPACE = "AWS/ApplicationELB"
_ENV = "dev"
_METRIC_NAMES = ["RequestCount"]
_LOG_GROUPS = ["/app/api"]
_BUDGET_NAME = "panoptes-monthly-budget"
_ACCOUNT_ID = "123456789012"
_POLL_INTERVAL_SECONDS = 3600

_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
)

# The fake clock's starting instant (the cadence math is relative to this).
_CLOCK_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _FakeClock:
    """A monotonic, manually-advanced UTC clock — the injected `clock` seam (no wall time).

    `now()` returns the current instant; `advance(seconds)` moves it forward. Lets the cadence
    test assert the once-per-interval gate without a real `time.sleep`.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _config(**overrides: ConfigValue) -> dict[str, ConfigValue]:
    base: dict[str, ConfigValue] = {
        "region": _REGION,
        "namespace": _NAMESPACE,
        "metric_names": _METRIC_NAMES,
        "log_groups": _LOG_GROUPS,
        "env": _ENV,
    }
    base.update(overrides)
    return base


def _cloudwatch_client() -> "CloudWatchClient":
    return boto3.client("cloudwatch", region_name=_REGION)


def _logs_client() -> "CloudWatchLogsClient":
    return boto3.client("logs", region_name=_REGION)


def _ce_client() -> "CostExplorerClient":
    # CE is global (us-east-1); the stub never makes a network call.
    return boto3.client("ce", region_name="us-east-1")


def _budgets_client() -> "BudgetsClient":
    return boto3.client("budgets", region_name="us-east-1")


def _sts_client() -> "STSClient":
    return boto3.client("sts", region_name=_REGION)


def _empty_feeds(cw_stub: Stubber, logs_stub: Stubber) -> None:
    """Queue one empty metrics page + one empty logs page so the non-cost feeds are quiet.

    The cost tests focus on the cost gauges, so the metric/log feeds return nothing — but
    they STILL run every cycle (that is the whole point of the cadence gate), so each `fetch`
    needs exactly one empty metrics response and one empty logs response queued.
    """
    cw_stub.add_response("get_metric_data", {"MetricDataResults": []})
    logs_stub.add_response("filter_log_events", {"events": []})


def _add_cost_responses(ce_stub: Stubber, budgets_stub: Stubber, sts_stub: Stubber) -> None:
    """Queue one CE GetCostAndUsage + one budgets DescribeBudget + one STS identity response.

    CE: two services (EC2 $120.50, S3 $30.25). Budget: limit $1000, actual $420 ⇒ burn 0.42.
    STS: the account id DescribeBudget needs as its partition key.
    """
    sts_stub.add_response(
        "get_caller_identity",
        {"Account": _ACCOUNT_ID, "Arn": f"arn:aws:iam::{_ACCOUNT_ID}:user/panoptes", "UserId": "U"},
    )
    ce_stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2025-12-02", "End": "2026-01-01"},
                    "Total": {},
                    "Groups": [
                        {
                            "Keys": ["AmazonEC2"],
                            "Metrics": {"UnblendedCost": {"Amount": "120.50", "Unit": "USD"}},
                        },
                        {
                            "Keys": ["AmazonS3"],
                            "Metrics": {"UnblendedCost": {"Amount": "30.25", "Unit": "USD"}},
                        },
                    ],
                    "Estimated": False,
                }
            ]
        },
    )
    budgets_stub.add_response(
        "describe_budget",
        {
            "Budget": {
                "BudgetName": _BUDGET_NAME,
                "BudgetLimit": {"Amount": "1000.0", "Unit": "USD"},
                "CalculatedSpend": {"ActualSpend": {"Amount": "420.0", "Unit": "USD"}},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
            }
        },
    )


def test_cost_path_off_by_default_makes_no_ce_or_budgets_call() -> None:
    """Without `cost_budget_name`, fetch emits NO cost gauges and touches NO CE/budgets client.

    The Stubbers on CE/budgets have ZERO queued responses; if the source called either client
    the stub would raise. The cost path must be fully opt-in.
    """
    cw = _cloudwatch_client()
    logs = _logs_client()
    ce = _ce_client()
    budgets = _budgets_client()
    cw_stub, logs_stub = Stubber(cw), Stubber(logs)
    ce_stub, budgets_stub = Stubber(ce), Stubber(budgets)
    _empty_feeds(cw_stub, logs_stub)
    cw_stub.activate()
    logs_stub.activate()
    # No cost responses queued — a call would raise StubAssertionError.
    ce_stub.activate()
    budgets_stub.activate()

    source = CloudWatchSource(
        _config(),  # cost_budget_name absent ⇒ cost off
        cloudwatch_client=cw,
        logs_client=logs,
        ce_client=ce,
        budgets_client=budgets,
    )
    signals = source.fetch(_WINDOW)

    cost_signals = [
        s for s in signals if isinstance(s, MetricSignal) and s.name.startswith("panoptes_cost_")
    ]
    assert cost_signals == []
    # Neither cost client was touched.
    ce_stub.assert_no_pending_responses()
    budgets_stub.assert_no_pending_responses()


def test_cost_path_emits_per_service_spend_and_budget_burn_gauges() -> None:
    """With cost configured, fetch emits exact per-service spend + a burn gauge, env-stamped."""
    cw, logs = _cloudwatch_client(), _logs_client()
    ce, budgets, sts = _ce_client(), _budgets_client(), _sts_client()
    cw_stub, logs_stub = Stubber(cw), Stubber(logs)
    ce_stub, budgets_stub, sts_stub = Stubber(ce), Stubber(budgets), Stubber(sts)
    _empty_feeds(cw_stub, logs_stub)
    _add_cost_responses(ce_stub, budgets_stub, sts_stub)
    for stub in (cw_stub, logs_stub, ce_stub, budgets_stub, sts_stub):
        stub.activate()

    clock = _FakeClock(_CLOCK_START)
    source = CloudWatchSource(
        _config(cost_budget_name=_BUDGET_NAME),
        cloudwatch_client=cw,
        logs_client=logs,
        ce_client=ce,
        budgets_client=budgets,
        sts_client=sts,
        clock=clock.now,
    )
    signals = source.fetch(_WINDOW)

    spend = {
        s.labels["service"]: s.value
        for s in signals
        if isinstance(s, MetricSignal) and s.name == "panoptes_cost_spend"
    }
    assert spend == {"AmazonEC2": 120.50, "AmazonS3": 30.25}
    # Exact spend-gauge label set: {env, service}, env-stamped.
    for s in signals:
        if isinstance(s, MetricSignal) and s.name == "panoptes_cost_spend":
            assert set(s.labels) == {"env", "service"}
            assert s.labels["env"] == _ENV

    burn = [
        s for s in signals if isinstance(s, MetricSignal) and s.name == "panoptes_cost_budget_burn"
    ]
    assert len(burn) == 1
    assert burn[0].value == 0.42  # 420 / 1000
    # Exact burn-gauge label set: {env}.
    assert set(burn[0].labels) == {"env"}
    assert burn[0].labels["env"] == _ENV

    for stub in (ce_stub, budgets_stub, sts_stub):
        stub.assert_no_pending_responses()


def test_cost_read_fires_at_most_once_per_poll_interval() -> None:
    """G3 cadence: a 2nd fetch within the interval issues no CE/budgets call; fires once elapsed.

    The CE/budgets stubs are queued for EXACTLY TWO reads (cycle 1 + the post-interval cycle).
    The metric/log feeds are queued for THREE cycles (they run every cycle). The fake clock is
    advanced under the interval for cycle 2 (cost suppressed) and past it for cycle 3 (cost
    fires). No wall-clock sleep is used.

    Steps:
        1. Build a source with the cost path on and an injected fake clock.
        2. Cycle 1 — fetch: cost fires (first read is always due) ⇒ cost gauges present.
        3. Advance the clock by less than the interval; cycle 2 — fetch: cost gauges ABSENT
           (the cadence gate is closed), and NO CE/budgets response is consumed.
        4. Advance the clock past the interval; cycle 3 — fetch: cost fires again ⇒ gauges
           present. All cost stubs are then exhausted (exactly two reads total).
    """
    cw, logs = _cloudwatch_client(), _logs_client()
    ce, budgets, sts = _ce_client(), _budgets_client(), _sts_client()
    cw_stub, logs_stub = Stubber(cw), Stubber(logs)
    ce_stub, budgets_stub, sts_stub = Stubber(ce), Stubber(budgets), Stubber(sts)

    # The cheap feeds run every cycle → three empty pages each.
    for _ in range(3):
        _empty_feeds(cw_stub, logs_stub)
    # The cost read fires only on cycle 1 and cycle 3 → exactly two CE/budgets/STS reads.
    for _ in range(2):
        _add_cost_responses(ce_stub, budgets_stub, sts_stub)
    for stub in (cw_stub, logs_stub, ce_stub, budgets_stub, sts_stub):
        stub.activate()

    clock = _FakeClock(_CLOCK_START)
    source = CloudWatchSource(
        _config(cost_budget_name=_BUDGET_NAME, cost_poll_interval_seconds=_POLL_INTERVAL_SECONDS),
        cloudwatch_client=cw,
        logs_client=logs,
        ce_client=ce,
        budgets_client=budgets,
        sts_client=sts,
        clock=clock.now,
    )

    def _has_cost(signals: list[object]) -> bool:
        return any(
            isinstance(s, MetricSignal) and s.name.startswith("panoptes_cost_") for s in signals
        )

    # Cycle 1 — first read is always due.
    assert _has_cost(list(source.fetch(_WINDOW))) is True

    # Cycle 2 — within the interval: cost suppressed (gate closed).
    clock.advance(_POLL_INTERVAL_SECONDS - 1)
    assert _has_cost(list(source.fetch(_WINDOW))) is False

    # Cycle 3 — past the interval: cost fires again.
    clock.advance(2)  # now strictly past the interval since cycle-1's read
    assert _has_cost(list(source.fetch(_WINDOW))) is True

    # Exactly two cost reads consumed — the cadence gate suppressed the middle cycle.
    for stub in (ce_stub, budgets_stub, sts_stub):
        stub.assert_no_pending_responses()


def test_cost_transport_error_is_swallowed_and_cadence_not_advanced() -> None:
    """A CE error is non-fatal: fetch returns (no cost gauges), and the next cycle RETRIES.

    The cadence marker must only advance on a SUCCESSFUL read, so a failed call does not black
    out cost for a whole interval. Cycle 1 errors on CE (no gauges, no crash); cycle 2 — still
    within the interval but due because the marker never advanced — succeeds.

    Steps:
        1. Queue a CE client error for cycle 1, then a full successful cost read for cycle 2.
        2. Cycle 1 — fetch: no crash, no cost gauges (the error was swallowed).
        3. Advance the clock by less than the interval; cycle 2 — fetch: cost fires anyway
           (the failed read never advanced the cadence marker), gauges present.
    """
    cw, logs = _cloudwatch_client(), _logs_client()
    ce, budgets, sts = _ce_client(), _budgets_client(), _sts_client()
    cw_stub, logs_stub = Stubber(cw), Stubber(logs)
    ce_stub, budgets_stub, sts_stub = Stubber(ce), Stubber(budgets), Stubber(sts)

    for _ in range(2):
        _empty_feeds(cw_stub, logs_stub)

    # Cycle 1: CE errors on the FIRST cost call (spend) — `_fetch_budget_burn` (and thus its
    # STS GetCallerIdentity) is never reached, so no cycle-1 STS/budgets responses are queued.
    ce_stub.add_client_error("get_cost_and_usage", "ThrottlingException", "rate exceeded")
    # Cycle 2: a full successful read (STS identity + CE + budgets).
    _add_cost_responses(ce_stub, budgets_stub, sts_stub)

    for stub in (cw_stub, logs_stub, ce_stub, budgets_stub, sts_stub):
        stub.activate()

    clock = _FakeClock(_CLOCK_START)
    source = CloudWatchSource(
        _config(cost_budget_name=_BUDGET_NAME, cost_poll_interval_seconds=_POLL_INTERVAL_SECONDS),
        cloudwatch_client=cw,
        logs_client=logs,
        ce_client=ce,
        budgets_client=budgets,
        sts_client=sts,
        clock=clock.now,
    )

    def _cost_count(signals: list[object]) -> int:
        return sum(
            1
            for s in signals
            if isinstance(s, MetricSignal) and s.name.startswith("panoptes_cost_")
        )

    # Cycle 1 — CE error swallowed: no crash, no cost gauges.
    assert _cost_count(list(source.fetch(_WINDOW))) == 0

    # Cycle 2 — WITHIN the interval, but due because the failed read didn't advance the marker.
    clock.advance(_POLL_INTERVAL_SECONDS - 1)
    assert _cost_count(list(source.fetch(_WINDOW))) > 0

    for stub in (ce_stub, budgets_stub, sts_stub):
        stub.assert_no_pending_responses()
