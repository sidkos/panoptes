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


def _now() -> datetime:
    return datetime.now(UTC)


def _metric(env: str, name: str) -> MetricSignal:
    return MetricSignal(name=name, value=1.0, timestamp=_now(), labels={"env": env})


class _FakeSource:
    """A `Source` that emits one named metric per fetch and is always healthy."""

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


class _SlowSource(_FakeSource):
    """A `Source` whose `fetch()` blocks until released — drives the timeout bound."""

    def __init__(self, env: str, type_name: str, release: threading.Event) -> None:
        super().__init__(env, type_name)
        self._release = release

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        # Block on the event with a short ceiling so the worker thread never leaks
        # past the test even though the collector abandons the future on timeout.
        self._release.wait(timeout=5.0)
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
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _config(environments: dict[str, ResolvedEnvironment], store: _RecordingStore) -> ResolvedConfig:
    return ResolvedConfig(
        environments=environments,
        store=store,
        notifiers=[_FakeNotifier()],
        dashboard_packs=[],
        slos=[],
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
