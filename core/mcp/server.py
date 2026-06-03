"""The Panoptes MCP (stdio) server — registers the read-only discovery/query tools.

Builds a `FastMCP` stdio server (spec `## API Surface` → MCP server) whose tools are
thin, config-bound wrappers over the pure functions in `tools_discovery` /
`tools_query`. The server is **fully read-only**: every registered tool is a
read-shaped name and there is no write path of any kind (the structural read-only
test in `tests/unit/test_mcp_query.py` enforces both the exact tool set and the
no-mutation-verb-name invariant).

Three registration concerns live here:

1. **Core tools** — the v0.1-implemented discovery + query tools, registered for
   every tool name the config lists that Panoptes implements.
2. **v0.2 stub tools** — `compare_envs` / `get_slo` / `get_cost` are valid FUTURE
   tools, not unknown adapters, so listing them is NOT a config-resolve failure.
   v0.1 registers them as stubs that raise an explicit "not available in v0.1
   (ships v0.2)" `CapabilityError` AT CALL TIME.
3. **Consumer-pack hook** — if `PANOPTES_CONSUMER_PACK` names a module path, import
   it and call its `register_tools(mcp_server)` so an injected pack can add its own
   read-only tools (default unset = core-only). This keeps `core` free of any
   consumer import — the pack is injected, never bundled (spec § Consumer-pack
   loading).

IMPORTANT (FastMCP / PEP-563): this module must NOT add
`from __future__ import annotations` — deferred annotations break FastMCP's schema
generation for the nested-`TypedDict` returns the registered tools expose.
"""

import importlib
import importlib.util
import os
from collections.abc import Callable, Mapping
from typing import Protocol

from fastmcp import FastMCP

from core.config import ResolvedConfig
from core.errors import CapabilityError, PanoptesError
from core.mcp.tools_discovery import (
    DashboardData,
    DashboardSummary,
    SignalCatalog,
    describe_signal_catalog,
    get_dashboard_data,
    list_dashboards,
)
from core.mcp.tools_query import (
    HealthRollup,
    IncidentFanOut,
    LogFanOut,
    describe_health,
    query_metric,
    search_incidents,
    search_logs,
)
from core.model import IncidentSignal, LogSignal, MetricSeries

# The env var naming the consumer pack's module path (default unset = core-only).
# The pack's module defines `register_tools(mcp_server)` (spec § Consumer-pack loading).
_CONSUMER_PACK_ENV_VAR = "PANOPTES_CONSUMER_PACK"


class _ToolCallable(Protocol):
    """A registered tool: callable with arbitrary read-only arguments.

    Defined as a Protocol (rather than `Callable[..., object]`) so the registry stays
    precisely typed under `mypy --strict` `disallow_any_explicit` — a `...` ParamSpec
    in a `Callable` is treated as implicit `Any` args, which the policy forbids; the
    explicit `*object`/`**object` signature here carries no `Any`.
    """

    def __call__(self, *args: object, **kwargs: object) -> object: ...


class _FastMcpAdapter:
    """A thin, `Any`-free adapter over the exact FastMCP surface Panoptes uses.

    FastMCP IS typed (it ships `py.typed`) and is generic over its lifespan-result
    type, so a bare `FastMCP` annotation is `FastMCP[Any]` — which
    `disallow_any_explicit` rejects, and whose overloaded `tool`/`run` signatures do
    not structurally satisfy a hand-written Protocol. Wrapping the instance in this
    adapter confines the single `Any` boundary (the `FastMCP` generic) to one place
    and exposes precisely the two operations the server needs — `tool(name)` (the
    `tool(name=...)` registration form) and `run()` (stdio transport) — with clean,
    precise signatures the rest of the module can depend on.
    """

    def __init__(self, mcp: "FastMCP[object]") -> None:
        self._mcp = mcp

    def tool[**ToolParams](self, name: str, fn: Callable[ToolParams, object]) -> None:
        """Register `fn` as a FastMCP tool under the explicit `name`."""
        self._mcp.tool(name=name)(fn)

    def run(self) -> None:
        """Run the underlying FastMCP server over the stdio transport."""
        self._mcp.run(transport="stdio")


