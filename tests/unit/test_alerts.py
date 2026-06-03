"""Unit tests for declarative alert rules (`core/alerts.py`).

Covers (spec § Alert-rule model / plan Phase 3):
- each `Comparison` (gt/ge/lt/le) breaches and does NOT breach at the boundary;
- `evaluate` returns an `Alert` carrying `rule.labels` + `env` on a breach, `None` else;
- a store that cannot answer (CapabilityError / no series) is a NON-breach (never raises);
- the `for_cycles` debounce: `AlertState` fires only after N consecutive breaches and
  resolves on the first non-breach cycle — driven by N method calls, NO wall-clock sleep;
- `envs:["all"]` expands to every enabled env.

The store is a tiny in-test fake; the `AlertState` debounce is driven by repeated calls
(the collector holds the state across cycles — there is no sleep anywhere).
"""

from datetime import UTC, datetime

from core.alerts import (
    AlertRule,
    AlertState,
    Comparison,
    evaluate,
    rule_applies_to_env,
)
from core.errors import CapabilityError
from core.model import Alert, CanonicalSignal, MetricQuery, MetricSeries


def _now() -> datetime:
    return datetime.now(UTC)


class _ValueStore:
    """A fake store returning one series at `value` for the env carried in its labels.

    The series carries an `env` label so `evaluate` can scope the latest value to the
    target env (metrics already carry their own `env` label).
    """

    type = "value"

    def __init__(self, value: float, *, env: str = "dev") -> None:
        self._value = value
        self._env = env

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return [
            MetricSeries(
                metric="panoptes_k8s_pods_crashloop",
                labels={"env": self._env},
                points=[(_now(), self._value)],
            )
        ]


class _EmptyStore:
    """A fake store returning no series (a legitimate 'no data' answer)."""

    type = "empty"

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []


class _CapabilityErrorStore:
    """A fake store that cannot answer PromQL (mirrors the passthrough store)."""

    type = "passthrough"

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        raise CapabilityError("this store cannot answer PromQL queries")


