"""Unit tests for `_make_invoker` — the signature-derived MCP tool invoker (deepening C).

`_make_invoker` replaces 11 hand-mirrored `invoker(*_args, **kwargs)` closures: it derives the
uniform `_ToolCallable` invoker from the tool function's OWN signature. For each parameter it
dispatches on the LIVE `inspect.Parameter.annotation` (a real type object — this module bans
`from __future__ import annotations`) to the matching kwarg coercer (`_str_kwarg` /
`_opt_str_kwarg` / `_str_dict_kwarg`), builds the typed kwargs, and calls `tool_fn(**kwargs)`.

Because the tool function ALREADY encapsulates the tool→core rename internally (e.g.
`query_metric_tool(env, metric, window)` calls `query_metric(context, name=metric, ...)`), the
invoker is derived from the tool fn ALONE — no core_fn, no renames dict.

The PEP-563 liveness guard is load-bearing: if a future contributor adds
`from __future__ import annotations` to `core/mcp/server.py`, `inspect.Parameter.annotation`
would become a deferred `str` and the type-dispatch would silently misfire. A guard test
pins that a real registered tool fn's annotations are live `type`/`UnionType` objects, so that
regression fails RED here.
"""

import types

import pytest
from core.mcp.server import (
    _make_invoker,
    _str_dict_kwarg,
    _str_kwarg,
)


def test_make_invoker_extracts_and_forwards_a_required_str_param() -> None:
    """A tool fn with one required `str` param → the invoker extracts + forwards it by keyword."""
    received: dict[str, object] = {}

    def env_tool(env: str) -> str:
        received["env"] = env
        return f"ran for {env}"

    invoker = _make_invoker(env_tool)
    result = invoker(env="dev")

    assert result == "ran for dev"
    assert received == {"env": "dev"}


def test_make_invoker_forwards_optional_str_param_as_none_when_absent() -> None:
    """A `str | None` param absent from the kwargs is forwarded as `None` (not a KeyError)."""
    received: dict[str, object] = {}

    def search_tool(env: str, level: str | None = None) -> str:
        received["env"] = env
        received["level"] = level
        return "ok"

    invoker = _make_invoker(search_tool)
    invoker(env="dev")  # `level` omitted → forwarded as None

    assert received == {"env": "dev", "level": None}


def test_make_invoker_forwards_optional_str_param_when_present() -> None:
    """A `str | None` param present in the kwargs is forwarded verbatim."""
    received: dict[str, object] = {}

    def search_tool(env: str, level: str | None = None) -> str:
        received["level"] = level
        return "ok"

    _make_invoker(search_tool)(env="dev", level="error")

    assert received["level"] == "error"


def test_make_invoker_handles_internal_tool_to_core_rename() -> None:
    """A tool fn that renames a param to the core fn's name works end-to-end via the invoker.

    Mirrors `query_metric_tool(env, metric, ...)` calling `query_metric(context, name=metric)`:
    the rename is INTERNAL to the tool fn, so invoking with `metric=...` reaches the core under
    `name=...` for free — `_make_invoker` needs no renames dict.
    """
    reached_core: dict[str, object] = {}

    def _core(*, name: str) -> str:
        reached_core["name"] = name
        return f"core saw {name}"

    def metric_tool(metric: str) -> str:
        # The tool fn does the rename metric -> name (exactly like query_metric_tool).
        return _core(name=metric)

    result = _make_invoker(metric_tool)(metric="cpu")

    assert result == "core saw cpu"
    assert reached_core == {"name": "cpu"}


def test_make_invoker_forwards_a_dict_param() -> None:
    """A `dict[str, str] | None` param is coerced + forwarded (and None when absent)."""
    received: dict[str, object] = {}

    def filtered_tool(env: str, filters: dict[str, str] | None = None) -> str:
        received["filters"] = filters
        return "ok"

    invoker = _make_invoker(filtered_tool)
    invoker(env="dev", filters={"job": "api"})
    assert received["filters"] == {"job": "api"}

    invoker(env="dev")  # filters omitted → None
    assert received["filters"] is None


