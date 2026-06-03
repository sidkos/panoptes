"""`QueryContext` — the small read-only seam the MCP tools depend on.

Every MCP query/discovery tool used to reach into `ResolvedConfig`'s concrete shape
directly (`config.environments.values()`, `config.store`, `config.dashboard_packs`,
the `_enabled_envs`/`_require_env`/`_sources_providing` module helpers). That made a
test of one tool construct a whole `ResolvedConfig`. `QueryContext` is the deep seam
that fixes it: it exposes exactly the handful of operations the tools need over the
resolved config, so the tools depend on this small interface rather than the config's
shape — and a test can drive context behavior from a MINIMAL hand-built config.

The interface is intentionally SMALL (the review flagged over-engineering risk): it
carries only methods a tool actually uses — the enabled-env walk, single-env
resolution (raising the existing clear `CapabilityError`), capability-filtered source
selection, the store, the dashboard-pack catalog, and the all-envs/env-names faces
the signal catalog needs (which include disabled envs, unlike the fetch walk).

IMPORTANT (FastMCP / PEP-563): this module must NOT add
`from __future__ import annotations` — it lives under `core/mcp/`, and deferred
annotations break FastMCP's schema generation for the tool returns that consume it.
"""

from core.config import ResolvedConfig, ResolvedEnvironment, SloConfig
from core.errors import CapabilityError
from core.mcp._metric_helpers import (
    _latest_value,
    _step_seconds_for,
    _window_for,
    escape_promql_value,
)
from core.model import DashboardPack, MetricQuery, MetricSeries, SignalKind
from core.planes.source import Source
from core.planes.store import Store


class QueryContext:
    """A small read-only view over a `ResolvedConfig` for the MCP tools.

    Built once in `build_server` from the resolved config and handed to the tool
    functions, so the tools (and their tests) depend on this interface — not the
    concrete `ResolvedConfig` layout. Holds the config by reference; every method is
    a thin, intention-revealing read.
    """

    def __init__(self, config: ResolvedConfig) -> None:
        self._config = config

    def enabled_envs(self) -> list[ResolvedEnvironment]:
        """The enabled environments, in declaration order (disabled envs are inert)."""
        return [env for env in self._config.environments.values() if env.enabled]

    def all_envs(self) -> list[ResolvedEnvironment]:
        """Every declared environment, incl. disabled — the signal-catalog source walk."""
        return list(self._config.environments.values())

    def env_names(self) -> list[str]:
        """Every declared environment name, in declaration order (for the catalog)."""
        return list(self._config.environments.keys())

    def require_env(self, env: str) -> ResolvedEnvironment:
        """Resolve a single env by name, failing explicitly if it is unknown/disabled.

        Raises:
            CapabilityError: the env is not declared, or is declared but disabled —
                the same clear, named error the tools surfaced before the seam.
        """
        environment = self._config.environments.get(env)
        if environment is None:
            available = ", ".join(self._config.environments.keys()) or "(none)"
            raise CapabilityError(
                f"No environment named '{env}'. Available environments: {available}."
            )
        if not environment.enabled:
            raise CapabilityError(
                f"Environment '{env}' is disabled (enabled: false) and has no live sources."
            )
        return environment

    def sources_for(self, environment: ResolvedEnvironment, kind: SignalKind) -> list[Source]:
        """The env's sources whose `capabilities()` include `kind` (may be empty)."""
        return [
            resolved.source
            for resolved in environment.sources
            if kind in resolved.source.capabilities()
        ]

    @property
    def store(self) -> Store:
        """The resolved metric store (answers `query_metric` / dashboard PromQL)."""
        return self._config.store

    def read_gauge(self, metric: str, env: str, window: str = "15m") -> float | None:
        """Read one `env`-scoped gauge from the store, returning its latest scalar (or None).

        Concentrates the gauge-read sequence the MCP tools repeated: escape `env`, build the
        `env`-scoped selector, query the store, and pick the latest value. A store that cannot
        answer PromQL (a `passthrough` store → `CapabilityError`) is SWALLOWED to `None` so the
        calling tool (`get_cluster_state` / `get_cost` / `get_slo` / `describe_health`) stays
        answerable from the store's "no data" rather than crashing into the MCP surface.

        Returns `None`, NEVER an invented `0.0`, when there is no data — so a caller chooses
        `read_gauge(...) or 0.0` only where a zero default is the intended semantic (the cost /
        cluster snapshots), and `describe_health` simply omits an absent metric.

        Args:
            metric: The gauge metric name (a `panoptes_*` series the store carries).
            env: The environment to scope the read to. It is escaped UNCONDITIONALLY (F7) —
                a caller must NEVER interpolate `env` into a selector itself.
            window: The trailing window string (default `"15m"`, the prior internal default).

        Returns:
            The latest sample value across the resolved series, or `None` when the store has no
            data OR could not answer PromQL (the `CapabilityError` is swallowed).
        """
        try:
            series = self.read_series(metric, env, window)
        except CapabilityError:
            # A passthrough store cannot answer — the gauge read is "no data" (None), so the
            # calling tool reports unreachable/zero rather than raising into the MCP surface.
            return None
        return _latest_value(series)

    def read_series(self, metric: str, env: str, window: str = "15m") -> list[MetricSeries]:
        """Read one `env`-scoped metric from the store, returning its raw series list.

        The series-returning sibling of `read_gauge` for callers that need the full per-label
        series (the per-namespace `pod_restarts_total` sum, the per-service `panoptes_cost_spend`
        map, the `compare_envs` per-env comparison). Unlike `read_gauge`, it PROPAGATES a
        `CapabilityError` — `compare_envs`'s fan-out depends on a per-env outage surfacing so the
        env is marked down, not silently treated as an empty result.

        Like `read_gauge`, it OWNS the F7 escape: `escape_promql_value(env)` is applied
        unconditionally, so a quote-bearing env stays a single closed selector string.

        Args:
            metric: The metric name (a `panoptes_*` series the store carries).
            env: The environment to scope the read to (escaped UNCONDITIONALLY, F7).
            window: The trailing window string (default `"15m"`).

        Returns:
            The raw `list[MetricSeries]` the store returned (possibly empty).

        Raises:
            CapabilityError: the store cannot answer PromQL (e.g. a `passthrough` store) — left
                to PROPAGATE so the caller can mark the env down.
        """
        # The escape is the security invariant (F7): never interpolate `env` raw.
        expr = f'{metric}{{env="{escape_promql_value(env)}"}}'
        return self._config.store.query(
            MetricQuery(
                expr=expr, window=_window_for(window), step_seconds=_step_seconds_for(window)
            )
        )

    @property
    def dashboard_packs(self) -> list[DashboardPack]:
        """The resolved dashboard-pack catalog (core + injected consumer packs)."""
        return self._config.dashboard_packs

    @property
    def slos(self) -> list[SloConfig]:
        """The resolved SLO definitions (the `get_slo` tool looks one up by name)."""
        return self._config.slos
