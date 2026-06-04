"""The collector loop — fetch → normalize → store, per enabled env, per source.

The collector is the scheduled worker (spec `## Performance Constraints`,
playbook Phase 4): on each cycle it walks every ENABLED environment and, for each
of that env's resolved sources, probes `health()`, runs a window-bounded `fetch`,
and hands the resulting signals to the single store. It is deliberately resilient —
no single source/env failure aborts a cycle:

- A source `health()` or `fetch` raising is caught, logged (f-string), and the loop
  CONTINUES to the next source/env — a flaky upstream never stalls the whole run.
- A source whose `health()` RETURNS `reachable=False` (e.g. a CloudWatch assume-role
  denial, which the source surfaces as unreachable rather than raising) is normally
  skipped for the cycle: its signals must not reach the store. It is recorded as a
  per-source failure so the throttle + error log treat it like any other failure.
  The deliberate EXCEPTION is a source that sets `fetch_when_unreachable=True` on the
  `Source` Protocol (http-health): for it `reachable=False` means "the MONITORED
  endpoint is down" and its fetch emits the mandated outage signal
  (`panoptes_health_up=0`) in exactly that state, so the collector still runs the fetch
  and lets that `0` reach the store (skipping it would blank the overview traffic-light
  precisely when it must show RED — F3a).
- A `store.write()` raising (e.g. VM unreachable) is caught per-batch, logged, and
  the loop continues to the next env.
- Each `fetch` is bounded by that source's `fetch_timeout_seconds` (spec default
  30s): the fetch runs in a `ThreadPoolExecutor` and is awaited with a bounded
  `concurrent.futures.wait(..., timeout=...)`; a fetch exceeding the bound is
  ABANDONED for the cycle (its signals never reach the store) and the loop
  continues. A thread pool + bounded wait is the cleanest *synchronous* bound —
  `signal`-based alarms only fire on the main thread and cannot bound a per-source
  call cleanly. `wait` (not `future.result(timeout=...)`) is used so a
  `timeout_seconds == 0` bound means "do not wait" rather than block.
- A chronically-down source is log-throttled via a PER-SOURCE consecutive-failure
  counter held on the collector instance: after `K` consecutive failures (default
  `K=3`) error logging for that source drops to once per `N` cycles (default
  `N=10`); the counter resets on a successful fetch. The throttle state persists
  across `run_once()` calls on the same instance, so a test drives `M` cycles by
  calling `run_once()` `M` times (no wall-clock sleep) and asserts a bounded
  error-log count.

Timing seams (clock/sleep/executor) are injectable so tests never actually sleep.
A `python -m core.collector --once --config <path>` CLI loads the config via
`core.config.load_config`, builds a `Collector`, and runs.
"""

import argparse
import logging
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from core.alerts import AlertRule, AlertState, evaluate, rule_applies_to_env
from core.config import ResolvedConfig, ResolvedSource, load_config
from core.model import Alert, CanonicalSignal, TimeWindow

_LOGGER = logging.getLogger(__name__)

# Each poll fetches the trailing 15 minutes of signals (spec `## Performance
# Constraints` — default last 15m per poll, bounding upstream API call volume).
_FETCH_WINDOW_MINUTES = 15

# Throttle defaults (spec): after K consecutive failures, error-log once per N cycles.
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_THROTTLE_EVERY = 10

# Default poll cadence for the scheduled loop (spec Open Question 1).
_DEFAULT_POLL_INTERVAL_SECONDS = 60


@dataclass
class _FailureState:
    """Per-source throttle bookkeeping (consecutive failures + cycles since last log)."""

    consecutive_failures: int = 0
    cycles_since_logged: int = 0


@dataclass(frozen=True)
class _SlowFetchTimeout(Exception):
    """Internal marker: a source's fetch exceeded its `fetch_timeout_seconds`."""

    timeout_seconds: int


