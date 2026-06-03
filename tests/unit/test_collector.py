"""Phase 4 unit tests for the collector loop (`core/collector.py`).

Covers (spec `## Tests` → Collector; playbook Phase 4 table):
- per-env per-source dispatch — every enabled env's every source is fetched and its
  signals reach the store;
- a **disabled env is skipped** entirely (no health/fetch/write for its sources);
- one source failing `health()` **does not abort the run** — sibling sources + the
  next env are still fetched and written;
- a `store.write()` raising (e.g. VM unreachable) is **caught per-batch, logged, and
  the loop continues** to the next env;
- a `fetch` exceeding its `fetch_timeout_seconds` is **abandoned** for that cycle
  (its signals never reach the store) and the loop continues — driven with a tiny
  timeout + a short bounded wait, NOT a long wall-clock sleep;
- a perpetually-failing source is **log-throttled** — driving `M` cycles via
  `run_once()` on ONE collector (no wall-clock sleep) bounds the error-log call
  count (after `K=3` failures, once per `N=10` cycles);
- `--once` runs exactly one cycle (the scheduled loop is driven via an injected
  sleep so the test never actually sleeps).

All fakes implement the plane Protocols directly. The collector is constructed from
a `ResolvedConfig` assembled in-test (no YAML), with injected logger/clock seams.
"""

import logging
import threading
from datetime import UTC, datetime

import pytest
from core.alerts import AlertRule, Comparison
from core.collector import Collector
from core.config import ResolvedConfig, ResolvedEnvironment, ResolvedSource
from core.model import (
    Alert,
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier


def _now() -> datetime:
    return datetime.now(UTC)


def _metric(env: str, name: str) -> MetricSignal:
    return MetricSignal(name=name, value=1.0, timestamp=_now(), labels={"env": env})


class _FakeSource:
    """A `Source` that emits one named metric per fetch and is always healthy."""

    # The default for the `Source` Protocol's outage-fetch opt-in: most sources treat
    # `reachable=False` as "no usable signal, skip the fetch". http-health overrides
    # this to True because its outage IS the signal (panoptes_health_up=0).
    fetch_when_unreachable = False

    def __init__(self, env: str, type_name: str) -> None:
        self.type = type_name
        self._env = env
        self.fetch_calls = 0
        self.health_calls = 0

    def capabilities(self) -> set[SignalKind]:
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        return [_metric(self._env, f"panoptes_{self.type}_count")]

    def health(self) -> SourceHealth:
        self.health_calls += 1
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _HealthFailingSource(_FakeSource):
    """A `Source` whose `health()` always raises — must not abort the run."""

    def health(self) -> SourceHealth:
        self.health_calls += 1
        raise RuntimeError("health probe blew up")


class _FetchFailingSource(_FakeSource):
    """A `Source` whose `fetch()` always raises — drives the throttle counter."""

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        raise RuntimeError("upstream unreachable")


class _UnreachableSource(_FakeSource):
    """A `Source` whose `health()` RETURNS reachable=False (does not raise).

    Mirrors a CloudWatch assume-role denial: the source surfaces a credential failure as
    an unreachable health result, not an exception. The collector must skip its fetch and
    keep its signals out of the store (F2k).
    """

    def health(self) -> SourceHealth:
        self.health_calls += 1
        return SourceHealth(
            reachable=False, detail="AccessDenied: not authorized", checked_at=_now()
        )


class _OutageSignalSource(_FakeSource):
    """A `Source` mirroring http-health: health() returns reachable=False during an
    outage, but fetch() deliberately emits the down signal (panoptes_health_up=0.0).

    `fetch_when_unreachable=True` tells the collector the unreachable state IS the
    signal, so it must still run the fetch and let the `0` reach the store (F3a).
    """

    fetch_when_unreachable = True

    def health(self) -> SourceHealth:
        self.health_calls += 1
        return SourceHealth(
            reachable=False, detail="endpoint down (latency 12.0ms)", checked_at=_now()
        )

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        return [
            MetricSignal(
                name="panoptes_health_up", value=0.0, timestamp=_now(), labels={"env": self._env}
            )
        ]


class _SlowSource(_FakeSource):
    """A `Source` whose `fetch()` blocks until released — drives the timeout bound."""

    def __init__(self, env: str, type_name: str, release: threading.Event) -> None:
        super().__init__(env, type_name)
        self._release = release

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        # Block on the event with a SHORT ceiling (0.2s) so the worker self-releases
        # quickly and the per-cycle executor's shutdown(wait=True) at run_once() exit is
        # bounded to a fraction of a second. The timeout-abandonment assertions still
        # hold: fetch_timeout_seconds=0 abandons the future immediately, so the slow
        # signals never reach the store regardless of this ceiling — the ceiling only
        # bounds teardown, it is not the thing under test (F3d).
        self._release.wait(timeout=0.2)
        return [_metric(self._env, "panoptes_slow_count")]


class _RecordingStore:
    """A `Store` that records every batch handed to `write`."""

    type = "recording"

    def __init__(self) -> None:
        self.batches: list[list[CanonicalSignal]] = []

    def write(self, signals: list[CanonicalSignal]) -> None:
        self.batches.append(signals)

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []

    def written_names(self) -> set[str]:
        names: set[str] = set()
        for batch in self.batches:
            for signal in batch:
                if isinstance(signal, MetricSignal):
                    names.add(signal.name)
        return names


class _WriteFailingStore(_RecordingStore):
    """A `Store` whose `write` always raises — must be caught per-batch."""

    def write(self, signals: list[CanonicalSignal]) -> None:
        raise RuntimeError("VM unreachable")


class _FakeNotifier:
    """A `Notifier` recording every `Alert` it is handed (for the alert-wiring tests)."""

    type = "logging"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)


