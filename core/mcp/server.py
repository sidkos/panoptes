"""The Panoptes MCP (stdio) server — registers the read-only discovery/query tools.

Builds a `FastMCP` stdio server (spec `## API Surface` → MCP server) whose tools are
thin, config-bound wrappers over the pure functions in `tools_discovery` /
`tools_query`. The server is **fully read-only**: every registered tool is a
read-shaped name and there is no write path of any kind (the structural read-only
test in `tests/unit/test_mcp_query.py` enforces both the exact tool set and the
no-mutation-verb-name invariant).

Two registration concerns live here:

1. **Core tools** — the fully-implemented discovery + query tools, registered for
   every tool name the config lists that Panoptes implements (the v0.1 trio + v0.2's
   `get_cluster_state`/`get_slo`/`compare_envs` + v0.3's `get_cost`). EVERY shipped tool
   is real — the v0.2 call-time "not available" stub machinery was DELETED once `get_cost`
   (the last stub) shipped in v0.3, so there is no longer a stub path. A future planned-but-
   unimplemented tool would reinstate that seam at that time.
2. **Consumer-pack hook** — if `PANOPTES_CONSUMER_PACK` names a module path, import
   it and call its `register_tools(mcp_server)` so an injected pack can add its own
   read-only tools (default unset = core-only). This keeps `core` free of any
   consumer import — the pack is injected, never bundled (spec § Consumer-pack
   loading).

IMPORTANT (FastMCP / PEP-563): this module must NOT add
`from __future__ import annotations` — deferred annotations break FastMCP's schema
generation for the nested-`TypedDict` returns the registered tools expose.
"""

import functools
import importlib
import importlib.util
import inspect
import os
import types
import typing
from collections.abc import Callable, Mapping
from typing import Protocol

from fastmcp import FastMCP