def test_make_invoker_calls_a_zero_arg_tool_with_no_kwargs() -> None:
    """A tool fn with NO parameters is invoked with no kwargs (ignoring any passed args)."""
    calls: list[int] = []

    def catalog_tool() -> str:
        calls.append(1)
        return "catalog"

    result = _make_invoker(catalog_tool)()

    assert result == "catalog"
    assert calls == [1]


def test_make_invoker_raises_on_missing_required_str_kwarg() -> None:
    """A required `str` param absent from the kwargs surfaces the coercer's `TypeError`.

    The derived invoker reuses `_str_kwarg`, whose contract is to raise on absence/wrong type —
    so a caller that omits a required arg gets a clear error, not a silent wrong call.
    """

    def env_tool(env: str) -> str:
        return env

    with pytest.raises(TypeError):
        _make_invoker(env_tool)()  # `env` missing


def test_unrecognized_annotation_falls_back_to_str_coercion() -> None:
    """A param whose annotation is none of the three known forms falls back to `_str_kwarg`.

    The fallback keeps a future tool param type from silently dropping the arg; it coerces as a
    required str (the dominant tool-param shape), surfacing a clear error if that is wrong.
    """
    received: dict[str, object] = {}

    def odd_tool(env: int) -> str:  # `int` is not str / str|None / dict[str,str]|None
        received["env"] = env
        return "ok"

    # Falls back to _str_kwarg, which requires a str — a str value forwards fine.
    _make_invoker(odd_tool)(env="dev")
    assert received["env"] == "dev"


# --- the PEP-563 liveness guard (load-bearing) ----------------------------------


def test_kwarg_coercers_are_module_level_and_importable() -> None:
    """The kwarg coercers stay module-level (additive — consumer packs may reuse them)."""
    assert callable(_str_kwarg)
    assert callable(_str_dict_kwarg)


def test_real_tool_fn_annotations_are_live_types_not_deferred_strings() -> None:
    """PEP-563 GUARD: a real registered tool fn's `Parameter.annotation` is a LIVE type.

    `_make_invoker` dispatches on `inspect.Parameter.annotation` as a real type object — which
    is only true while `core/mcp/server.py` does NOT use `from __future__ import annotations`.
    If a contributor adds that import, the annotations become deferred `str`s and the dispatch
    silently misfires. This pins the invariant: build a server, then assert the registered
    `query_metric` tool's parameter annotations are live `type`/`UnionType` objects (never
    `str`). A regression here is the EARLY warning a contributor needs.
    """
    import inspect

    from core.mcp.server import _register_query_metric

    # Capture the tool fn the registrar hands to `_register_tool` (the FastMCP-facing wrapper),
    # without a full build — a tiny fake server records the registered fn.
    captured: dict[str, object] = {}

    class _CaptureServer:
        def _register_tool(self, name: str, fn: object, invoker: object) -> None:
            captured["fn"] = fn

    # `context` is unused by signature inspection; a bare object satisfies the closure capture.
    _register_query_metric(object(), _CaptureServer(), "query_metric")  # type: ignore[arg-type]
    tool_fn = captured["fn"]
    assert callable(tool_fn)

    parameters = inspect.signature(tool_fn).parameters
    assert parameters, "the query_metric tool fn must declare parameters to inspect"
    for parameter in parameters.values():
        annotation = parameter.annotation
        # The load-bearing assertion: a LIVE type object, never a deferred PEP-563 string.
        assert not isinstance(annotation, str), (
            f"parameter {parameter.name!r} has a DEFERRED string annotation "
            f"({annotation!r}) — `from __future__ import annotations` must NOT be added to "
            "core/mcp/server.py (it breaks _make_invoker's signature-derived dispatch)."
        )
        assert isinstance(annotation, type | types.UnionType), (
            f"parameter {parameter.name!r} annotation {annotation!r} is neither a type nor a "
            "UnionType — _make_invoker's dispatch expects a live type object."
        )
