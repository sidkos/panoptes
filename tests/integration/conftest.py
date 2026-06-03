"""Integration harness — ephemeral VictoriaMetrics + Grafana + a synchronous MCP
stdio client, with synthetic-only data (Risk R10).

This conftest spins the real stack the Phase-8 integration tests exercise and hands
the test modules typed, ready-to-use fixtures:

- `victoriametrics`: a live single-node VictoriaMetrics container; its host base URL
  (`base_url`) plus synthetic-write + PromQL-read helpers, all polling for readiness
  (Risk R13 — no immediate assertion after container start).
- `grafana`: a live Grafana-OSS container provisioned with the core dashboard packs
  AND the injected demo consumer pack (proving the mount-injection path), reachable
  via its host base URL; plus a `poll_search_titles` helper.
- `mcp_stdio_client`: a factory that spawns `python -m core.mcp.server` over the real
  stdio transport (FastMCP's `Client`, driven SYNCHRONOUSLY) with a chosen consumer
  pack + a config bound to the live VM — so no `asyncio` leaks into a test body
  (spec § stdio MCP launch contract).

Why testcontainers (not `docker compose up`): the suite must be hermetic and
self-contained in CI with synthetic data only — no `.env`, no live AWS/Sentry. The
SAME images the compose file declares are spun ephemerally here; the compose file is
the operator's live-credential journey, not the CI path.

The `pytest_collection_modifyitems` hook enforces the Phase-8 anti-rot floor: an
integration run that collects fewer than 5 integration tests FAILS (keyed to the
assertion count, not the file count) — the in-suite half of the re-tightened gate.

No async in test bodies: the only `asyncio.run` in the whole suite is confined to
`_StdioMcpClient.call_tool` / `list_tool_names` here (spec § stdio MCP launch
contract); the FastMCP `Client` is async-only, so a thin sync runner wraps each
single round-trip and the no-async-in-tests rule holds for every test module.
"""

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from core.mcp.tools_discovery import (
    _DASHBOARD_QUERY_MINUTES,
    _DASHBOARD_QUERY_STEP_SECONDS,
)
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# --- Images (the SAME tags the compose file declares) ----------------------------
_VM_IMAGE = "victoriametrics/victoria-metrics:v1.103.0"
_GRAFANA_IMAGE = "grafana/grafana-oss:11.2.0"
_VM_PORT = 8428
_GRAFANA_PORT = 3000

# Repo root (this file is tests/integration/conftest.py → up two parents).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Readiness polling bounds (Risk R13). Generous so a cold container pull/boot never
# races the first assertion; the loop exits early the moment readiness is observed.
_READY_TIMEOUT_SECONDS = 180.0
_READY_INTERVAL_SECONDS = 1.0

# The Phase-8 anti-rot floor: an integration run must collect at least this many
# tests (keyed to the assertion count — collector-to-VM, grafana-provisioning, and
# the three separate test_mcp_stdio_e2e functions). The in-suite enforcement is the
# companion to the CI job's `--collect-only` count check (spec Phase 8 exit criterion).
_MIN_INTEGRATION_TESTS = 5