from core.config import ResolvedConfig
from core.errors import PanoptesError
from core.mcp.context import QueryContext
from core.mcp.tools_discovery import (
    DashboardData,
    DashboardSummary,
    SignalCatalog,
    describe_signal_catalog,
    get_dashboard_data,
    list_dashboards,
)
from core.mcp.tools_query import (
    ClusterState,
    CostBreakdown,
    EnvComparison,
    HealthRollup,
    IncidentFanOut,
    LogFanOut,
    SloResult,
    compare_envs,
    describe_health,
    get_cluster_state,
    get_cost,
    get_slo,
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

    def run_http(self, host: str, port: int) -> None:
        """Run the underlying FastMCP server over the streamable-HTTP transport.

        Additive over `run()` (stdio): the SAME registered tool table is served, only the
        transport differs (two faces, one store — spec § MCP server HTTP transport). For
        fastmcp 3.4.0 the `host`/`port` flow through `run`'s `**transport_kwargs` to
        `run_http_async`, which accepts them as named kwargs.

        Bind note: the server binds its CONTAINER port (e.g. `0.0.0.0:8080` inside the pod)
        — acceptable because on K8s the network boundary is the `ClusterIP` Service + the
        nginx ingress (the only public path), NOT the server's listen address. The GitHub
        auth gate is enforced at the ingress + oauth2-proxy, not by a non-public bind. The
        Phase-7 Helm render test asserts the Service is `ClusterIP` + the ingress
        forward-auth — that is where the boundary lives.
        """
        self._mcp.run(transport="streamable-http", host=host, port=port)

    def register_health_route(self) -> None:
        """Register the unauthenticated `GET /healthz` liveness route on the HTTP app.

        `/healthz` returns `{"status": "ok"}` (200) and carries NO signal data — it is the
        ONLY unauthenticated route (spec § MCP HTTP face): the nginx ingress exempts it from
        forward-auth so the kubelet liveness probe + the ingress can confirm the server is up
        WITHOUT a token. Registered via FastMCP 3.4.0's `custom_route`, which mounts a
        Starlette route on the HTTP app the streamable-HTTP transport serves. The handler is
        async because Starlette routes are async; it touches no config/store/tool — it cannot
        leak observability data.
        """
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response

        @self._mcp.custom_route("/healthz", methods=["GET"])
        async def _healthz(_request: Request) -> Response:
            """The liveness probe — a bare status object, no signal data, no auth."""
            return JSONResponse({"status": "ok"})

    def http_app(self) -> object:
        """Return the rendered Starlette HTTP app (the streamable-HTTP transport's ASGI app).

        Exposed so a SYNC test can drive `/healthz` (+ the MCP routes) with Starlette's
        `TestClient` without binding a socket. The return is typed `object` because the
        Starlette app type is part of the `FastMCP[Any]` boundary this adapter confines —
        the caller (a TestClient) consumes it as an ASGI callable, needing no concrete type.
        """
        app: object = self._mcp.http_app()
        return app


# The exact implemented read-only tool set (catalog / discovery / query). EVERY shipped tool
# is real — there are no call-time stubs (the v0.2 stub machinery was deleted once `get_cost`,
# the last stub, shipped in v0.3). The structural read-only test asserts a default-config server
# registers EXACTLY these (plus any injected-pack tools); a future write tool under any name
# would break it.
KNOWN_READ_ONLY_TOOLS: tuple[str, ...] = (
    "describe_signal_catalog",
    "list_dashboards",
    "get_dashboard_data",
    "query_metric",
    "search_incidents",
    "search_logs",
    "describe_health",
    # v0.2 — get_cluster_state renders the kubernetes snapshot from the store.
    "get_cluster_state",
    # v0.2 Phase 4 — get_slo + compare_envs.
    "get_slo",
    "compare_envs",
    # v0.3 Phase 3 — get_cost (the last tool to be promoted from a stub).
    "get_cost",
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
        # (every shipped tool registers one; consumer-pack tools may add their own).
        self._callables: dict[str, _ToolCallable] = {}

    def _register_tool[**ToolParams](
        self,
        name: str,
        fn: Callable[ToolParams, object],
        invoker: _ToolCallable | None = None,
    ) -> None:
        """Register a real-signature tool wrapper under `name` with FastMCP.

        `fn` keeps its real (typed, nested-`TypedDict`) signature so FastMCP can
        introspect it for schema generation — it is registered under the EXPLICIT
        `name` (independent of the wrapper's `__name__`). The name is mirrored into
        `_tool_names` for the synchronous structural read-only assertion. A
        `ParamSpec` keeps this `Any`-free while accepting any concrete wrapper.

        `invoker` is an OPTIONAL separate uniform-shape (`_ToolCallable`) handle that
        calls the SAME underlying tool logic. When supplied (every v0.1 core tool does),
        it is stored in `_callables` so a synchronous unit test can invoke that tool
        directly via `tool_callable(name)(...)` — without driving FastMCP's async stdio
        transport. FastMCP rejects `*args`/`**kwargs` tools, so the introspectable `fn`
        and the uniform `invoker` must be two separate handles. It stays OPTIONAL so the
        documented consumer-pack seam — `server._register_tool(name, fn)` — keeps working
        unchanged; a pack that wants the same synchronous invocability passes its own invoker
        (the core registrars derive theirs from the tool fn via `_make_invoker`).
        """
        self.mcp.tool(name, fn)
        self._tool_names.append(name)
        if invoker is not None:
            self._callables[name] = invoker

    def tool_names(self) -> list[str]:
        """The names of every registered tool (synchronous; the read-only test seam)."""
        return list(self._tool_names)

    def tool_callable(self, name: str) -> _ToolCallable:
        """The bound uniform-shape callable for a registered tool (synchronous test seam).

        Covers every shipped tool that registered an `invoker` handle, so a unit test can
        invoke it directly without driving FastMCP's async transport.
        """
        return self._callables[name]

    def run(self) -> None:
        """Run the server over the stdio transport (the v0.1 entrypoint)."""
        self.mcp.run()

    def run_http(self, host: str, port: int) -> None:
        """Run the server over the streamable-HTTP transport (the v0.2 hosted face).

        Delegates to the adapter's `run_http`, mirroring `run()` for stdio. The SAME
        tool table is served — only the transport differs (two faces, one store). The
        GitHub auth gate is the nginx ingress + oauth2-proxy's job, not this bind (see
        `_FastMcpAdapter.run_http`'s bind note).
        """
        self.mcp.run_http(host, port)

    def http_app(self) -> object:
        """Return the Starlette HTTP app the streamable-HTTP transport serves.

        Exposed so a SYNC test can drive `/healthz` + the MCP routes via Starlette's
        `TestClient` without binding a socket. Delegates to the adapter.
        """
        return self.mcp.http_app()


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


def build_server(
    config: ResolvedConfig,
    resolve: Callable[[str], object] | None = None,
) -> PanoptesMcpServer:
    """Construct the FastMCP stdio server + register core / stub / pack tools.

    Args:
        config: The resolved config (bound into every tool wrapper).
        resolve: The consumer-pack resolver seam — given the `PANOPTES_CONSUMER_PACK`
            reference, returns the loaded pack module (carrying `register_tools`). Defaults
            to the real file-path-primary + dotted-fallback `_import_consumer_pack`; a test
            injects a fake resolver returning an in-memory module to exercise the injection
            mechanism WITHOUT touching the filesystem or `sys.path`.

    Returns:
        A `PanoptesMcpServer` with all configured tools registered.
    """
    # Default to the real adapter here (not as a literal default arg) so the production
    # resolver function need not be in scope at the `def` site and the seam stays a single,
    # explicit indirection. A test passes its own `resolve` to bypass the filesystem.
    pack_resolver = resolve if resolve is not None else _import_consumer_pack

    mcp: FastMCP[object] = FastMCP("panoptes")
    server = PanoptesMcpServer(config, _FastMcpAdapter(mcp))

    # Register the unauthenticated `/healthz` liveness route on the HTTP app (spec § MCP
    # HTTP face). It is harmless on the stdio face (custom routes only surface on the HTTP
    # app) and is the single route the nginx ingress exempts from forward-auth — so it is
    # wired here, on the SAME server, for both faces (two faces, one server).
    server.mcp.register_health_route()

    # Build the small `QueryContext` seam ONCE from the resolved config and bind it
    # into every tool wrapper, so the tools depend on the context interface — not the
    # `ResolvedConfig` shape (the deep test seam: a tool test drives only the context).
    context = QueryContext(config)
    core_registrars = _core_registrars(context)
    for name in _configured_tool_names(config):
        registrar = core_registrars.get(name)
        if registrar is not None:
            registrar(server, name)
        # An unknown tool name (not a registered core tool) is ignored here — config-level
        # adapter validation is the loader's job; the MCP server only wires the tools it knows.
        # (No silent write path is created.) Every shipped tool is now real — the v0.2 call-time
        # "not available" stub machinery was deleted once the last stub (`get_cost`) shipped.

    _load_consumer_pack(server, resolve=pack_resolver)
    return server


# --- Uniform-invoker kwarg coercion (F3f synchronous test seam) ----------------------
# The `_ToolCallable` invoker shape is `(*args: object, **kwargs: object)`, so a test
# passes tool args by keyword and these helpers narrow each `object` value back to the
# concrete type the underlying core function needs — `object`-typed and isinstance-guarded
# so they carry no `Any` and never silently coerce a wrong-typed test argument.


def _str_kwarg(kwargs: dict[str, object], key: str) -> str:
    """Return the required str-valued kwarg `key`, raising on absence/wrong type."""
    value = kwargs.get(key)
    if not isinstance(value, str):
        raise TypeError(f"tool invoker expected str kwarg '{key}', got {type(value).__name__}")
    return value


def _opt_str_kwarg(kwargs: dict[str, object], key: str) -> str | None:
    """Return an optional str-valued kwarg `key` (None when absent), raising on wrong type."""
    value = kwargs.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"tool invoker expected str|None kwarg '{key}', got {type(value).__name__}")
    return value


def _str_dict_kwarg(kwargs: dict[str, object], key: str) -> dict[str, str] | None:
    """Return an optional `dict[str, str]` kwarg `key`.

    An absent key yields `None`; a present empty dict yields `{}` (NOT `None`); a present
    populated dict is coerced (raising on a non-str key/value).
    """
    value = kwargs.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"tool invoker expected dict kwarg '{key}', got {type(value).__name__}")
    coerced: dict[str, str] = {}
    for inner_key, inner_value in value.items():
        if not isinstance(inner_key, str) or not isinstance(inner_value, str):
            raise TypeError(f"tool invoker expected dict[str, str] for kwarg '{key}'")
        coerced[inner_key] = inner_value
    return coerced


