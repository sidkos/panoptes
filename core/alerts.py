"""Declarative alert rules — the model + per-cycle evaluation the collector drives.

A `AlertRule` is a PromQL threshold rule the operator declares in config; the collector
evaluates each enabled rule once per cycle against the live store (spec § Alert-rule
model). Evaluation lives in the collector — not a separate daemon — because the collector
already runs each cycle over every enabled env with a live store handle, so a rule is one
extra read-query per cycle (no new scheduler, no alert-manager pod).

The pieces:

- `Comparison` — the threshold operator (`gt`/`ge`/`lt`/`le`).
- `AlertRule` (frozen) — `name`, `expr` (PromQL), `comparison`, `threshold`, `for_cycles`
  (debounce, default 1), `severity`, `envs` (`["all"]` = every enabled env), `labels`.
- `AlertState` — the per-`(rule, env)` consecutive-breach counter the collector holds
  ACROSS cycles (the same lifetime pattern as the collector's `_FailureState` throttle
  counter): it fires when the breach run reaches `for_cycles`, and resolves on the first
  non-breach cycle after firing. No wall-clock sleep is involved anywhere — a test drives
  N cycles by calling `record_breach`/`record_non_breach` N times.
- `evaluate(rule, env, store) -> Alert | None` — the pure per-cycle breach test: query the
  store for `rule.expr` scoped to `env`, take the latest value, compare to `threshold`, and
  return an `Alert` (carrying `rule.labels` + `env`) on a breach, else `None`. A store that
  cannot answer (a `CapabilityError`, or no series for the env) is treated as a NON-breach
  — it never raises into the cycle (the collector's resilience boundary still wraps it as
  defense in depth).

The "should this rule fire/resolve THIS cycle" decision is split across two units on
purpose: `evaluate` is the stateless single-cycle breach test (easy to unit-test per
comparison), and `AlertState` is the stateful debounce/transition tracker (easy to
unit-test by repeated calls). The collector composes them.
"""

import enum
import operator
from collections.abc import Callable
from dataclasses import dataclass

from core.errors import CapabilityError, PanoptesError
from core.model import Alert, Labels, MetricQuery, MetricSeries, TimeWindow
from core.planes.store import Store

# The trailing window each rule evaluation queries (mirrors the collector fetch window +
# the MCP tools' default): a rule reads the latest value over the trailing 15 minutes.
_EVAL_WINDOW_MINUTES = 15
# A sane sub-window step so the range query returns points rather than one degenerate
# bucket (mirrors the MCP `_step_seconds_for` floor).
_EVAL_STEP_SECONDS = 15

# The `envs` sentinel meaning "every enabled env" (spec § Alert-rule model).
_ALL_ENVS = "all"

# The mandatory env label every signal/series carries (used to scope a rule to one env).
_ENV_LABEL = "env"


class Comparison(enum.Enum):
    """The threshold operator of an `AlertRule` (spec § Alert-rule model)."""

    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"


# Map each `Comparison` to its `operator` function (value, threshold) -> bool. Kept as a
# module table so `evaluate` is a single lookup + call, not a branch ladder.
_COMPARATORS: dict[Comparison, Callable[[float, float], bool]] = {
    Comparison.GT: operator.gt,
    Comparison.GE: operator.ge,
    Comparison.LT: operator.lt,
    Comparison.LE: operator.le,
}


@dataclass(frozen=True)
class AlertRule:
    """A declarative PromQL threshold rule (spec § Alert-rule model).

    `envs == ["all"]` means "every enabled env"; otherwise the rule applies only to the
    listed env names. `for_cycles` is the debounce: the rule fires only after that many
    CONSECUTIVE breaching cycles (default 1 = fire on the first breach). `labels` are
    carried onto every fired `Alert` (with `env` stamped on at evaluation time).
    """

    name: str
    expr: str
    comparison: Comparison
    threshold: float
    for_cycles: int
    severity: str
    envs: list[str]
    labels: Labels


def rule_applies_to_env(rule: AlertRule, env: str) -> bool:
    """Whether `rule` should be evaluated for `env`.

    `["all"]` applies to every env; otherwise the env must be in the rule's explicit list.
    """
    if _ALL_ENVS in rule.envs:
        return True
    return env in rule.envs