# The four dashboard titles that must appear in Grafana once provisioning settles:
# the three core packs + the injected demo (consumer) pack. Titles (not UIDs)
# because the shipped dashboard.json files carry no `uid` (Grafana auto-assigns one).
_EXPECTED_DASHBOARD_TITLES = frozenset(
    {"Errors (Sentry)", "Logs", "Overview", "Demo (consumer pack)"}
)


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Fail a WHOLE `-m integration` run that collects fewer than the anti-rot floor.

    Mirrors the CI `--collect-only` count gate INSIDE the suite so neither an empty
    collection nor a quiet file-consolidation can silently shrink integration
    coverage below the floor (spec Phase 8 / Risk R7). Only enforced when the run is
    actually scoped to the `integration` marker — a plain unit run (`-m "not
    integration"`) collects zero integration items by design and must not be failed.

    The floor is a WHOLE-SUITE guard: a developer narrowing the run to a single node
    id (`...::test_parity`) or a `-k` filter for iteration is NOT trying to shrink the
    committed suite, so those narrowed invocations are exempt — only an unnarrowed
    `pytest -m integration` (exactly what CI runs) is gated. This keeps single-test
    debugging usable while still red-barring a genuine collection/consolidation loss.
    """
    marker_expr = config.getoption("-m", default="")
    if not isinstance(marker_expr, str) or "integration" not in marker_expr:
        return
    # Exclude a negated expression like "not integration" (no integration items).
    if "not integration" in marker_expr.replace("  ", " "):
        return
    # Exempt explicitly-narrowed runs: a `-k` filter, or any positional arg that names
    # a specific node id (contains `::`). The whole-suite CI invocation passes neither.
    if config.getoption("keyword", default=""):
        return
    if any("::" in str(arg) for arg in config.args):
        return
    integration_items = [item for item in items if item.get_closest_marker("integration")]
    if len(integration_items) < _MIN_INTEGRATION_TESTS:
        raise pytest.UsageError(
            f"Integration anti-rot floor: expected >= {_MIN_INTEGRATION_TESTS} "
            f"integration tests, collected {len(integration_items)}. A collection "
            "error or a file-consolidation must not silently shrink the suite."
        )


def _poll_until[ReadyResult](
    predicate: Callable[[], ReadyResult | None],
    *,
    timeout_seconds: float = _READY_TIMEOUT_SECONDS,
    interval_seconds: float = _READY_INTERVAL_SECONDS,
    description: str,
) -> ReadyResult:
    """Poll `predicate` (bounded retry/backoff) until it returns a non-None value.

    The single readiness primitive every fixture/helper uses (Risk R13): never an
    immediate assertion. Returns the first truthy result; raises a clear timeout error
    naming `description` if the bound elapses first.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: str = "predicate never returned a value"
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # transport errors during boot are expected; retry.
            last_error = f"{type(exc).__name__}: {exc}"
            result = None
        if result is not None:
            return result
        time.sleep(interval_seconds)
    raise TimeoutError(
        f"Timed out after {timeout_seconds:.0f}s waiting for {description} (last: {last_error})."
    )


# --- VictoriaMetrics fixture -----------------------------------------------------


@dataclass(frozen=True)
class VictoriaMetricsHandle:
    """A live VictoriaMetrics container: its host base URL + synthetic write/read seams."""

    base_url: str

    def import_samples(self, lines: list[str]) -> None:
        """POST JSON-line import bodies to `/api/v1/import` (the VM write path).

        Each line is a VictoriaMetrics import object (the exact shape
        `VictoriaMetricsStore._to_import_line` produces). Raises on a non-2xx so a
        rejected synthetic write is loud, not silent.
        """
        body = "\n".join(lines).encode("utf-8")
        with httpx.Client(timeout=10.0) as client:
            response = client.post(f"{self.base_url}/api/v1/import", content=body)
            response.raise_for_status()

    def wait_ready(self) -> None:
        """Poll `/health` until VM answers 200 (Risk R13) before any read-back."""

        def _probe() -> bool | None:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/health")
                return True if response.status_code == 200 else None

        _poll_until(_probe, description="VictoriaMetrics /health")

    def wait_for_series(self, expr: str) -> None:
        """Poll an instant `/api/v1/query` until `expr` returns at least one result.

        Used after a synthetic import to absorb VM's brief ingest-visibility lag
        before a test issues its range read-back — so the read-back is never racy.
        """

        def _probe() -> bool | None:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/v1/query", params={"query": expr})
                if response.status_code != 200:
                    return None
                payload = response.json()
                result = payload.get("data", {}).get("result", [])
                return True if result else None

        _poll_until(_probe, description=f"VictoriaMetrics series for {expr!r}")