# A single param-coercer: pulls one tool argument out of the invoker kwargs, narrowed to the
# concrete type its annotation declares. The three coercers above all share this shape.
_ParamCoercer = Callable[[dict[str, object], str], object]


def _coercer_for_annotation(annotation: object) -> _ParamCoercer:
    """Pick the kwarg coercer matching a tool-fn parameter's LIVE annotation.

    Dispatches on the raw `inspect.Parameter.annotation` (a real type object, because this
    module bans `from __future__ import annotations` — the PEP-563 guard test pins that):

    - `str` → `_str_kwarg` (a required string);
    - `str | None` (a `types.UnionType` over `str` + `NoneType`) → `_opt_str_kwarg`;
    - `dict[str, str] | None` (a `UnionType` whose non-`None` member is a `dict[...]` generic)
      → `_str_dict_kwarg`;
    - anything else → `_str_kwarg` (the dominant tool-param shape; a wrong fallback surfaces a
      clear `TypeError` from the coercer rather than silently dropping the argument).

    Never calls `typing.get_type_hints()` — that would re-resolve deferred string annotations
    and break on a PEP-563 module. Only `typing.get_args` / `typing.get_origin` on the already-
    live annotation are used (pure structural introspection, no re-resolution).
    """
    if annotation is str:
        return _str_kwarg
    # `X | None` is a `types.UnionType`; inspect its members structurally (no re-resolution).
    if isinstance(annotation, types.UnionType):
        members = typing.get_args(annotation)
        non_none = tuple(member for member in members if member is not type(None))
        if len(non_none) == 1:
            (inner,) = non_none
            # `dict[str, str]` is a generic alias whose origin is the builtin `dict`.
            if typing.get_origin(inner) is dict:
                return _str_dict_kwarg
            # `str | None` AND any other UNRECOGNIZED `X | None` route to the OPTIONAL coercer
            # (NIT-9): an `| None` param is optional, so an absent value must forward as `None`
            # rather than raising — using the required-str coercer for an unrecognized optional
            # would be the footgun. `_opt_str_kwarg` still type-checks a present value as str.
            return _opt_str_kwarg
    # An unrecognized NON-optional annotation → coerce as a required str (the dominant shape).
    return _str_kwarg


