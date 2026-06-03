# 3. No `typing.get_type_hints()` in `core/mcp/*` (PEP-563 / FastMCP)

- Date: 2026-06-04
- Status: Accepted

## Context

FastMCP generates each tool's JSON schema by inspecting the tool function's annotations at
registration time. Under deferred annotations (`from __future__ import annotations`, PEP 563)
those annotations are *strings*, which breaks FastMCP's schema generation for the nested
`TypedDict` returns the tools expose. So every module under `core/mcp/` bans
`from __future__ import annotations`.

`core/mcp/server.py::_make_invoker` derives a tool's transport-free invoker from its
`inspect.signature`, dispatching each parameter on its annotation (`str` → `_str_kwarg`,
`str | None` → `_opt_str_kwarg`, …). This relies on the annotation being a **live type
object**, which it is *only* because the module is PEP-563-free.

## Decision

`_make_invoker` (and any future signature-introspecting code in `core/mcp/*`) must read
`inspect.Parameter.annotation` directly and **must never call `typing.get_type_hints()`**.
`get_type_hints()` re-resolves deferred string annotations against module globals and can
raise `NameError` / silently mis-dispatch on a PEP-563 module.

A liveness-guard test (`test_make_invoker.py`) asserts a real tool fn's
`Parameter.annotation` is a live `type | types.UnionType`, never a `str` — so adding
`from __future__ import annotations` to the module, or reintroducing `get_type_hints()`,
red-bars instead of failing silently.

## Consequences

- The invoker derivation stays correct and `Any`-free.
- The PEP-563 ban in `core/mcp/*` is now load-bearing for two reasons (FastMCP schema-gen
  *and* the invoker derivation), pinned by the guard test.
- If a future FastMCP version inspects annotations lazily, this constraint can be revisited;
  until then, this ADR closes any "just use `get_type_hints()`" simplification.
