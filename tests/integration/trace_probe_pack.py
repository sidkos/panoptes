"""An integration-test-only consumer pack that exposes the trace-probe surface.

The negative end-to-end assertion (spec `## Tests` → Integration) requires asking
the LIVE MCP server for traces over the real stdio transport and confirming the
explicit "no trace source" `CapabilityError` surfaces over the wire. `core`'s
`search_traces` capability-negotiation function is implemented but is deliberately
NOT in the server's wired-in tool set, so there is no built-in trace tool to call
over stdio.

Rather than modifying `core` (forbidden — and `core` correctly stays trace-toolless
in v0.1), this pack registers `search_traces` through the SAME documented consumer
injection hook a real consumer would use (`register_tools(mcp_server)`), wrapping the
real `core.mcp.tools_query.search_traces`. The negative test points
`PANOPTES_CONSUMER_PACK` at this file so the genuine core capability-negotiation
error travels the real transport — proving the negative path end-to-end without any
core change and without a hand-rolled fake.

This pack lives under `tests/integration/` (not `examples/`) because it is purely a
test fixture for the negative-path assertion; the brand-neutral demo pack under
`examples/demo-pack/` remains the positive-path injection fixture.

IMPORTANT (FastMCP / PEP-563): no `from __future__ import annotations` — deferred
annotations break FastMCP's schema generation. The registered tool presents concrete
`env` / `window` params (FastMCP rejects `*args`/`**kwargs`).
"""

from core.mcp.context import QueryContext
from core.mcp.tools_query import search_traces


def register_tools(mcp_server: object) -> None:
    """Register a read-only `search_traces` tool bound to the server's resolved config.

    Mirrors the demo pack's hook shape: resolve the server's bound config dynamically
    (depending on the documented hook contract, not the server's concrete type), wrap
    it in the `QueryContext` seam the tools now consume, and register a thin wrapper
    that delegates to the real core `search_traces`, which raises the explicit "no
    trace source" `CapabilityError` in v0.1.
    """
    config = mcp_server._config  # type: ignore[attr-defined]
    context = QueryContext(config)

    def search_traces_tool(env: str, window: str) -> list[object]:
        """Probe for trace signals — always the explicit 'no trace source' error in v0.1."""
        return search_traces(context, env=env, window=window)

    mcp_server._register_tool("search_traces", search_traces_tool)  # type: ignore[attr-defined]