def _make_invoker[**ToolParams](tool_fn: Callable[ToolParams, object]) -> _ToolCallable:
    """Derive a uniform `_ToolCallable` invoker from a tool function's OWN signature.

    Replaces the 11 hand-mirrored `invoker(*_args, **kwargs)` closures. For each of `tool_fn`'s
    parameters it picks the coercer matching the parameter's LIVE annotation (via
    `_coercer_for_annotation`), pulls that argument out of the invoker kwargs narrowed to its
    concrete type, then calls `tool_fn(**extracted)`. Because each tool fn ALREADY encapsulates
    its tool→core rename internally (e.g. `query_metric_tool(env, metric, window)` calls
    `query_metric(context, name=metric, ...)`), the derivation needs ONLY the tool fn — no
    core function and no renames mapping.

    A `ParamSpec` (`ToolParams`) types the input so ANY concrete-signature tool wrapper is
    accepted without an explicit `Any` (the same `[**ToolParams]` form `_register_tool` uses —
    `Callable[..., object]` would trip `disallow_any_explicit`).

    The parameter coercers are resolved ONCE at registration time (the `tool_fn` signature is
    stable), so each invocation just applies them — no per-call `inspect` cost.

    Args:
        tool_fn: The introspectable, typed tool wrapper (the `*_tool` closure each `_register_*`
            builds). Its parameter ANNOTATIONS must be live type objects — guaranteed because
            this module bans `from __future__ import annotations` (see the module docstring +
            the PEP-563 guard test).

    Returns:
        A `_ToolCallable` (`(*args, **kwargs) -> object`) that narrows each kwarg and calls
        `tool_fn`; it ignores positional args (the uniform invoker is keyword-driven).
    """
    # Resolve (param_name, coercer) pairs once — the signature does not change after build.
    coercers: list[tuple[str, _ParamCoercer]] = [
        (parameter.name, _coercer_for_annotation(parameter.annotation))
        for parameter in inspect.signature(tool_fn).parameters.values()
    ]
    # The invoker calls `tool_fn` with a dynamically-built kwargs dict, which the static
    # `ParamSpec` signature cannot express. Bind a uniform-shape view of the SAME callable: a
    # single honest cast to the real `_ToolCallable` Protocol (the tool fn IS callable with the
    # extracted kwargs — its concrete signature is a subset of `(*args, **kwargs)`), not an
    # `Any`-launder. The coercers reproduce exactly the args each tool fn declares.
    uniform_tool_fn = typing.cast(_ToolCallable, tool_fn)

    def invoker(*_args: object, **kwargs: object) -> object:
        extracted = {name: coerce(kwargs, name) for name, coerce in coercers}
        return uniform_tool_fn(**extracted)

    return invoker