@pytest.fixture(scope="session")
def victoriametrics() -> Iterator[VictoriaMetricsHandle]:
    """Spin an ephemeral single-node VictoriaMetrics, yield a host-reachable handle."""
    # Imported lazily so a unit-only run never imports testcontainers/docker.
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(_VM_IMAGE)
        .with_exposed_ports(_VM_PORT)
        # Persist nowhere special: the ephemeral container's default storage is fine
        # for a test run (synthetic data only; torn down at session end).
        .with_command("--storageDataPath=/storage --retentionPeriod=30d")
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(_VM_PORT))
        handle = VictoriaMetricsHandle(base_url=f"http://{host}:{port}")
        handle.wait_ready()
        yield handle
    finally:
        container.stop()


# --- Grafana fixture -------------------------------------------------------------


@dataclass(frozen=True)
class GrafanaHandle:
    """A live Grafana container provisioned with core + injected-demo dashboard packs."""

    base_url: str

    def wait_api_ready(self) -> None:
        """Poll `/api/health` until Grafana answers 200 (Risk R13)."""

        def _probe() -> bool | None:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/health")
                return True if response.status_code == 200 else None

        _poll_until(_probe, description="Grafana /api/health")

    def poll_search_titles(self, expected: frozenset[str]) -> set[str]:
        """Poll `GET /api/search` until every `expected` dashboard title appears.

        Provisioning is async (Risk R13): Grafana's file provider rescans on an
        interval, so the dashboards are NOT present the instant the container is up.
        Returns the observed title set once it is a superset of `expected`.
        """

        def _probe() -> set[str] | None:
            with httpx.Client(timeout=5.0) as client:
                # Anonymous access is enabled with an Admin role on this test
                # container, so /api/search needs no auth header.
                response = client.get(f"{self.base_url}/api/search", params={"type": "dash-db"})
                if response.status_code != 200:
                    return None
                titles = {
                    str(entry.get("title")) for entry in response.json() if isinstance(entry, dict)
                }
                return titles if expected.issubset(titles) else None

        return _poll_until(
            _probe,
            description=f"Grafana dashboards {sorted(expected)}",
        )


def _sync_dashboards_to(generated_dir: Path, vm_base_url: str) -> None:
    """Provision core + injected-demo dashboard JSON into `generated_dir`.

    Drives the REAL `GrafanaDashboardProvider.provision` over the resolved core packs
    + the injected demo consumer pack, so the synced tree is exactly what the Grafana
    file provider then loads — the mount-injection path under test. No Grafana URL is
    passed to the provider here (the post-sync ping is the container's job).
    """
    # Imported lazily (registers the provider) to keep unit collection import-light.
    from core.dashboards.grafana import GrafanaDashboardProvider
    from core.model import DashboardPack

    packs = [
        DashboardPack(
            id="errors-sentry",
            tier="core",
            json_path=_REPO_ROOT / "core/dashboards/errors-sentry/dashboard.json",
        ),
        DashboardPack(
            id="logs",
            tier="core",
            json_path=_REPO_ROOT / "core/dashboards/logs/dashboard.json",
        ),
        DashboardPack(
            id="overview",
            tier="core",
            json_path=_REPO_ROOT / "core/dashboards/overview/dashboard.json",
        ),
        # The injected consumer pack: its json_path is the mounted DIR (the provider
        # globs dashboards/**/dashboard.json under it) — proving injection by glob.
        DashboardPack(
            id="consumer",
            tier="consumer",
            json_path=_REPO_ROOT / "examples/demo-pack",
            selector="path",
        ),
    ]
    provider = GrafanaDashboardProvider({"provisioning_dir": str(generated_dir)})
    try:
        provider.provision(packs)
    finally:
        # Release the provider's long-lived httpx socket (F2c) so the run is clean under
        # `pytest -m integration -W error::ResourceWarning`.
        provider.close()