def _config(
    environments: dict[str, ResolvedEnvironment],
    store: _RecordingStore,
    *,
    notifiers: list[_FakeNotifier] | None = None,
    alerts: list[AlertRule] | None = None,
) -> ResolvedConfig:
    # Annotate the local as `list[Notifier]` so the invariant `ResolvedConfig.notifiers`
    # field accepts our `_FakeNotifier`s (the structural-Protocol element widens here).
    notifier_list: list[Notifier] = list(notifiers) if notifiers is not None else [_FakeNotifier()]
    return ResolvedConfig(
        environments=environments,
        store=store,
        notifiers=notifier_list,
        dashboard_packs=[],
        slos=[],
        alerts=alerts if alerts is not None else [],
        mcp={},
    )


def _enabled_env(name: str, sources: list[ResolvedSource]) -> ResolvedEnvironment:
    return ResolvedEnvironment(name=name, enabled=True, sources=sources)


def _resolved(source: _FakeSource, *, fetch_timeout_seconds: int = 30) -> ResolvedSource:
    return ResolvedSource(
        source=source,
        fetch_timeout_seconds=fetch_timeout_seconds,
        poll_interval_seconds=60,
    )


def test_run_once_dispatches_each_source_and_writes_to_store() -> None:
    store = _RecordingStore()
    cloudwatch = _FakeSource("dev", "cloudwatch")
    sentry = _FakeSource("dev", "sentry")
    env = _enabled_env("dev", [_resolved(cloudwatch), _resolved(sentry)])
    collector = Collector(_config({"dev": env}, store))

    collector.run_once()

    assert cloudwatch.fetch_calls == 1
    assert sentry.fetch_calls == 1
    assert store.written_names() == {"panoptes_cloudwatch_count", "panoptes_sentry_count"}


def test_run_once_skips_disabled_environment() -> None:
    store = _RecordingStore()
    live = _FakeSource("dev", "cloudwatch")
    inert = _FakeSource("stage", "cloudwatch")
    config = _config(
        {
            "dev": _enabled_env("dev", [_resolved(live)]),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[_resolved(inert)]),
        },
        store,
    )
    collector = Collector(config)

    collector.run_once()

    assert live.fetch_calls == 1
    # The disabled env's source is never touched.
    assert inert.fetch_calls == 0
    assert inert.health_calls == 0
    assert store.written_names() == {"panoptes_cloudwatch_count"}