# --- Module-level core tool registrars (one per tool) --------------------------------
#
# Each registrar binds the `QueryContext` into the introspectable `*_tool` wrapper (FastMCP
# schema generation) and DERIVES the uniform `_ToolCallable` invoker from that wrapper's own
# signature via `_make_invoker` — the invoker is a faithful, transport-free stand-in for the
# registered tool (F3f). The derivation replaced 11 hand-mirrored invoker closures: because
# each `*_tool` wrapper already encapsulates the tool→core rename internally, `_make_invoker`
# needs only the wrapper to reproduce the call (no core_fn, no renames dict).
#
# These were lifted OUT of `_core_registrars` (where they were nested closures) to module
# scope so `_core_registrars` stays a flat, trivial dispatch dict — the v0.3 `get_cost`
# promotion would otherwise push the nested-closure version past the ruff C90 ceiling. Each
# takes `context` first (bound via `functools.partial` in `_core_registrars`), keeping the
# registrar's call shape `(server, name)`.


def _register_describe_signal_catalog(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def describe_signal_catalog_tool() -> SignalCatalog:
        """List environments, configured sources + capabilities, metrics, dashboards."""
        return describe_signal_catalog(context)

    server._register_tool(
        name, describe_signal_catalog_tool, _make_invoker(describe_signal_catalog_tool)
    )


def _register_list_dashboards(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def list_dashboards_tool() -> list[DashboardSummary]:
        """Return the dashboard catalog (core + injected consumer packs)."""
        return list_dashboards(context.dashboard_packs)

    server._register_tool(name, list_dashboards_tool, _make_invoker(list_dashboards_tool))


def _register_get_dashboard_data(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def get_dashboard_data_tool(dashboard_id: str, env: str) -> DashboardData:
        """Execute one dashboard's panels for `env`: title + PromQL + series."""
        return get_dashboard_data(dashboard_id, env, context)

    server._register_tool(name, get_dashboard_data_tool, _make_invoker(get_dashboard_data_tool))


def _register_query_metric(context: QueryContext, server: "PanoptesMcpServer", name: str) -> None:
    def query_metric_tool(
        env: str, metric: str, window: str, filters: dict[str, str] | None = None
    ) -> list[MetricSeries]:
        """Run a PromQL passthrough query for a metric against the store."""
        return query_metric(context, env=env, name=metric, window=window, filters=filters)

    server._register_tool(name, query_metric_tool, _make_invoker(query_metric_tool))


def _register_search_incidents(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def search_incidents_tool(
        env: str, window: str, tag: str | None = None, level: str | None = None
    ) -> list[IncidentSignal] | IncidentFanOut:
        """Search incident signals for `env` (or fan out across all enabled envs)."""
        return search_incidents(context, env=env, window=window, tag=tag, level=level)

    server._register_tool(name, search_incidents_tool, _make_invoker(search_incidents_tool))


def _register_search_logs(context: QueryContext, server: "PanoptesMcpServer", name: str) -> None:
    def search_logs_tool(
        env: str, query: str, window: str, level: str | None = None
    ) -> list[LogSignal] | LogFanOut:
        """Search log signals for `env` (or fan out across all enabled envs)."""
        return search_logs(context, env=env, query=query, window=window, level=level)

    server._register_tool(name, search_logs_tool, _make_invoker(search_logs_tool))


def _register_describe_health(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def describe_health_tool(env: str) -> HealthRollup:
        """Roll up per-source reachability + open-incident count for `env`."""
        return describe_health(context, env=env)

    server._register_tool(name, describe_health_tool, _make_invoker(describe_health_tool))


def _register_get_cluster_state(
    context: QueryContext, server: "PanoptesMcpServer", name: str
) -> None:
    def get_cluster_state_tool(env: str) -> ClusterState:
        """Render `env`'s kubernetes cluster snapshot from the stored k8s gauges."""
        return get_cluster_state(context, env=env)

    server._register_tool(name, get_cluster_state_tool, _make_invoker(get_cluster_state_tool))


def _register_get_slo(context: QueryContext, server: "PanoptesMcpServer", name: str) -> None:
    def get_slo_tool(env: str, slo_name: str) -> SloResult:
        """Evaluate the named SLO for `env`: objective vs. actual + the error budget."""
        return get_slo(context, env=env, name=slo_name)

    server._register_tool(name, get_slo_tool, _make_invoker(get_slo_tool))


def _register_compare_envs(context: QueryContext, server: "PanoptesMcpServer", name: str) -> None:
    def compare_envs_tool(metric: str, window: str) -> EnvComparison:
        """Compare one metric across every enabled env (per-env series + error markers)."""
        return compare_envs(context, metric=metric, window=window)

    server._register_tool(name, compare_envs_tool, _make_invoker(compare_envs_tool))


def _register_get_cost(context: QueryContext, server: "PanoptesMcpServer", name: str) -> None:
    def get_cost_tool(env: str, window: str) -> CostBreakdown:
        """Render `env`'s cost snapshot over `window` from the stored cost gauges."""
        return get_cost(context, env=env, window=window)

    server._register_tool(name, get_cost_tool, _make_invoker(get_cost_tool))


def _core_registrars(
    context: QueryContext,
) -> Mapping[str, Callable[[PanoptesMcpServer, str], None]]:
    """Map each core tool name to a registrar with `context` bound in (a flat dispatch dict).

    Each value is one of the module-level `_register_*` functions with `context` pre-bound
    via `functools.partial`, leaving the registrar's call shape `(server, name)`. Keeping
    this a flat dict (the registrars live at module scope) holds `_core_registrars`'s
    cyclomatic complexity at ~1, so adding a tool never inflates this function.
    """
    return {
        "describe_signal_catalog": functools.partial(_register_describe_signal_catalog, context),
        "list_dashboards": functools.partial(_register_list_dashboards, context),
        "get_dashboard_data": functools.partial(_register_get_dashboard_data, context),
        "query_metric": functools.partial(_register_query_metric, context),
        "search_incidents": functools.partial(_register_search_incidents, context),
        "search_logs": functools.partial(_register_search_logs, context),
        "describe_health": functools.partial(_register_describe_health, context),
        "get_cluster_state": functools.partial(_register_get_cluster_state, context),
        "get_slo": functools.partial(_register_get_slo, context),
        "compare_envs": functools.partial(_register_compare_envs, context),
        "get_cost": functools.partial(_register_get_cost, context),
    }


def _load_consumer_pack(
    server: PanoptesMcpServer, resolve: Callable[[str], object] | None = None
) -> None:
    """Resolve the consumer pack named by `PANOPTES_CONSUMER_PACK` and register its tools.

    Default unset = core-only (no resolve, no consumer coupling). When set, the pack is
    resolved via the injectable `resolve` seam and its `register_tools(mcp_server)` hook
    is called so the injected pack can add its own read-only tools. `core` never imports
    the pack statically — the pack is injected at deploy time, never bundled.

    Args:
        server: The MCP server the resolved pack registers its tools on.
        resolve: The pack resolver — given the `PANOPTES_CONSUMER_PACK` reference, returns
            the loaded module. Two real adapters justify the seam: the file-path
            (`spec_from_file_location`) primary and the dotted (`import_module`) fallback,
            both housed in the default `_import_consumer_pack`. A test injects a fake
            resolver to drive the registration WITHOUT any filesystem access.

    `PANOPTES_CONSUMER_PACK` is primarily a FILE PATH: the compose deployment mounts the
    consumer's pack as a single file at `/packs/consumer/pack.py` and points the env var at
    it, so a mounted (non-installed, not-on-`sys.path`) pack is loaded via the default
    resolver's `spec_from_file_location`. A dotted module name is also accepted as a
    fallback (an installed or in-repo importable pack).
    """
    # Default to the real two-adapter resolver; a test passes its own fake resolver.
    pack_resolver = resolve if resolve is not None else _import_consumer_pack
    pack_ref = os.environ.get(_CONSUMER_PACK_ENV_VAR)
    if not pack_ref:
        return
    module = pack_resolver(pack_ref)
    register_tools = getattr(module, "register_tools", None)
    if callable(register_tools):
        register_tools(server)


def _import_consumer_pack(pack_ref: str) -> object:
    """Load the consumer pack from a file path (deploy model) or a dotted module name.

    The DEFAULT resolver adapter for the `_load_consumer_pack` seam: it houses the two
    real resolution paths (file-path primary, dotted fallback) chosen by a `.py`/`/`/
    `os.sep` heuristic on the reference.
    """
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
    """CLI entrypoint: load the config from `PANOPTES_CONFIG` and run the selected server.

    Invoked as `python -m core.mcp.server`. The config path is read from the
    `PANOPTES_CONFIG` env var (the compose file sets it to the mounted config). The
    transport is selected by the config's `mcp.transport` field: `stdio` (the default,
    the local-dev face) runs the stdio server; `http` runs the streamable-HTTP server
    (the hosted face — two faces, one store). The already-loaded config is reused for
    the HTTP path; it is never re-loaded.
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

    # Dispatch on the configured transport. `http` reuses the SAME loaded config via the
    # HTTP entrypoint (imported lazily so the stdio path never drags in the HTTP runner);
    # anything else (incl. the default/unset) runs stdio.
    transport = config.mcp.get("transport", "stdio")
    if transport == "http":
        from core.mcp.http import run_http

        run_http(config)
    else:
        build_server(config).run()


if __name__ == "__main__":
    main()
