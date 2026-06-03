"""The `http-health` source — probes an HTTP `/health` endpoint into gauge metrics.

`fetch` issues a single GET against the configured `url`, times the round-trip, and
emits two derived gauge `MetricSignal`s (spec `## Data Model` → Derived metrics):

| Metric | Value | Labels (exact) |
|--------|-------|----------------|
| `panoptes_health_up` | `1.0` on a 2xx/3xx response, `0.0` otherwise | `env`, `url` |
| `panoptes_health_latency_ms` | the measured request latency in milliseconds | `env`, `url` |

"Down" covers three failure shapes — connection refused, timeout, and a 5xx
status — and **all three still record a latency** (the time spent before the
failure) so a Grafana latency panel is never blank during an outage. `_up` is
`0.0` for every down shape.

`env` is mandatory on every signal (model invariant). The source is configured
per-environment, so it reads its own `env` from the config block: the loader builds
one source instance per env and threads the env into each block. This adapter
consumes a flat `env` config field rather than coupling to a Phase-1 loader change
(Phase-7 YAML/loader wiring supplies it; tests pass it directly).

This adapter is the LIGHT delegate to `core.rest`: it shares only the injectable
`httpx.Client` construction seam (via `RestClient`). It deliberately does NOT use the
shared raise/failure-surfacing path, because a probe failure is NOT an error to raise
— every down shape (5xx / connection-refused / timeout) must map to `up=0.0` with a
latency still recorded, not a `PanoptesError`. So it drives the raw client and keeps
its own up/down branching.

httpx is mocked in tests with `respx`, which patches the transport globally, so the
`RestClient`'s default `httpx.Client()` is intercepted without an injected client; the
client is still threaded as a constructor seam for explicit control.
"""

import time
from datetime import UTC, datetime

import httpx

from core.model import (
    CanonicalSignal,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.rest import RestClient
from core.sources._config import require_str_field

# Derived-metric names (spec `## Data Model` — `panoptes_` prefix avoids colliding
# with native upstream metric names in PromQL).
_METRIC_UP = "panoptes_health_up"
_METRIC_LATENCY_MS = "panoptes_health_latency_ms"

# Per-probe request timeout (seconds). A hung endpoint must surface as `_up=0` with
# a bounded latency rather than stalling the collector cycle.
_REQUEST_TIMEOUT_SECONDS = 10.0


@SOURCES.register("http-health")
class HttpHealthSource:
    """Probes one HTTP `/health` endpoint and emits up/latency gauge metrics."""

    type = "http-health"

    def __init__(self, config: ConfigBlock, client: httpx.Client | None = None) -> None:
        """Read the required `url` and `env` from config; accept an optional client.

        The `client` seam keeps the source unit-testable without monkeypatching;
        under `respx` a default `httpx.Client()` is intercepted globally, so
        production code passes no client and tests need not inject one. The client is
        threaded into the shared `RestClient` (sharing only the construction seam —
        see the module docstring on why this probe keeps its own up/down branching).
        """
        self._url = require_str_field(config, "url", self.type)
        # `env` is mandatory because every emitted signal must carry an `env` label
        # (model invariant). Read it from the per-env config block the loader builds.
        self._env = require_str_field(config, "env", self.type)
        self._rest = RestClient(client)

    def capabilities(self) -> set[SignalKind]:
        """http-health only ever emits metric signals (the two health gauges)."""
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """GET the health URL, timing latency, and emit the up + latency gauges.

        `window` is part of the `Source` Protocol but a point-in-time health probe
        has no historical window to honor — the probe reflects "now". The latency is
        always recorded; `_up` is `1.0` only for a successful 2xx/3xx response.
        """
        up_value, latency_ms, timestamp = self._probe()
        labels = {"env": self._env, "url": self._url}
        return [
            MetricSignal(
                name=_METRIC_UP,
                value=up_value,
                timestamp=timestamp,
                labels=dict(labels),
            ),
            MetricSignal(
                name=_METRIC_LATENCY_MS,
                value=latency_ms,
                timestamp=timestamp,
                labels=dict(labels),
            ),
        ]

    def health(self) -> SourceHealth:
        """Trivial reachability probe: reuse `_probe` and report up/down + latency."""
        up_value, latency_ms, timestamp = self._probe()
        reachable = up_value == 1.0
        detail = (
            f"{self._url} reachable in {latency_ms:.1f}ms"
            if reachable
            else f"{self._url} unreachable (latency {latency_ms:.1f}ms)"
        )
        return SourceHealth(reachable=reachable, detail=detail, checked_at=timestamp)

    def _probe(self) -> tuple[float, float, datetime]:
        """Issue the GET, returning `(up_value, latency_ms, timestamp)`.

        A 2xx/3xx response yields `up=1.0`; a 4xx/5xx status, a connection error, or
        a timeout all yield `up=0.0` — but every branch records the elapsed latency
        (the time spent before success or failure) so the latency gauge is never
        blank during an outage. The timestamp is captured once, at probe start, so
        both emitted metrics share a single consistent sample time.

        `httpx.HTTPStatusError` (raised by `raise_for_status` on a 5xx) is a subclass
        of `httpx.HTTPError`, so the single `except httpx.HTTPError` branch covers all
        three down shapes — 5xx, connection-refused, and timeout — uniformly.
        """
        timestamp = datetime.now(UTC)
        start = time.perf_counter()
        try:
            response = self._rest.http.get(self._url, timeout=_REQUEST_TIMEOUT_SECONDS)
            # raise_for_status turns a 5xx into an HTTPStatusError (an HTTPError),
            # collapsing the 5xx case into the same down branch as transport errors.
            response.raise_for_status()
            latency_ms = (time.perf_counter() - start) * 1000.0
            return (1.0, latency_ms, timestamp)
        except httpx.HTTPError:
            # 5xx status, connection refused, or timeout — all "down". The elapsed
            # time before the failure is still a meaningful latency to record so the
            # latency gauge is never blank during an outage.
            latency_ms = (time.perf_counter() - start) * 1000.0
            return (0.0, latency_ms, timestamp)