# The v0.2-listed-but-unimplemented tools. Listing one is NOT a resolve failure —
# v0.1 registers it as a call-time "not available" stub. All are read-shaped names,
# so they also satisfy the structural no-mutation-verb-name invariant.
_V0_2_STUB_TOOLS = ("compare_envs", "get_slo", "get_cost")

# The exact v0.1-implemented read-only tool set (catalog / discovery / query). The
# structural read-only test asserts a default-config server registers EXACTLY these
# (plus any injected-pack tools); a future write tool under any name would break it.
KNOWN_READ_ONLY_TOOLS: tuple[str, ...] = (
    "describe_signal_catalog",
    "list_dashboards",
    "get_dashboard_data",
    "query_metric",
    "search_incidents",
    "search_logs",
    "describe_health",
)


class PanoptesMcpServer:
    """A config-bound FastMCP stdio server exposing the read-only Panoptes tools.

    Holds the resolved config, the FastMCP instance, and a typed registry of the
    tools it registered (name -> callable). The registry is the source of truth the
    structural read-only test introspects synchronously — it never has to drive
    FastMCP's async transport to learn the registered tool names.
    """

    def __init__(self, config: ResolvedConfig, mcp: _FastMcpAdapter) -> None:
        self._config = config
        self.mcp = mcp
        # The synchronous source of truth the structural read-only test introspects.
        # FastMCP only exposes its tool table through ASYNC accessors, so the names
        # are mirrored here to keep the read-only assertion fully synchronous.
        self._tool_names: list[str] = []
        # The uniform `_ToolCallable` invokers for tools a test invokes directly
        # (currently the v0.2 stubs, which already match the `_ToolCallable` shape).
        self._callables: dict[str, _ToolCallable] = {}

    def _register_tool[**ToolParams](self, name: str, fn: Callable[ToolParams, object]) -> None:
        """Register a real-signature tool wrapper under `name` with FastMCP.

        `fn` keeps its real (typed, nested-`TypedDict`) signature so FastMCP can
        introspect it for schema generation — it is registered under the EXPLICIT
        `name` (independent of the wrapper's `__name__`). The name is mirrored into
        `_tool_names` for the synchronous structural read-only assertion. A
        `ParamSpec` keeps this `Any`-free while accepting any concrete wrapper.
        """
        self.mcp.tool(name, fn)
        self._tool_names.append(name)

    def _register_stub[**ToolParams](
        self, name: str, fastmcp_fn: Callable[ToolParams, object], invoker: _ToolCallable
    ) -> None:
        """Register a v0.2 call-time stub under `name`.

        FastMCP rejects `*args`/`**kwargs` tools, so the stub presents a concrete
        (typed) signature to FastMCP via `fastmcp_fn`, while a separate uniform
        `_ToolCallable` `invoker` (arbitrary read-only args → raise) is stored in
        `_callables` so the v0.2-stub test can invoke it directly under any keyword
        shape. Both raise the same explicit not-available `CapabilityError`.
        """
        self.mcp.tool(name, fastmcp_fn)
        self._tool_names.append(name)
        self._callables[name] = invoker

    def tool_names(self) -> list[str]:
        """The names of every registered tool (synchronous; the read-only test seam)."""
        return list(self._tool_names)

    def tool_callable(self, name: str) -> _ToolCallable:
        """The bound callable for a stub tool (for the synchronous v0.2-stub test)."""
        return self._callables[name]

    def run(self) -> None:
        """Run the server over the stdio transport (the v0.1 entrypoint)."""
        self.mcp.run()


def _configured_tool_names(config: ResolvedConfig) -> list[str]:
    """The tool names the config lists, defaulting to the full v0.1 set when omitted.

    A config with no `mcp.tools` still gets the core read-only tools (the example
    config lists them explicitly, but an omitted list should not yield a tool-less
    server).
    """
    raw_tools = config.mcp.get("tools")
    if isinstance(raw_tools, list):
        return [str(tool) for tool in raw_tools]
    return list(KNOWN_READ_ONLY_TOOLS)


