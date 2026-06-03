# 4. Shared MCP metric helpers live in `core/mcp/_metric_helpers.py`

- Date: 2026-06-04
- Status: Accepted

## Context

The `read_gauge` / `read_series` store-reader methods on `QueryContext`
(`core/mcp/context.py`) need `escape_promql_value`, `_window_for`, `_step_seconds_for`, and
`_latest_value`. Those helpers originally lived in `core/mcp/tools_query.py`, which already
imports `QueryContext` from `context.py`. Implementing the readers by importing those
helpers back into `context.py` would create a **bidirectional** `context ↔ tools_query`
import — fragile, and a trap for every future contributor who touches either file.

`escape_promql_value` is additionally **public**: consumer packs and the Grafana `$env`
substitution import it from `core.mcp.tools_query`.

## Decision

The shared helpers live in a new leaf module `core/mcp/_metric_helpers.py` that imports only
`core.model` + stdlib (the bottom of the dependency graph). Both `context.py` and
`tools_query.py` import from it. `tools_query.py` **re-exports** `escape_promql_value` (via
`__all__`) so the public path `from core.mcp.tools_query import escape_promql_value` keeps
working unchanged.

## Consequences

- No import cycle (`python -c "import core.mcp.context, core.mcp.tools_query"` succeeds).
- The PromQL-injection escape — a security invariant — has exactly one home, applied
  unconditionally inside `read_series` (which `read_gauge` delegates to), so all gauge-read
  call sites share it.
- The public `escape_promql_value` contract is preserved via the re-export; consumer packs
  are unaffected.
- Future shared MCP metric helpers go in `_metric_helpers.py`, not in `tools_query.py` or
  `context.py`, to keep the boundary acyclic.