class AlertState:
    """The per-`(rule, env)` consecutive-breach + firing tracker the collector holds.

    Lifetime mirrors the collector's `_FailureState` throttle counter: one instance per
    `(rule, env)` pair, persisted on the collector ACROSS `run_once()` cycles, so the
    debounce + fire/resolve transitions advance one cycle per call with NO wall-clock
    sleep. The collector keys a dict of these by `(rule.name, env)`.

    State machine:
    - `record_breach(for_cycles)` increments the consecutive-breach counter and returns
      `True` exactly once — on the cycle the counter first REACHES `for_cycles` (the FIRE
      transition). While already firing it returns `False` (no re-fire).
    - `record_non_breach()` resets the breach counter; if the rule WAS firing it returns
      `True` exactly once (the RESOLVE transition) and clears the firing flag, else `False`.
    """

    def __init__(self) -> None:
        # Consecutive breaching cycles observed since the last non-breach.
        self._consecutive_breaches = 0
        # Whether the rule is currently in the FIRING state (between fire and resolve).
        self._firing = False

    def record_breach(self, for_cycles: int) -> bool:
        """Record a breaching cycle; return True ONLY on the fire transition.

        Args:
            for_cycles: The rule's debounce — fire once the consecutive-breach run reaches
                this many cycles (a `for_cycles <= 0` is normalized to 1 so a misconfigured
                rule still fires on the first breach rather than never).

        Returns:
            `True` exactly on the cycle the counter first reaches the debounce threshold
            (the rule transitions to firing); `False` on earlier breaches and on every
            breach while already firing (no duplicate fire).
        """
        threshold = for_cycles if for_cycles >= 1 else 1
        self._consecutive_breaches += 1
        if self._firing:
            # Already firing — a continued breach is not a new fire transition.
            return False
        if self._consecutive_breaches >= threshold:
            self._firing = True
            return True
        return False

    def record_non_breach(self) -> bool:
        """Record a non-breaching cycle; return True ONLY on the resolve transition.

        Resets the consecutive-breach counter unconditionally. Returns `True` exactly once
        — on the first non-breach cycle after the rule had been firing (the resolve
        transition), clearing the firing flag — else `False`.
        """
        self._consecutive_breaches = 0
        if self._firing:
            self._firing = False
            return True
        return False


def evaluate(rule: AlertRule, env: str, store: Store) -> Alert | None:
    """Evaluate `rule` for `env` against `store` for the CURRENT cycle.

    Queries the store for `rule.expr`, keeps the series scoped to `env` (metrics carry
    their own `env` label), takes the latest value across them, and compares to
    `rule.threshold` per `rule.comparison`. Returns an `Alert` (carrying `rule.labels` +
    `env` + a rendered message) when the current cycle breaches, else `None`.

    A store that cannot answer the query (raises `CapabilityError` — e.g. a passthrough
    store — or any `PanoptesError`), or returns no series for the env, is treated as a
    NON-breach: evaluation NEVER raises into the cycle. The fire/resolve debounce is the
    `AlertState`'s job; this is the stateless single-cycle breach test.

    Args:
        rule: The alert rule to evaluate.
        env: The environment to scope the evaluation to.
        store: The metric store answering the PromQL query.

    Returns:
        An `Alert` on a breach this cycle, else `None`.
    """
    value = _latest_value_for_env(rule, env, store)
    if value is None:
        # No data for the env (or the store cannot answer) — not a breach.
        return None
    comparator = _COMPARATORS[rule.comparison]
    if not comparator(value, rule.threshold):
        return None
    # Breach: build the Alert carrying the rule's labels plus the evaluated env.
    labels: dict[str, str] = {**rule.labels, _ENV_LABEL: env}
    message = (
        f"{rule.expr} = {value} {rule.comparison.value} threshold {rule.threshold} in env {env}"
    )
    return Alert(name=rule.name, severity=rule.severity, message=message, labels=labels)


def _latest_value_for_env(rule: AlertRule, env: str, store: Store) -> float | None:
    """Query the store for `rule.expr` and return the latest value scoped to `env`.

    Returns `None` when the store cannot answer (a `PanoptesError`/`CapabilityError`, e.g.
    a passthrough store) OR when there is no series carrying `env=<env>` — both are
    legitimate "no breach signal" states the caller treats as a non-breach. Series are
    filtered to the target env (metrics carry their own `env` label) so a single rule expr
    evaluated per env reads only that env's value.
    """
    window = TimeWindow.last(minutes=_EVAL_WINDOW_MINUTES)
    query = MetricQuery(expr=rule.expr, window=window, step_seconds=_EVAL_STEP_SECONDS)
    try:
        series = store.query(query)
    except (CapabilityError, PanoptesError):
        # A store that cannot answer PromQL (passthrough) — treat as no-data, never raise.
        return None
    return _latest_value(series, env)


def _latest_value(series: list[MetricSeries], env: str) -> float | None:
    """The most-recent sample value across the series whose `env` label matches `env`.

    Returns `None` when no series for the env has any point — distinct from a real `0.0`
    value, so the caller does not invent a breach from missing data.
    """
    latest_timestamp = None
    latest_value: float | None = None
    for one_series in series:
        # Only series for the target env contribute (a rule is evaluated per env; metrics
        # carry their own `env` label, so cross-env series are filtered out here).
        if one_series.labels.get(_ENV_LABEL) != env:
            continue
        for timestamp, value in one_series.points:
            if latest_timestamp is None or timestamp >= latest_timestamp:
                latest_timestamp = timestamp
                latest_value = value
    return latest_value