def build_server(config: ResolvedConfig) -> PanoptesMcpServer:
    """Construct the FastMCP stdio server + register core / stub / pack tools.

    Args:
        config: The resolved config (bound into every tool wrapper).

    Returns:
        A `PanoptesMcpServer` with all configured tools registered.
    """
    mcp: FastMCP[object] = FastMCP("panoptes")
    server = PanoptesMcpServer(config, _FastMcpAdapter(mcp))

    core_registrars = _core_registrars(config)
    for name in _configured_tool_names(config):
        registrar = core_registrars.get(name)
        if registrar is not None:
            registrar(server, name)
        elif name in _V0_2_STUB_TOOLS:
            _register_v0_2_stub(server, name)
        # An unknown tool name that is neither a v0.1 core tool nor a known v0.2 stub
        # is ignored here — config-level adapter validation is the loader's job; the
        # MCP server only wires the tools it knows. (No silent write path is created.)

    _load_consumer_pack(server)
    return server


def _core_registrars(
    config: ResolvedConfig,
) -> Mapping[str, Callable[[PanoptesMcpServer, str], None]]:
    """Map each v0.1 core tool name to a registrar that binds `config` into a wrapper.

    Each wrapper closes over `config` so the FastMCP-facing signature carries only the
    caller-supplied arguments (env / name / window / …), and returns the precise
    nested-`TypedDict` shapes from `tools_discovery` / `tools_query`.
    """

    def register_describe_signal_catalog(server: PanoptesMcpServer, name: str) -> None:
        def describe_signal_catalog_tool() -> SignalCatalog:
            """List environments, configured sources + capabilities, metrics, dashboards."""
            return describe_signal_catalog(config)

        server._register_tool(name, describe_signal_catalog_tool)

    def register_list_dashboards(server: PanoptesMcpServer, name: str) -> None:
        def list_dashboards_tool() -> list[DashboardSummary]:
            """Return the dashboard catalog (core + injected consumer packs)."""
            return list_dashboards(config.dashboard_packs)

        server._register_tool(name, list_dashboards_tool)

    def register_get_dashboard_data(server: PanoptesMcpServer, name: str) -> None:
        def get_dashboard_data_tool(dashboard_id: str, env: str) -> DashboardData:
            """Execute one dashboard's panels for `env`: title + PromQL + series."""
            return get_dashboard_data(dashboard_id, env, config, config.dashboard_packs)

        server._register_tool(name, get_dashboard_data_tool)

    def register_query_metric(server: PanoptesMcpServer, name: str) -> None:
        def query_metric_tool(
            env: str, metric: str, window: str, filters: dict[str, str] | None = None
        ) -> list[MetricSeries]:
            """Run a PromQL passthrough query for a metric against the store."""
            return query_metric(config, env=env, name=metric, window=window, filters=filters)

        server._register_tool(name, query_metric_tool)

    def register_search_incidents(server: PanoptesMcpServer, name: str) -> None:
        def search_incidents_tool(
            env: str, window: str, tag: str | None = None, level: str | None = None
        ) -> list[IncidentSignal] | IncidentFanOut:
            """Search incident signals for `env` (or fan out across all enabled envs)."""
            return search_incidents(config, env=env, window=window, tag=tag, level=level)

        server._register_tool(name, search_incidents_tool)

    def register_search_logs(server: PanoptesMcpServer, name: str) -> None:
        def search_logs_tool(
            env: str, query: str, window: str, level: str | None = None
        ) -> list[LogSignal] | LogFanOut:
            """Search log signals for `env` (or fan out across all enabled envs)."""
            return search_logs(config, env=env, query=query, window=window, level=level)

        server._register_tool(name, search_logs_tool)

    def register_describe_health(server: PanoptesMcpServer, name: str) -> None:
        def describe_health_tool(env: str) -> HealthRollup:
            """Roll up per-source reachability + open-incident count for `env`."""
            return describe_health(config, env=env)

        server._register_tool(name, describe_health_tool)

    return {
        "describe_signal_catalog": register_describe_signal_catalog,
        "list_dashboards": register_list_dashboards,
        "get_dashboard_data": register_get_dashboard_data,
        "query_metric": register_query_metric,
        "search_incidents": register_search_incidents,
        "search_logs": register_search_logs,
        "describe_health": register_describe_health,
    }


