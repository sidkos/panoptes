"""Unit tests for the MCP streamable-HTTP transport (`core/mcp/http.py`).

These run in the BASE gate (no `-m integration` mark) and NEVER bind a real port: the
blocking server-runner is injected via the `run_http(config, *, runner=...)` seam, so a
fake runner records what it was handed instead of actually serving. This gives
`core/mcp/http.py` real unit coverage (the `core.mcp` ≥80 floor now includes it).

Covers (spec § MCP server — HTTP transport / plan Phase 5):
- `run_http` reuses the SAME `build_server(config)` as the stdio path — the registered
  tool table is IDENTICAL across the two faces (two faces, one store);
- the transport dispatch in `core.mcp.server.main` selects the HTTP path for a
  `mcp.transport: http` config and the stdio path otherwise (asserted via a spy, never
  a real bind);
- host/port are read from the environment (`PANOPTES_MCP_HOST`/`PANOPTES_MCP_PORT`) with
  sensible defaults, and handed to the runner.

No `asyncio`/`async`/`await` anywhere — the seam is a plain synchronous callable.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from core.config import (
    McpConfig,
    ResolvedConfig,
    ResolvedEnvironment,
    ResolvedSource,
)
from core.mcp.http import run_http
from core.mcp.server import KNOWN_READ_ONLY_TOOLS, PanoptesMcpServer, build_server
from core.model import (
    Alert,
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    fetch_when_unreachable = False

    def __init__(self, source_type: str, capabilities: set[SignalKind]) -> None:
        self.type = source_type
        self._capabilities = capabilities

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _FakeStore:
    type = "fake"

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _config(mcp: McpConfig | None = None) -> ResolvedConfig:
    """A minimal dev-only config (the tool table is what the parity assertion compares)."""
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    ResolvedSource(
                        source=_FakeSource("http-health", {SignalKind.METRIC}),
                        fetch_timeout_seconds=30,
                        poll_interval_seconds=60,
                    )
                ],
            )
        },
        store=_FakeStore(),
        notifiers=notifiers,
        dashboard_packs=[],
        slos=[],
        mcp=mcp if mcp is not None else {},
    )


def test_run_http_reuses_build_server_identical_tool_table() -> None:
    """`run_http` builds the server via the SAME `build_server(config)` as the stdio path.

    The registered tool table the HTTP face exposes must be IDENTICAL to the stdio face's
    — they are the same `build_server(config)` registration, only the transport differs
    (two faces, one store). The injected runner captures the built server so its
    `tool_names()` can be compared to a directly-built stdio server.
    """
    config = _config(mcp={"transport": "http"})
    captured: dict[str, object] = {}

    def _fake_runner(server: PanoptesMcpServer, host: str, port: int) -> None:
        # Record the server + bind args instead of serving (no port is bound).
        captured["server"] = server
        captured["host"] = host
        captured["port"] = port

    run_http(config, runner=_fake_runner)

    http_server = captured["server"]
    assert isinstance(http_server, PanoptesMcpServer)
    # The HTTP face's tool table equals the stdio face's (the SAME build_server output).
    stdio_server = build_server(config)
    assert set(http_server.tool_names()) == set(stdio_server.tool_names())
    # And it is exactly the known read-only set for this default config (no divergence).
    assert set(http_server.tool_names()) == set(KNOWN_READ_ONLY_TOOLS)


def test_run_http_reads_default_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env overrides, `run_http` binds the default container host/port (0.0.0.0:8080)."""
    monkeypatch.delenv("PANOPTES_MCP_HOST", raising=False)
    monkeypatch.delenv("PANOPTES_MCP_PORT", raising=False)
    captured: dict[str, object] = {}

    def _fake_runner(server: PanoptesMcpServer, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    run_http(_config(mcp={"transport": "http"}), runner=_fake_runner)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8080


def test_run_http_reads_host_and_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_http` honors `PANOPTES_MCP_HOST`/`PANOPTES_MCP_PORT` env overrides."""
    monkeypatch.setenv("PANOPTES_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("PANOPTES_MCP_PORT", "9099")
    captured: dict[str, object] = {}

    def _fake_runner(server: PanoptesMcpServer, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    run_http(_config(mcp={"transport": "http"}), runner=_fake_runner)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9099


def test_run_http_invalid_port_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric `PANOPTES_MCP_PORT` falls back to the default rather than crashing."""
    monkeypatch.setenv("PANOPTES_MCP_PORT", "not-a-number")
    captured: dict[str, object] = {}

    def _fake_runner(server: PanoptesMcpServer, host: str, port: int) -> None:
        captured["port"] = port

    run_http(_config(mcp={"transport": "http"}), runner=_fake_runner)
    assert captured["port"] == 8080


# --- transport dispatch in core.mcp.server.main ----------------------------------


def test_main_dispatches_http_for_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` routes a `mcp.transport: http` config to `core.mcp.http.run_http`.

    The dispatch is asserted via a spy on `run_http` + a spy on the stdio `run()` — only
    the HTTP path must fire for `transport: http`. No port is bound (both paths are spied).
    """
    import core.mcp.server as server_module

    http_calls: list[ResolvedConfig] = []
    stdio_calls: list[object] = []

    # Spy the HTTP entrypoint (imported lazily inside main) — patch it on its home module.
    import core.mcp.http as http_module

    def _spy_run_http(config: ResolvedConfig, *, runner: object = None) -> None:
        http_calls.append(config)

    monkeypatch.setattr(http_module, "run_http", _spy_run_http)

    # Spy the stdio path so a misroute is caught (it must NOT fire for http).
    def _spy_stdio_run(self: PanoptesMcpServer) -> None:
        stdio_calls.append(self)

    monkeypatch.setattr(PanoptesMcpServer, "run", _spy_stdio_run)

    # Drive main() with an http-transport config loaded from a written file.
    _run_main_with_transport(monkeypatch, server_module, "http")

    assert len(http_calls) == 1, "the http transport must dispatch to run_http"
    assert stdio_calls == [], "the stdio run() must NOT fire for transport: http"


def test_main_dispatches_stdio_for_default_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` routes a `mcp.transport: stdio` (default) config to the stdio `run()`."""
    import core.mcp.http as http_module
    import core.mcp.server as server_module

    http_calls: list[ResolvedConfig] = []
    stdio_calls: list[object] = []

    def _spy_run_http(config: ResolvedConfig, *, runner: object = None) -> None:
        http_calls.append(config)

    monkeypatch.setattr(http_module, "run_http", _spy_run_http)

    def _spy_stdio_run(self: PanoptesMcpServer) -> None:
        stdio_calls.append(self)

    monkeypatch.setattr(PanoptesMcpServer, "run", _spy_stdio_run)

    _run_main_with_transport(monkeypatch, server_module, "stdio")

    assert len(stdio_calls) == 1, "the stdio transport must dispatch to run()"
    assert http_calls == [], "run_http must NOT fire for transport: stdio"


def _run_main_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    server_module: object,
    transport: str,
) -> None:
    """Write a minimal config with the given `mcp.transport` and run `main()` against it.

    A passthrough store keeps the config resolvable with no live backend; the transport
    field is what `main` dispatches on. `main` is the real entrypoint — `register_core_adapters`
    runs so the passthrough store builds.

    The core adapter modules are evicted from `sys.modules` first so `main`'s
    `register_core_adapters()` genuinely RE-runs the `@STORES.register(...)` decorators
    (the F8 import-cache fix): under pytest the modules are already imported, so a warm-cache
    `import_module` would be a no-op and — combined with the root conftest restoring each
    registry around every test — `main` would observe an EMPTY registry and fail to build
    `passthrough`. The eviction is restored in `finally` so a sibling test's cache is intact.
    """
    import sys
    import tempfile
    from pathlib import Path

    from core.bootstrap import _CORE_ADAPTER_MODULES

    config_yaml = (
        "panoptes:\n"
        "  environments:\n"
        "    dev: {enabled: true, sources: []}\n"
        "  store: {type: passthrough}\n"
        f"  mcp: {{transport: {transport}}}\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(config_yaml)
        config_path = handle.name
    monkeypatch.setenv("PANOPTES_CONFIG", config_path)
    evicted = {name: sys.modules.pop(name) for name in _CORE_ADAPTER_MODULES if name in sys.modules}
    # `main` takes no args; it reads PANOPTES_CONFIG.
    main_fn: Callable[[], None] = server_module.main  # type: ignore[attr-defined]
    try:
        main_fn()
    finally:
        sys.modules.update(evicted)
        Path(config_path).unlink(missing_ok=True)