class Collector:
    """Walks enabled envs, fetching each source into the store, resiliently."""

    def __init__(
        self,
        config: ResolvedConfig,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        throttle_every: int = _DEFAULT_THROTTLE_EVERY,
        sleep: Callable[[float], None] | None = None,
        window_factory: Callable[[], TimeWindow] | None = None,
    ) -> None:
        """Construct a collector from a resolved config, with injectable seams.

        Args:
            config: The resolved config (instantiated sources per env + one store).
            failure_threshold: `K` — consecutive failures before throttling a source.
            throttle_every: `N` — once throttled, error-log once per this many cycles.
            sleep: The sleep used by the scheduled loop; defaults to `time.sleep`.
                Injectable so tests drive the loop without a real wall-clock sleep.
            window_factory: Builds the per-poll `TimeWindow`; defaults to the trailing
                15-minute window. Injectable for deterministic tests.
        """
        self._config = config
        self._failure_threshold = failure_threshold
        self._throttle_every = throttle_every
        # `time.sleep` imported lazily inside the default so the module has no import
        # cost / no accidental real-sleep in test paths that inject their own.
        self._sleep = sleep if sleep is not None else self._default_sleep
        self._window_factory = (
            window_factory
            if window_factory is not None
            else (lambda: TimeWindow.last(minutes=_FETCH_WINDOW_MINUTES))
        )
        # Per-source throttle state, keyed by (env_name, source.type) — lives on the
        # instance so it persists across run_once() calls (the test seam for driving
        # M cycles on ONE collector).
        self._failure_states: dict[tuple[str, str], _FailureState] = {}
        # Per-(rule, env) alert debounce/firing state, keyed by (rule.name, env_name) —
        # SAME lifetime pattern as `_failure_states`: held on the instance so the
        # `for_cycles` debounce + fire/resolve transitions advance one cycle per
        # `run_once()` call with NO wall-clock sleep (the alert test seam).
        self._alert_states: dict[tuple[str, str], AlertState] = {}

    @staticmethod
    def _default_sleep(seconds: float) -> None:
        # Imported here (not at module scope) so injecting a fake sleep fully avoids
        # the real one — no test path ever touches `time.sleep`.
        import time

        time.sleep(seconds)

    def run(
        self, poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS, once: bool = False
    ) -> None:
        """Run the collector: a single cycle when `once`, else a scheduled loop.

        The scheduled loop runs `run_once()` then sleeps `poll_interval_seconds`
        between cycles via the injected `sleep` seam, so a test can break the loop
        deterministically without a real wall-clock sleep.
        """
        if once:
            self.run_once()
            return
        # Scheduled mode: cycle, sleep, repeat. The injected sleep is the test's break
        # seam (it raises to exit); production uses time.sleep and runs until killed.
        while True:
            self.run_once()
            self._sleep(poll_interval_seconds)

    def run_once(self) -> None:
        """Execute exactly one collection cycle across every enabled environment.

        For each enabled env: fetch + store every source, THEN evaluate every alert rule
        that applies to the env against the freshly-written store (so a rule sees this
        cycle's data). Disabled envs are wired-but-inert — no fetch, no rule evaluation.
        """
        window = self._window_factory()
        # One short-lived executor per cycle bounds every fetch and is torn down at
        # the end of the cycle (a leaked slow worker thread cannot outlive the run).
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="panoptes-fetch") as executor:
            for env in self._config.environments.values():
                if not env.enabled:
                    # Disabled envs are wired-but-inert: no health/fetch/write, and no
                    # rule evaluation (a rule scoped to a disabled env never evaluates).
                    continue
                for resolved_source in env.sources:
                    self._collect_source(env.name, resolved_source, window, executor)
                # Alert rules are evaluated AFTER this env's sources are written, so a
                # rule reads the value this cycle just stored (not last cycle's).
                self._evaluate_alerts_for_env(env.name)

    def _evaluate_alerts_for_env(self, env_name: str) -> None:
        """Evaluate every applicable alert rule for `env_name`, firing/resolving as needed.

        For each rule whose `envs` includes this env (or `["all"]`): run `evaluate` against
        the store, advance the per-`(rule, env)` `AlertState`, and call `notify()` on EVERY
        configured notifier on a FIRE (the debounced breach run reaches `for_cycles`) and on
        a RESOLVE (the first non-breach after firing). A rule whose evaluation RAISES (a
        genuine bug — `evaluate` already swallows store-can't-answer as a non-breach) is
        caught + logged here and skipped, so one bad rule never aborts the cycle (the same
        resilience boundary the source loop uses).
        """
        for rule in self._config.alerts:
            if not rule_applies_to_env(rule, env_name):
                continue
            try:
                self._evaluate_one_rule(rule, env_name)
            # Resilience boundary: a rule evaluation raising (e.g. the store backend itself
            # erroring with a non-PanoptesError) is caught + logged; sibling rules + envs
            # still process. `evaluate` already treats a store-can't-answer as a non-breach,
            # so this catches only genuine bugs, never the expected no-data path.
            except Exception as exc:
                _LOGGER.error(
                    f"alert rule '{rule.name}' evaluation failed for env={env_name}: {exc}; "
                    f"skipping this rule for this cycle and continuing"
                )

    def _evaluate_one_rule(self, rule: AlertRule, env_name: str) -> None:
        """Evaluate one rule for one env, advancing its `AlertState` and firing on transition.

        Builds (or reuses) the per-`(rule, env)` `AlertState`, runs `evaluate`, and on a
        FIRE or RESOLVE transition dispatches the alert to every notifier. The `AlertState`
        persists across cycles on the collector instance, so the `for_cycles` debounce
        advances one cycle per call with no sleep.
        """
        state = self._alert_states.setdefault((rule.name, env_name), AlertState())
        alert = evaluate(rule, env_name, self._config.store)
        if alert is not None:
            # A breach this cycle: advance the debounce; fire on the transition.
            if state.record_breach(rule.for_cycles):
                self._dispatch_alert(alert)
        # A non-breach this cycle: reset the counter; fire a RESOLVE on the transition.
        elif state.record_non_breach():
            self._dispatch_alert(self._resolve_alert(rule, env_name))

    def _dispatch_alert(self, alert: Alert) -> None:
        """Send `alert` to EVERY configured notifier, resiliently.

        A notifier raising (e.g. an SNS publish error) is caught + logged so one failing
        delivery channel never aborts the cycle or starves the other notifiers — the same
        per-channel resilience the rest of the collector applies.
        """
        for notifier in self._config.notifiers:
            try:
                notifier.notify(alert)
            # Resilience boundary: a notifier delivery failure is caught + logged; the
            # remaining notifiers + the rest of the cycle still run.
            except Exception as exc:
                _LOGGER.error(
                    f"notifier '{notifier.type}' failed to deliver alert "
                    f"'{alert.name}': {exc}; continuing with the remaining notifiers"
                )

    @staticmethod
    def _resolve_alert(rule: AlertRule, env_name: str) -> Alert:
        """Build the RESOLVE-transition `Alert` for a rule that just stopped breaching.

        Carries the rule's labels + env (mirroring a fired alert) with a `resolved` severity
        marker and a clear resolve message, so a notifier sees the recovery distinctly from
        the original fire.
        """
        labels: dict[str, str] = {**rule.labels, "env": env_name}
        return Alert(
            name=rule.name,
            severity="resolved",
            message=f"alert '{rule.name}' resolved in env {env_name} (no longer breaching)",
            labels=labels,
        )

    def _collect_source(
        self,
        env_name: str,
        resolved_source: ResolvedSource,
        window: TimeWindow,
        executor: ThreadPoolExecutor,
    ) -> None:
        """Health-probe, fetch (bounded), and store one source — resiliently.

        Any failure (health raise, fetch raise, fetch timeout, store-write raise) is
        caught and logged here so a single source/env never aborts the cycle. A
        successful fetch resets that source's throttle counter; a failure increments
        it and the error is logged subject to the throttle.
        """
        source = resolved_source.source
        key = (env_name, source.type)

        # `health()` is a read-only reachability probe; a raise must not abort the run.
        # The broad `except Exception` is the deliberate resilience boundary — ANY
        # source failure is caught, logged, and the loop continues to the next source.
        try:
            health = source.health()
        except Exception as exc:
            self._record_failure(env_name, key, source.type, f"health() failed: {exc}")
            return
        # An UNREACHABLE source is normally skipped for this cycle: its fetch would
        # attempt upstream calls with no usable credentials and its signals must NOT
        # reach the store (e.g. a CloudWatch assume-role denial, surfaced as
        # reachable=False rather than raising). Record it as a failure so the per-source
        # throttle + error log treat it exactly like a fetch failure (resilience
        # boundary, F2k).
        #
        # The deliberate EXCEPTION is a source that opts in via
        # `fetch_when_unreachable=True` (http-health): for it, `reachable=False` means
        # "the MONITORED endpoint is down" and its fetch is purpose-built to emit the
        # outage signal (panoptes_health_up=0) in exactly that state. Skipping it would
        # drop that 0 — leaving the overview traffic-light blank/stale precisely when it
        # must show RED — so we fall through to the fetch (F3a).
        if not health.reachable and not source.fetch_when_unreachable:
            self._record_failure(
                env_name, key, source.type, f"health() reported unreachable: {health.detail}"
            )
            return

        # Bound the fetch by this source's fetch_timeout_seconds. The fetch runs in the
        # shared executor; `future.result(timeout=...)` abandons a fetch that overruns.
        future: Future[list[CanonicalSignal]] = executor.submit(source.fetch, window)
        try:
            signals = self._await_fetch(future, resolved_source.fetch_timeout_seconds)
        except _SlowFetchTimeout as timeout:
            self._record_failure(
                env_name,
                key,
                source.type,
                f"fetch exceeded {timeout.timeout_seconds}s timeout; abandoned for this cycle",
            )
            return
        # Resilience boundary (see health() above): any fetch raise is caught + continues.
        except Exception as exc:
            self._record_failure(env_name, key, source.type, f"fetch() failed: {exc}")
            return

        # A successful fetch clears the throttle counter for this source.
        self._record_success(key)

        # Store-write failures (e.g. VM unreachable) are caught per-batch so the loop
        # continues to the next env — the batch is lost for this cycle, not the run.
        try:
            self._config.store.write(signals)
        # Resilience boundary: a store-write raise is caught per-batch; loop continues.
        except Exception as exc:
            _LOGGER.error(
                f"store.write failed for env={env_name} source={source.type}: {exc}; "
                f"dropping {len(signals)} signal(s) this cycle and continuing"
            )

    def _await_fetch(
        self, future: Future[list[CanonicalSignal]], timeout_seconds: int
    ) -> list[CanonicalSignal]:
        """Await a fetch future bounded by `timeout_seconds`, raising on overrun.

        Uses `concurrent.futures.wait(..., timeout=...)` rather than
        `future.result(timeout=...)` so a `timeout_seconds == 0` bound (the test seam
        for "trips immediately") is honored as "do not wait" instead of being coerced
        to a blocking call. On overrun the future is left running on its worker thread
        (the per-cycle executor is torn down at cycle end) and a `_SlowFetchTimeout`
        marker is raised so the caller abandons the source for this cycle.
        """
        done, _pending = wait([future], timeout=timeout_seconds, return_when=FIRST_COMPLETED)
        if future not in done:
            raise _SlowFetchTimeout(timeout_seconds=timeout_seconds)
        # The fetch completed within the bound — surface its result or its exception.
        return future.result()

    def _record_success(self, key: tuple[str, str]) -> None:
        """Reset the throttle counter for a source after a successful fetch."""
        self._failure_states.pop(key, None)

    def _record_failure(
        self, env_name: str, key: tuple[str, str], source_type: str, detail: str
    ) -> None:
        """Increment a source's failure counter and error-log subject to the throttle.

        For the first `K` consecutive failures every failure is logged. Once `K` is
        reached the source is "chronically down": logging drops to once per `N`
        cycles so a persistently-down upstream does not emit one error every poll.
        The counter (and the cycles-since-logged window) live on the instance, so the
        throttle persists across `run_once()` calls.
        """
        state = self._failure_states.setdefault(key, _FailureState())
        state.consecutive_failures += 1

        message = f"source failure for env={env_name} source={source_type}: {detail}"
        if state.consecutive_failures <= self._failure_threshold:
            # Pre-throttle window: log every failure.
            _LOGGER.error(message)
            state.cycles_since_logged = 0
            return

        # Throttled: log once per N cycles. cycles_since_logged counts cycles elapsed
        # since the last emitted error for this source.
        state.cycles_since_logged += 1
        if state.cycles_since_logged >= self._throttle_every:
            _LOGGER.error(
                f"{message} (still failing after {state.consecutive_failures} cycles; "
                f"throttled to once per {self._throttle_every} cycles)"
            )
            state.cycles_since_logged = 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the `python -m core.collector` CLI parser."""
    parser = argparse.ArgumentParser(
        prog="python -m core.collector",
        description="Run the Panoptes collector loop (fetch → store).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the panoptes: YAML config file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one collection cycle and exit (for tests/CI).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=_DEFAULT_POLL_INTERVAL_SECONDS,
        help="Scheduled-mode poll cadence in seconds (ignored with --once).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint: load config, build a `Collector`, and run.

    `python -m core.collector --once --config <path>` runs a single cycle; without
    `--once` it runs the scheduled loop at `--poll-interval-seconds`.
    """
    # Structured logging to stdout so the f-string log lines are visible when run via
    # docker-compose (spec `## Performance Constraints` — structured f-string logging).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    # Register the core adapters before resolving the config (which builds them).
    # Shared with core.mcp.server.main via core.bootstrap so the two entrypoints'
    # adapter sets never drift.
    from core.bootstrap import register_core_adapters

    register_core_adapters()
    config = load_config(args.config)
    collector = Collector(config)
    _LOGGER.info(
        f"collector starting: config={args.config} once={args.once} "
        f"poll_interval_seconds={args.poll_interval_seconds}"
    )
    collector.run(poll_interval_seconds=args.poll_interval_seconds, once=args.once)


if __name__ == "__main__":
    main()