def test_health_failure_does_not_abort_run(caplog: pytest.LogCaptureFixture) -> None:
    store = _RecordingStore()
    broken = _HealthFailingSource("dev", "cloudwatch")
    healthy = _FakeSource("dev", "sentry")
    env = _enabled_env("dev", [_resolved(broken), _resolved(healthy)])
    collector = Collector(_config({"dev": env}, store))

    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()

    # The healthy sibling is still fetched and written despite the broken source.
    assert healthy.fetch_calls == 1
    assert store.written_names() == {"panoptes_sentry_count"}
    assert "cloudwatch" in caplog.text


def test_unreachable_source_is_skipped_and_sibling_still_stored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A source whose health() returns reachable=False is skipped; sibling still stored (F2k).

    The unreachable source must NOT be fetched (no upstream call with no usable creds) and
    its signals must NOT reach the store, while a healthy sibling is fetched + written and
    the failure is error-logged.
    """
    store = _RecordingStore()
    unreachable = _UnreachableSource("dev", "cloudwatch")
    healthy = _FakeSource("dev", "sentry")
    env = _enabled_env("dev", [_resolved(unreachable), _resolved(healthy)])
    collector = Collector(_config({"dev": env}, store))

    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()

    # The unreachable source was never fetched; the healthy sibling was fetched + written.
    assert unreachable.fetch_calls == 0
    assert healthy.fetch_calls == 1
    assert store.written_names() == {"panoptes_sentry_count"}
    assert "cloudwatch" in caplog.text


def test_outage_signal_source_is_fetched_despite_unreachable_health() -> None:
    """An http-health-style source still emits its down signal during an outage (F3a).

    Its health() reports reachable=False (the monitored endpoint is down), but because
    it opts in via fetch_when_unreachable=True, the collector must STILL run the fetch so
    the mandated derived metric panoptes_health_up=0 reaches the store — that 0 is what
    turns the overview traffic-light RED. Skipping it would leave the light blank/stale
    exactly when it must show the outage.
    """
    store = _RecordingStore()
    outage = _OutageSignalSource("dev", "http-health")
    env = _enabled_env("dev", [_resolved(outage)])
    collector = Collector(_config({"dev": env}, store))

    collector.run_once()

    # The fetch ran despite reachable=False, and the down signal landed in the store.
    assert outage.fetch_calls == 1
    assert "panoptes_health_up" in store.written_names()
    # The actual outage value (0.0) reached the store — not merely the metric name.
    down_values = [
        signal.value
        for batch in store.batches
        for signal in batch
        if isinstance(signal, MetricSignal) and signal.name == "panoptes_health_up"
    ]
    assert down_values == [0.0]


def test_store_write_failure_is_caught_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _WriteFailingStore()
    first = _FakeSource("dev", "cloudwatch")
    second = _FakeSource("stage", "cloudwatch")
    config = _config(
        {
            "dev": _enabled_env("dev", [_resolved(first)]),
            "stage": _enabled_env("stage", [_resolved(second)]),
        },
        store,
    )
    collector = Collector(config)

    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()

    # Both envs were processed even though every write raised.
    assert first.fetch_calls == 1
    assert second.fetch_calls == 1
    assert "VM unreachable" in caplog.text or "write" in caplog.text.lower()


def test_slow_fetch_is_abandoned_on_timeout_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    store = _RecordingStore()
    slow = _SlowSource("dev", "slow", release)
    fast = _FakeSource("dev", "fast")
    # A tiny fetch_timeout_seconds on the slow source so the bound trips immediately;
    # no long wall-clock sleep — the future times out almost at once.
    env = _enabled_env(
        "dev",
        [
            ResolvedSource(source=slow, fetch_timeout_seconds=0, poll_interval_seconds=60),
            _resolved(fast),
        ],
    )
    collector = Collector(_config({"dev": env}, store))

    try:
        with caplog.at_level(logging.ERROR, logger="core.collector"):
            collector.run_once()
    finally:
        # Let the slow worker thread unwind so it does not leak past the test.
        release.set()

    # The slow source's signals never reached the store; the fast sibling still did.
    assert store.written_names() == {"panoptes_fast_count"}
    assert "panoptes_slow_count" not in store.written_names()
    assert "slow" in caplog.text


def test_chronically_down_source_is_log_throttled() -> None:
    store = _RecordingStore()
    failing = _FetchFailingSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(failing)])
    # Explicit K=3 / N=10 (the defaults) so the bound is unambiguous in-test.
    collector = Collector(_config({"dev": env}, store), failure_threshold=3, throttle_every=10)

    error_calls = 0

    class _CountingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal error_calls
            if record.levelno >= logging.ERROR:
                error_calls += 1

    handler = _CountingHandler()
    collector_logger = logging.getLogger("core.collector")
    collector_logger.addHandler(handler)
    previous_level = collector_logger.level
    collector_logger.setLevel(logging.ERROR)
    try:
        cycles = 30
        for _ in range(cycles):
            collector.run_once()
    finally:
        collector_logger.removeHandler(handler)
        collector_logger.setLevel(previous_level)

    # First K=3 cycles each log; thereafter only once per N=10. Over 30 cycles that is
    # 3 (the pre-throttle window) + the throttled emissions — far fewer than one per
    # cycle (30). Assert the bound holds and the source kept failing every cycle.
    assert failing.fetch_calls == cycles
    assert error_calls < cycles
    assert error_calls <= 3 + (cycles // 10) + 1


def test_successful_fetch_resets_throttle_counter() -> None:
    store = _RecordingStore()

    class _FlakySource(_FakeSource):
        def __init__(self) -> None:
            super().__init__("dev", "cloudwatch")
            self.should_fail = True

        def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
            self.fetch_calls += 1
            if self.should_fail:
                raise RuntimeError("upstream unreachable")
            return [_metric("dev", "panoptes_recovered_count")]

    flaky = _FlakySource()
    env = _enabled_env("dev", [_resolved(flaky)])
    collector = Collector(_config({"dev": env}, store), failure_threshold=3, throttle_every=10)

    # Fail 5 cycles (enters throttle), then recover — the recovery write succeeds.
    for _ in range(5):
        collector.run_once()
    flaky.should_fail = False
    collector.run_once()

    assert "panoptes_recovered_count" in store.written_names()


def test_run_once_via_run_executes_exactly_one_cycle() -> None:
    store = _RecordingStore()
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    collector = Collector(_config({"dev": env}, store))

    collector.run(once=True)

    assert source.fetch_calls == 1


def test_scheduled_run_loops_until_sleep_breaks() -> None:
    """The scheduled loop runs `run_once` then sleeps; an injected sleep that stops
    after N cycles drives the loop without any real wall-clock sleep."""
    store = _RecordingStore()
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])

    sleep_calls = 0

    def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            # Break the otherwise-infinite scheduled loop deterministically.
            raise KeyboardInterrupt

    collector = Collector(_config({"dev": env}, store), sleep=_fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        collector.run(poll_interval_seconds=60, once=False)

    # One fetch per cycle; the loop sleeps between cycles, breaking on the 3rd sleep.
    assert source.fetch_calls == 3


# --- v0.2: alert-rule evaluation wiring ------------------------------------------


class _QueryableStore(_RecordingStore):
    """A `_RecordingStore` that ALSO answers PromQL with a fixed value per env.

    `write` records batches (so the "evaluated AFTER the write" ordering can be asserted);
    `query` returns a one-point series at `value_by_env[env]` for whichever env the rule
    targets (the value is keyed by the env carried in the query's selector — here the test
    sets one value per env directly).
    """

    def __init__(self, value: float, *, env: str = "dev") -> None:
        super().__init__()
        self._value = value
        self._env = env
        self.query_calls = 0
        # Recorded so a test can assert rules were evaluated AFTER the store write.
        self.write_then_query_order: list[str] = []

    def write(self, signals: list[CanonicalSignal]) -> None:
        super().write(signals)
        self.write_then_query_order.append("write")

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.query_calls += 1
        self.write_then_query_order.append("query")
        return [
            MetricSeries(
                metric="panoptes_k8s_pods_crashloop",
                labels={"env": self._env},
                points=[(_now(), self._value)],
            )
        ]


def _crashloop_rule(
    *, for_cycles: int = 1, envs: list[str] | None = None, threshold: float = 0.0
) -> AlertRule:
    return AlertRule(
        name="crashloop-high",
        expr="panoptes_k8s_pods_crashloop",
        comparison=Comparison.GT,
        threshold=threshold,
        for_cycles=for_cycles,
        severity="critical",
        envs=envs if envs is not None else ["dev"],
        labels={"team": "platform"},
    )


def test_rules_are_evaluated_after_the_store_write() -> None:
    """Alert rules are evaluated AFTER the per-cycle fetch/store write (ordering)."""
    store = _QueryableStore(5.0)  # 5 > 0 → breach
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    collector = Collector(_config({"dev": env}, store, alerts=[_crashloop_rule()]))

    collector.run_once()

    # The store was written before any rule query ran this cycle.
    assert store.write_then_query_order[0] == "write"
    assert "query" in store.write_then_query_order
    assert store.write_then_query_order.index("write") < store.write_then_query_order.index("query")


def test_firing_rule_notifies_every_configured_notifier() -> None:
    """A firing rule calls `notify()` on EVERY configured notifier with the alert."""
    store = _QueryableStore(5.0)  # breach
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    notifier_a = _FakeNotifier()
    notifier_b = _FakeNotifier()
    collector = Collector(
        _config({"dev": env}, store, notifiers=[notifier_a, notifier_b], alerts=[_crashloop_rule()])
    )

    collector.run_once()

    # Both notifiers received the fired alert.
    assert len(notifier_a.alerts) == 1
    assert len(notifier_b.alerts) == 1
    fired = notifier_a.alerts[0]
    assert fired.name == "crashloop-high"
    assert fired.labels["env"] == "dev"
    assert fired.labels["team"] == "platform"


def test_for_cycles_debounce_fires_only_after_n_cycles() -> None:
    """A `for_cycles=3` rule fires `notify()` only on the third consecutive breaching cycle."""
    store = _QueryableStore(5.0)  # breach every cycle
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    notifier = _FakeNotifier()
    collector = Collector(
        _config({"dev": env}, store, notifiers=[notifier], alerts=[_crashloop_rule(for_cycles=3)])
    )

    collector.run_once()  # cycle 1 breach — no fire
    assert notifier.alerts == []
    collector.run_once()  # cycle 2 breach — no fire
    assert notifier.alerts == []
    collector.run_once()  # cycle 3 breach — FIRE
    assert len(notifier.alerts) == 1


def test_resolve_fires_notify_once_on_the_transition() -> None:
    """A non-breach cycle after firing fires `notify()` once (the resolve transition)."""

    # A store whose value we flip between cycles via a mutable holder.
    class _FlippingStore(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.value = 5.0  # start breaching

        def query(self, query: MetricQuery) -> list[MetricSeries]:
            return [
                MetricSeries(
                    metric="panoptes_k8s_pods_crashloop",
                    labels={"env": "dev"},
                    points=[(_now(), self.value)],
                )
            ]

    store = _FlippingStore()
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    notifier = _FakeNotifier()
    collector = Collector(
        _config({"dev": env}, store, notifiers=[notifier], alerts=[_crashloop_rule()])
    )

    collector.run_once()  # breach → FIRE (1 notify)
    assert len(notifier.alerts) == 1
    store.value = 0.0  # 0 > 0 is False → non-breach
    collector.run_once()  # non-breach after firing → RESOLVE (1 more notify)
    assert len(notifier.alerts) == 2
    collector.run_once()  # still non-breach → no further notify (already resolved)
    assert len(notifier.alerts) == 2


def test_evaluate_raising_is_caught_and_does_not_abort_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An `evaluate` raising is caught + logged; OTHER rules/envs still process (resilience).

    The store's `query` raises a NON-PanoptesError (a genuine bug shape, which `evaluate`
    does NOT swallow), so the collector's own resilience boundary must catch it — one bad
    rule never aborts the cycle.
    """

    class _ExplodingQueryStore(_RecordingStore):
        def query(self, query: MetricQuery) -> list[MetricSeries]:
            raise RuntimeError("query backend exploded")

    store = _ExplodingQueryStore()
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    notifier = _FakeNotifier()
    collector = Collector(
        _config({"dev": env}, store, notifiers=[notifier], alerts=[_crashloop_rule()])
    )

    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()  # must NOT raise

    # The cycle completed (the source was still fetched + written); the bad eval was logged.
    assert source.fetch_calls == 1
    assert "crashloop-high" in caplog.text or "alert" in caplog.text.lower()


