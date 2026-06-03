"""The collector loop — fetch → normalize → store, per enabled env, per source.

The collector is the scheduled worker (spec `## Performance Constraints`,
playbook Phase 4): on each cycle it walks every ENABLED environment and, for each
of that env's resolved sources, probes `health()`, runs a window-bounded `fetch`,
and hands the resulting signals to the single store. It is deliberately resilient —
no single source/env failure aborts a cycle:

- A source `health()` or `fetch` raising is caught, logged (f-string), and the loop
  CONTINUES to the next source/env — a flaky upstream never stalls the whole run.
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

from core.config import ResolvedConfig, ResolvedSource, load_config
from core.model import CanonicalSignal, TimeWindow

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

    source_type: str
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
        """Execute exactly one collection cycle across every enabled environment."""
        window = self._window_factory()
        # One short-lived executor per cycle bounds every fetch and is torn down at
        # the end of the cycle (a leaked slow worker thread cannot outlive the run).
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="panoptes-fetch") as executor:
            for env in self._config.environments.values():
                if not env.enabled:
                    # Disabled envs are wired-but-inert: no health/fetch/write.
                    continue
                for resolved_source in env.sources:
                    self._collect_source(env.name, resolved_source, window, executor)

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
            source.health()
        except Exception as exc:
            self._record_failure(env_name, key, source.type, f"health() failed: {exc}")
            return

        # Bound the fetch by this source's fetch_timeout_seconds. The fetch runs in the
        # shared executor; `future.result(timeout=...)` abandons a fetch that overruns.
        future: Future[list[CanonicalSignal]] = executor.submit(source.fetch, window)
        try:
            signals = self._await_fetch(future, source.type, resolved_source.fetch_timeout_seconds)
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
        self, future: Future[list[CanonicalSignal]], source_type: str, timeout_seconds: int
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
            raise _SlowFetchTimeout(source_type=source_type, timeout_seconds=timeout_seconds)
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


def _register_core_adapters() -> None:
    """Import the core adapter modules so they self-register on the registries.

    Adapters self-register via their `@REGISTRY.register(...)` decorator at import
    time, but nothing imports them eagerly (so `flutter test`-style unit runs and the
    pure config loader never drag in `boto3`/`httpx`). The CLI, however, builds REAL
    adapters from the config, so it must trigger registration first. Imported lazily
    inside the entrypoint (not at module scope) to keep the collector module's import
    graph free of the heavy upstream-SDK dependencies the adapters pull in.
    """
    # Imported purely for the registration side-effect (the decorator runs at import
    # time). `import_module` makes the side-effect-only intent explicit and avoids an
    # unused-import lint on a bare `import core.stores.victoriametrics`.
    import importlib

    for module_path in (
        "core.notifiers.logging_notifier",
        "core.sources.cloudwatch",
        "core.sources.http_health",
        "core.sources.sentry",
        "core.stores.passthrough",
        "core.stores.victoriametrics",
    ):
        importlib.import_module(module_path)


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
    _register_core_adapters()
    config = load_config(args.config)
    collector = Collector(config)
    _LOGGER.info(
        f"collector starting: config={args.config} once={args.once} "
        f"poll_interval_seconds={args.poll_interval_seconds}"
    )
    collector.run(poll_interval_seconds=args.poll_interval_seconds, once=args.once)


if __name__ == "__main__":
    main()
