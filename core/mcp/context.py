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

from core.config import ResolvedConfig, ResolvedEnvironment
from core.errors import CapabilityError
from core.model import DashboardPack, SignalKind
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

    @property
    def dashboard_packs(self) -> list[DashboardPack]:
        """The resolved dashboard-pack catalog (core + injected consumer packs)."""
        return self._config.dashboard_packs