def test_disabled_env_rules_are_not_evaluated() -> None:
    """A rule scoped to a disabled env is never evaluated (disabled envs are inert)."""
    store = _QueryableStore(5.0, env="stage")
    live = _FakeSource("dev", "cloudwatch")
    config = _config(
        {
            "dev": _enabled_env("dev", [_resolved(live)]),
            "stage": ResolvedEnvironment(name="stage", enabled=False, sources=[]),
        },
        store,
        alerts=[_crashloop_rule(envs=["stage"])],  # only targets the DISABLED env
    )
    notifier = config.notifiers[0]
    assert isinstance(notifier, _FakeNotifier)
    collector = Collector(config)

    collector.run_once()

    # The disabled env's rule never evaluated → no alert fired.
    assert notifier.alerts == []


def test_all_envs_rule_evaluated_for_every_enabled_env() -> None:
    """An `envs:["all"]` rule is evaluated for EVERY enabled env (fans out)."""

    # Both envs breach (the store returns the breaching value regardless of env selector).
    class _AllEnvStore(_RecordingStore):
        def query(self, query: MetricQuery) -> list[MetricSeries]:
            # Return a breaching series for BOTH dev and stage so the all-envs rule fires
            # once per enabled env.
            return [
                MetricSeries(
                    metric="panoptes_k8s_pods_crashloop",
                    labels={"env": "dev"},
                    points=[(_now(), 5.0)],
                ),
                MetricSeries(
                    metric="panoptes_k8s_pods_crashloop",
                    labels={"env": "stage"},
                    points=[(_now(), 5.0)],
                ),
            ]

    store = _AllEnvStore()
    config = _config(
        {
            "dev": _enabled_env("dev", [_resolved(_FakeSource("dev", "cloudwatch"))]),
            "stage": _enabled_env("stage", [_resolved(_FakeSource("stage", "cloudwatch"))]),
        },
        store,
        alerts=[_crashloop_rule(envs=["all"])],
    )
    notifier = config.notifiers[0]
    assert isinstance(notifier, _FakeNotifier)
    collector = Collector(config)

    collector.run_once()

    # The all-envs rule fired once for dev and once for stage (two distinct alerts).
    assert len(notifier.alerts) == 2
    fired_envs = {alert.labels["env"] for alert in notifier.alerts}
    assert fired_envs == {"dev", "stage"}


def test_notifier_delivery_failure_is_caught_and_other_notifiers_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A notifier raising on delivery is caught + logged; the OTHER notifiers still run.

    One failing delivery channel (e.g. an SNS publish error) must not abort the cycle or
    starve the remaining notifiers — the same per-channel resilience the collector applies
    everywhere.
    """

    class _ExplodingNotifier(_FakeNotifier):
        type = "exploding"

        def notify(self, alert: Alert) -> None:
            raise RuntimeError("delivery channel down")

    exploding = _ExplodingNotifier()
    healthy = _FakeNotifier()
    store = _QueryableStore(5.0)  # breach → fire
    source = _FakeSource("dev", "cloudwatch")
    env = _enabled_env("dev", [_resolved(source)])
    # The exploding notifier is first so a non-resilient impl would never reach `healthy`.
    config = _config(
        {"dev": env}, store, notifiers=[exploding, healthy], alerts=[_crashloop_rule()]
    )
    collector = Collector(config)

    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()  # must NOT raise

    # The healthy notifier still received the alert despite the exploding sibling.
    assert len(healthy.alerts) == 1
    assert "exploding" in caplog.text or "deliver" in caplog.text.lower()