def _rule(
    *,
    comparison: Comparison,
    threshold: float,
    for_cycles: int = 1,
    envs: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> AlertRule:
    return AlertRule(
        name="crashloop-high",
        expr="panoptes_k8s_pods_crashloop",
        comparison=comparison,
        threshold=threshold,
        for_cycles=for_cycles,
        severity="critical",
        envs=envs if envs is not None else ["dev"],
        labels=labels if labels is not None else {"team": "platform"},
    )


# --- each Comparison: breach + boundary non-breach --------------------------------


def test_gt_breaches_above_threshold_not_at_boundary() -> None:
    rule = _rule(comparison=Comparison.GT, threshold=5.0)
    # 6 > 5 → breach.
    assert evaluate(rule, "dev", _ValueStore(6.0)) is not None
    # 5 > 5 is False → no breach at the boundary.
    assert evaluate(rule, "dev", _ValueStore(5.0)) is None


def test_ge_breaches_at_and_above_threshold() -> None:
    rule = _rule(comparison=Comparison.GE, threshold=5.0)
    assert evaluate(rule, "dev", _ValueStore(5.0)) is not None  # 5 >= 5 → breach
    assert evaluate(rule, "dev", _ValueStore(4.0)) is None  # 4 >= 5 is False


def test_lt_breaches_below_threshold_not_at_boundary() -> None:
    rule = _rule(comparison=Comparison.LT, threshold=1.0)
    assert evaluate(rule, "dev", _ValueStore(0.0)) is not None  # 0 < 1 → breach
    assert evaluate(rule, "dev", _ValueStore(1.0)) is None  # 1 < 1 is False


def test_le_breaches_at_and_below_threshold() -> None:
    rule = _rule(comparison=Comparison.LE, threshold=1.0)
    assert evaluate(rule, "dev", _ValueStore(1.0)) is not None  # 1 <= 1 → breach
    assert evaluate(rule, "dev", _ValueStore(2.0)) is None  # 2 <= 1 is False


# --- the produced Alert carries rule.labels + env ---------------------------------


def test_breach_alert_carries_rule_labels_and_env() -> None:
    rule = _rule(comparison=Comparison.GT, threshold=0.0, labels={"team": "platform"})
    alert = evaluate(rule, "dev", _ValueStore(3.0))
    assert isinstance(alert, Alert)
    assert alert.name == "crashloop-high"
    assert alert.severity == "critical"
    # The rule's labels are carried, plus env is stamped.
    assert alert.labels["team"] == "platform"
    assert alert.labels["env"] == "dev"
    # The message is a rendered string mentioning the breach value/threshold.
    assert "panoptes_k8s_pods_crashloop" in alert.message


# --- store-can't-answer / no-data is a non-breach (never raises) ------------------


def test_capability_error_store_is_non_breach_not_raise() -> None:
    """A store that raises CapabilityError is treated as a non-breach, never propagated."""
    rule = _rule(comparison=Comparison.GT, threshold=0.0)
    # GT 0 would breach on any positive value, but the store cannot answer → non-breach.
    assert evaluate(rule, "dev", _CapabilityErrorStore()) is None


def test_empty_series_is_non_breach() -> None:
    """No series for the env → non-breach (no data is not a breach)."""
    rule = _rule(comparison=Comparison.GT, threshold=0.0)
    assert evaluate(rule, "dev", _EmptyStore()) is None


def test_value_for_other_env_is_non_breach() -> None:
    """A series for a DIFFERENT env does not breach the target env's rule."""
    rule = _rule(comparison=Comparison.GT, threshold=0.0)
    # The store returns a series labelled env=stage; evaluating for dev finds no dev value.
    assert evaluate(rule, "dev", _ValueStore(9.0, env="stage")) is None


# --- for_cycles debounce via AlertState (NO sleep) --------------------------------


def test_for_cycles_debounce_fires_only_after_n_consecutive_breaches() -> None:
    """`AlertState` fires at the Nth consecutive breach, not before — no wall-clock sleep."""
    state = AlertState()
    # for_cycles=3 → the first two breaches do NOT fire; the third does.
    assert state.record_breach(for_cycles=3) is False  # cycle 1 breach
    assert state.record_breach(for_cycles=3) is False  # cycle 2 breach
    assert state.record_breach(for_cycles=3) is True  # cycle 3 breach → FIRE
    # A subsequent breach while already firing does NOT re-fire (already active).
    assert state.record_breach(for_cycles=3) is False


def test_for_cycles_one_fires_immediately() -> None:
    """The default `for_cycles=1` fires on the first breach."""
    state = AlertState()
    assert state.record_breach(for_cycles=1) is True


def test_non_breach_before_threshold_resets_the_counter() -> None:
    """A non-breach cycle before firing resets the consecutive-breach counter."""
    state = AlertState()
    assert state.record_breach(for_cycles=3) is False  # cycle 1 breach
    # A non-breach resolves nothing (was not firing) but resets the counter.
    assert state.record_non_breach() is False  # no resolve transition
    # The counter restarted: two more breaches still do not fire (need 3 consecutive).
    assert state.record_breach(for_cycles=3) is False  # 1
    assert state.record_breach(for_cycles=3) is False  # 2
    assert state.record_breach(for_cycles=3) is True  # 3 → FIRE


def test_resolve_fires_once_on_the_transition_from_firing_to_non_breach() -> None:
    """A non-breach cycle AFTER firing returns a single resolve transition."""
    state = AlertState()
    state.record_breach(for_cycles=1)  # fires
    # First non-breach after firing → resolve transition (True).
    assert state.record_non_breach() is True
    # A second consecutive non-breach is NOT another resolve (already resolved).
    assert state.record_non_breach() is False


# --- envs:["all"] expansion -------------------------------------------------------


def test_rule_applies_to_env_explicit_list() -> None:
    rule = _rule(comparison=Comparison.GT, threshold=0.0, envs=["dev", "stage"])
    assert rule_applies_to_env(rule, "dev") is True
    assert rule_applies_to_env(rule, "stage") is True
    assert rule_applies_to_env(rule, "prod") is False


def test_rule_applies_to_all_envs_with_all_sentinel() -> None:
    """`envs:["all"]` applies to EVERY env (the all-sentinel)."""
    rule = _rule(comparison=Comparison.GT, threshold=0.0, envs=["all"])
    assert rule_applies_to_env(rule, "dev") is True
    assert rule_applies_to_env(rule, "stage") is True
    assert rule_applies_to_env(rule, "any-env-name") is True
