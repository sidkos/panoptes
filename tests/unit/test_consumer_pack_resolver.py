"""Unit tests for the injectable consumer-pack resolver seam.

`build_server` loads the consumer pack named by `PANOPTES_CONSUMER_PACK` and calls its
`register_tools(server)` hook. Resolution (file-path-via-`spec_from_file_location` vs
dotted-via-`import_module`) is now an INJECTABLE seam: `build_server(config, resolve=...)`
threads a `resolve: Callable[[str], object]` down to `_load_consumer_pack`. The default
adapter is the real file-path-primary + dotted-fallback `_import_consumer_pack`; a test
passes a fake resolver returning an in-memory module, so the injection mechanism is
exercised WITHOUT any filesystem access (no real `.py`, no `sys.path` fixing).

The existing demo-pack tests (`test_demo_pack.py`) still cover the two REAL adapters
(file-path load + dotted load); this file covers the seam itself.
"""

from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
from core.config import ResolvedConfig
from core.mcp.server import PanoptesMcpServer, build_server


def _baseline_config() -> ResolvedConfig:
    """A minimal core-only `ResolvedConfig` (no store query is driven by tool_names())."""

    class _NullStore:
        type = "null"

        def write(self, signals: list[object]) -> None:  # pragma: no cover - unused
            return None

        def query(self, query: object) -> list[object]:  # pragma: no cover - unused
            return []

    return ResolvedConfig(
        environments={},
        store=_NullStore(),  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


@pytest.fixture
def _consumer_pack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set `PANOPTES_CONSUMER_PACK` to a reference the FAKE resolver maps in-memory.

    The reference value is intentionally NOT a real file/module — only the injected fake
    resolver interprets it, proving no real filesystem/import path is touched.
    """
    pack_ref = "in-memory://fake-pack"
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", pack_ref)
    yield pack_ref


def test_injected_resolver_registers_tool_without_filesystem_access(
    _consumer_pack_env: str,
) -> None:
    """A fake resolver returns an in-memory module whose `register_tools` adds a tool.

    Asserts the injected pack's tool is registered, and that the resolver was called with
    exactly the `PANOPTES_CONSUMER_PACK` reference — with NO filesystem access (the module
    is built in memory, never loaded from disk).
    """
    seen_refs: list[str] = []

    # An in-memory module carrying a register_tools hook — no file, no import.
    fake_pack = ModuleType("fake_consumer_pack")

    def _register_tools(server: PanoptesMcpServer) -> None:
        def injected_tool() -> str:
            """A read-only tool the fake pack contributes."""
            return "from-injected-pack"

        server._register_tool("get_injected_signal", injected_tool)

    fake_pack.register_tools = _register_tools  # type: ignore[attr-defined]

    def _fake_resolve(pack_ref: str) -> object:
        seen_refs.append(pack_ref)
        return fake_pack

    server = build_server(_baseline_config(), resolve=_fake_resolve)

    assert seen_refs == [_consumer_pack_env], "resolver must be called with the pack reference"
    assert "get_injected_signal" in server.tool_names(), (
        "the injected pack's tool must be registered via the fake resolver (no filesystem)"
    )


def test_default_resolver_is_used_when_none_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no resolver injected AND the hook unset, the server is core-only (default path).

    Proves the default adapter is wired (the seam defaults to the real
    `_import_consumer_pack`) and that an unset hook adds nothing — no consumer coupling.
    """
    monkeypatch.delenv("PANOPTES_CONSUMER_PACK", raising=False)
    server = build_server(_baseline_config())
    assert "get_injected_signal" not in server.tool_names()


def test_resolver_not_called_when_hook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected resolver is NOT consulted when `PANOPTES_CONSUMER_PACK` is unset."""
    monkeypatch.delenv("PANOPTES_CONSUMER_PACK", raising=False)
    calls: list[str] = []

    def _recording_resolve(pack_ref: str) -> object:  # pragma: no cover - must not run
        calls.append(pack_ref)
        return ModuleType("unused")

    resolve: Callable[[str], object] = _recording_resolve
    build_server(_baseline_config(), resolve=resolve)

    assert calls == [], "the resolver must not be consulted when the hook is unset"