def _register_v0_2_stub(server: PanoptesMcpServer, name: str) -> None:
    """Register a v0.2-listed tool as a call-time 'not available' stub.

    Listing a v0.2 tool is a valid (future) tool reference, not an unknown adapter,
    so it does NOT fail config resolution. The stub raises an explicit
    `CapabilityError` AT CALL TIME so a caller gets a clear not-available error
    rather than a missing-attribute crash. The stub accepts arbitrary keyword
    arguments so any invocation shape reaches the explicit error.
    """

    message = f"Tool '{name}' is not available in v0.1 (ships v0.2)."

    def v0_2_stub_tool(env: str = "all") -> object:
        """A v0.2 tool not implemented in v0.1 — raises an explicit not-available error.

        Carries a concrete `env` parameter (FastMCP rejects `*args`/`**kwargs` tools)
        so it presents a valid schema; the body always raises regardless of args.
        """
        raise CapabilityError(message)

    def v0_2_stub_invoker(*_args: object, **_kwargs: object) -> object:
        """The uniform-shape invoker stored for the synchronous v0.2-stub test."""
        raise CapabilityError(message)

    server._register_stub(name, v0_2_stub_tool, v0_2_stub_invoker)


def _load_consumer_pack(server: PanoptesMcpServer) -> None:
    """Import the consumer pack named by `PANOPTES_CONSUMER_PACK` and register its tools.

    Default unset = core-only (no import, no consumer coupling). When set, the pack is
    imported and its `register_tools(mcp_server)` hook is called so the injected pack
    can add its own read-only tools. `core` never imports the pack statically — the
    pack is injected at deploy time, never bundled.

    `PANOPTES_CONSUMER_PACK` is primarily a FILE PATH: the compose deployment mounts
    the consumer's pack as a single file at `/packs/consumer/pack.py` and points the
    env var at it, so a mounted (non-installed, not-on-`sys.path`) pack is loaded via
    `importlib.util.spec_from_file_location`. A dotted module name is also accepted as
    a fallback (an installed or in-repo importable pack).
    """
    pack_ref = os.environ.get(_CONSUMER_PACK_ENV_VAR)
    if not pack_ref:
        return
    module = _import_consumer_pack(pack_ref)
    register_tools = getattr(module, "register_tools", None)
    if callable(register_tools):
        register_tools(server)


def _import_consumer_pack(pack_ref: str) -> object:
    """Load the consumer pack from a file path (deploy model) or a dotted module name."""
    # A path-shaped ref (a mounted pack.py) loads from file; everything else is treated
    # as a dotted, importable module name.
    looks_like_path = pack_ref.endswith(".py") or "/" in pack_ref or os.sep in pack_ref
    if not looks_like_path:
        return importlib.import_module(pack_ref)
    spec = importlib.util.spec_from_file_location("panoptes_consumer_pack", pack_ref)
    if spec is None or spec.loader is None:
        raise PanoptesError(f"Cannot load consumer pack from file path: {pack_ref}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    """CLI entrypoint: load the config from `PANOPTES_CONFIG` and run the stdio server.

    Invoked as `python -m core.mcp.server`. The config path is read from the
    `PANOPTES_CONFIG` env var (the compose file sets it to the mounted config).
    """
    from pathlib import Path

    from core.bootstrap import register_core_adapters
    from core.config import load_config

    # Register the core adapters before resolving the config (which builds REAL
    # adapters from its `type` fields) — mirrors core.collector.main. Without this a
    # config naming `http-health`/`victoriametrics` would raise UnknownAdapterError.
    register_core_adapters()
    config_path = os.environ.get("PANOPTES_CONFIG", "panoptes.yaml")
    config = load_config(Path(config_path))
    build_server(config).run()


if __name__ == "__main__":
    main()