@pytest.fixture(scope="session")
def grafana(
    victoriametrics: VictoriaMetricsHandle, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[GrafanaHandle]:
    """Spin Grafana provisioned with core + injected-demo packs; yield a host handle.

    The provider syncs both tiers into a host temp dir which is bind-mounted into the
    container at the provisioning path the dashboards.yaml `${PANOPTES_PROVISIONING_DIR}`
    resolves to; the datasource.yaml `${VM_URL}` resolves to the VM container's host
    URL (Grafana reaches the host-published VM port from inside the container via the
    Docker host gateway).
    """
    from testcontainers.core.container import DockerContainer

    generated_dir = tmp_path_factory.mktemp("grafana-provisioning")
    # The host temp dir is owner-only (0700) by default; make it world-readable so the
    # in-container grafana user (uid 472) can read the bind-mounted provisioning on
    # Linux (Docker Desktop's macOS file-sharing is permissive and hides this).
    generated_dir.chmod(0o755)
    _sync_dashboards_to(generated_dir, victoriametrics.base_url)

    # Mount the provisioned dashboards OUTSIDE Grafana's data dir. A read-only bind
    # mount inside `/var/lib/grafana` (GF_PATHS_DATA, owned by uid 472) is fragile on
    # Linux — it interferes with Grafana initializing its data path and the container
    # never opens its port (works on macOS Docker Desktop, hangs on the Linux CI
    # runner). A neutral path keeps the file provider happy without touching the data dir.
    provisioning_dir = "/etc/panoptes/dashboards"
    container = (
        DockerContainer(_GRAFANA_IMAGE)
        .with_exposed_ports(_GRAFANA_PORT)
        # host.docker.internal does not resolve inside a Linux container by default;
        # map it to the host gateway so the datasource URL is resolvable on the runner.
        .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
        # Anonymous Admin so the test's /api/search needs no credentials (read-only
        # test surface — synthetic data only).
        .with_env("GF_AUTH_ANONYMOUS_ENABLED", "true")
        .with_env("GF_AUTH_ANONYMOUS_ORG_ROLE", "Admin")
        .with_env("GF_USERS_ALLOW_SIGN_UP", "false")
        # Resolved inside the provisioning YAMLs.
        .with_env("PANOPTES_PROVISIONING_DIR", provisioning_dir)
        # The datasource points at VM via the Docker host gateway (the VM port is
        # published on the host); Grafana need not load data for the search test, but
        # a resolvable datasource keeps provisioning clean.
        .with_env(
            "VM_URL", "http://host.docker.internal:" + victoriametrics.base_url.rsplit(":", 1)[1]
        )
        # Provision config + the synced dashboards (the mount-injection path).
        .with_volume_mapping(
            str(_REPO_ROOT / "core/dashboards/provisioning/datasource.yaml"),
            "/etc/grafana/provisioning/datasources/panoptes.yaml",
            "ro",
        )
        .with_volume_mapping(
            str(_REPO_ROOT / "core/dashboards/provisioning/dashboards.yaml"),
            "/etc/grafana/provisioning/dashboards/panoptes.yaml",
            "ro",
        )
        .with_volume_mapping(str(generated_dir), provisioning_dir, "ro")
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(_GRAFANA_PORT))
        handle = GrafanaHandle(base_url=f"http://{host}:{port}")
        handle.wait_api_ready()
        yield handle
    finally:
        container.stop()


# --- MCP stdio client fixture ----------------------------------------------------


def _write_integration_config(config_path: Path, vm_base_url: str) -> None:
    """Write a synthetic-only Panoptes config bound to the live VM (no AWS/Sentry).

    A single enabled `dev` env with one `http-health` source pointed at the live VM's
    own `/health` (a real, reachable URL — so `describe_health` reports it reachable)
    and the `victoriametrics` store pointed at the live VM. This keeps the MCP e2e
    fully hermetic (Risk R10) while exercising the real store + a real source.
    """
    config = {
        "panoptes": {
            "environments": {
                "dev": {
                    "enabled": True,
                    "sources": [{"type": "http-health", "url": f"{vm_base_url}/health"}],
                }
            },
            "store": {"type": "victoriametrics", "url": vm_base_url},
            "notifiers": [{"type": "logging"}],
            "dashboards": {
                "provider": "grafana",
                "env_variable": True,
                "core_packs": ["errors-sentry", "logs", "overview"],
                # The injected demo pack dir (its dashboards/**/dashboard.json is what
                # get_dashboard_data reads for the parity panel).
                "consumer_pack": {"path": str(_REPO_ROOT / "examples/demo-pack")},
            },
            "mcp": {
                "transport": "stdio",
                "tools": [
                    "describe_signal_catalog",
                    "list_dashboards",
                    "get_dashboard_data",
                    "query_metric",
                    "search_incidents",
                    "search_logs",
                    "describe_health",
                ],
            },
        }
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


class _StdioMcpClient:
    """A synchronous wrapper over the FastMCP stdio `Client` (spec § stdio launch).

    The FastMCP `Client` is async-only; every round-trip here opens the client as an
    async context manager and runs exactly one call via `asyncio.run`, so a test body
    only ever sees plain synchronous methods and the no-async-in-tests rule holds. The
    `asyncio` import is confined entirely to this conftest.
    """

    def __init__(self, transport: StdioTransport) -> None:
        self._transport = transport

    def call_tool_data(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Call one tool over stdio and return its structured content as a plain dict (sync).

        FastMCP serializes a tool's nested-`TypedDict` return into MCP
        `structuredContent` — a plain JSON dict — which is what the tests assert against
        (the `.data` field is a Pydantic model with attribute access, awkward to assert
        on). All the e2e tools used here return a `TypedDict`, so `structured_content`
        is always a dict; a missing one is a contract violation surfaced loudly.
        """

        async def _run() -> dict[str, object]:
            async with Client(self._transport) as client:
                result = await client.call_tool(name, dict(arguments))
                structured = result.structured_content
                if structured is None:
                    raise AssertionError(
                        f"Tool {name!r} returned no structured content over stdio."
                    )
                # Rebuild as an explicitly object-valued dict so the `dict[str, Any]`
                # FastMCP annotation never leaks `Any` into the typed test bodies
                # (No-Any policy — the values are asserted/narrowed at each use site).
                rebuilt: dict[str, object] = {str(key): value for key, value in structured.items()}
                return rebuilt

        return asyncio.run(_run())

    def expect_tool_error(self, name: str, arguments: Mapping[str, object]) -> str:
        """Call a tool expected to FAIL; return the surfaced error text (sync).

        Used by the negative path: a server-side `CapabilityError` propagates over the
        transport as a FastMCP `ToolError`; this returns its message so the test can
        assert on the explicit "no trace source" wording.
        """
        from fastmcp.exceptions import ToolError

        async def _run() -> str:
            async with Client(self._transport) as client:
                try:
                    await client.call_tool(name, dict(arguments))
                except ToolError as exc:
                    return str(exc)
                raise AssertionError(f"Tool {name!r} unexpectedly succeeded; expected an error.")

        return asyncio.run(_run())

    def list_tool_names(self) -> list[str]:
        """List the registered tool names over stdio (sync)."""

        async def _run() -> list[str]:
            async with Client(self._transport) as client:
                tools = await client.list_tools()
                return [tool.name for tool in tools]

        return asyncio.run(_run())


@pytest.fixture
def mcp_stdio_client(
    victoriametrics: VictoriaMetricsHandle, tmp_path: Path
) -> Callable[[Path | None], "_StdioClientContext"]:
    """Factory yielding a synchronous MCP stdio client bound to the live VM.

    The returned callable takes an optional consumer-pack file path (the demo pack for
    the positive path, the trace-probe pack for the negative path; `None` for
    core-only) and returns a context manager that, on enter, has a ready
    `_StdioMcpClient`. Each call spawns a FRESH `python -m core.mcp.server` over stdio
    with `PANOPTES_CONFIG` bound to a synthetic config pointing at the live VM and
    `PANOPTES_CONSUMER_PACK` set to the chosen pack.
    """
    config_path = tmp_path / "panoptes-integration.yaml"
    _write_integration_config(config_path, victoriametrics.base_url)

    def _factory(consumer_pack: Path | None) -> "_StdioClientContext":
        # Inherit the current environment (PATH / venv site-packages) and overlay only
        # the Panoptes wiring — StdioTransport REPLACES env wholesale, so a bare dict
        # would strip the interpreter's own environment.
        env: dict[str, str] = {**os.environ, "PANOPTES_CONFIG": str(config_path)}
        if consumer_pack is not None:
            env["PANOPTES_CONSUMER_PACK"] = str(consumer_pack)
        # PYTHONPATH so the spawned interpreter imports the in-repo `core` package
        # even when launched with a cwd at the repo root.
        env["PYTHONPATH"] = str(_REPO_ROOT)
        # Spawn the SAME interpreter running pytest (the venv's Python 3.12) — a bare
        # "python" can resolve to a system interpreter that rejects the PEP 695 generic
        # syntax `core` uses (`class Registry[T]`). This is the REAL production
        # entrypoint: `core.mcp.server.main()` registers the core adapters itself (via
        # core.bootstrap) before resolving the config, so no test-only launcher shim is
        # needed — the integration test exercises exactly the command compose runs.
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "core.mcp.server"],
            env=env,
            cwd=str(_REPO_ROOT),
        )
        return _StdioClientContext(_StdioMcpClient(transport))

    return _factory


@dataclass
class _StdioClientContext:
    """A trivial context manager wrapping a ready `_StdioMcpClient`.

    The stdio process is opened/closed per round-trip inside `_StdioMcpClient` (each
    `asyncio.run` enters/exits the FastMCP client's own async context), so this
    wrapper holds no long-lived process — it exists only to give tests a uniform
    `with factory(pack) as client:` shape.
    """

    client: _StdioMcpClient

    def __enter__(self) -> _StdioMcpClient:
        return self.client

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def demo_pack_path() -> Path:
    """The brand-neutral demo consumer pack (positive-path injection fixture)."""
    return _REPO_ROOT / "examples/demo-pack/pack.py"


@pytest.fixture
def trace_probe_pack_path() -> Path:
    """The integration-only trace-probe pack (negative-path injection fixture)."""
    return Path(__file__).resolve().parent / "trace_probe_pack.py"


# --- Synthetic-sample helpers (shared by collector-to-VM + parity) ---------------


def make_import_line(
    metric: str, value: float, timestamp: datetime, labels: Mapping[str, str]
) -> str:
    """Build one VictoriaMetrics JSON import line for a synthetic `panoptes_*` sample.

    Mirrors `VictoriaMetricsStore._to_import_line` exactly (so the synthetic write
    path is byte-identical to the production store write), including the `__name__`
    label and millisecond timestamps.
    """
    metric_labels: dict[str, str] = {"__name__": metric, **dict(labels)}
    epoch_millis = int(timestamp.timestamp() * 1000)
    line: dict[str, object] = {
        "metric": metric_labels,
        "values": [value],
        "timestamps": [epoch_millis],
    }
    return json.dumps(line, separators=(",", ":"))


# Re-exported so the parity test pins its direct PromQL caller to the SAME window/step
# `get_dashboard_data` uses (Risk R15 — identical TimeWindow + step on both faces).
DASHBOARD_QUERY_MINUTES = _DASHBOARD_QUERY_MINUTES
DASHBOARD_QUERY_STEP_SECONDS = _DASHBOARD_QUERY_STEP_SECONDS


def now_utc() -> datetime:
    """The current UTC time (single seam so a test's fixed timestamp is consistent)."""
    return datetime.now(UTC)
