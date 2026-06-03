"""Integration: the TWO unrelated consumer packs inject + answer over the real MCP transport.

The unit-level genericity proof (`tests/unit/test_genericity_two_consumers.py`) asserts the
core registry baseline is byte-identical across the two injections. THIS suite proves the
same two unrelated packs work END TO END over the live stdio MCP transport: each pack, when
injected via the v0.1 `PANOPTES_CONSUMER_PACK` hook into a freshly spawned
`python -m core.mcp.server`, registers its own tool AND that tool answers over the wire —
with NO core change distinguishing the two deploys, only the injected pack dir.

Two SEPARATE test functions (one per consumer) plus a cross-check that the two tools are
disjoint — keyed to assertion count (the anti-rot floor counts integration tests), so this
suite ADDS to the floor rather than consolidating into one node.

Every server round-trip is driven synchronously via the `mcp_stdio_client` factory's sync
wrapper (spec § stdio launch contract) — no `asyncio` in any test body. Synthetic-only (Risk
R10): the spawned server's config has one `http-health` source at the live VM + the
`victoriametrics` store; the consumer tools read the store directly (empty here → zeroed
shapes), so the assertion is "the injected tool answers its typed shape over the wire".
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import _StdioClientContext

pytestmark = pytest.mark.integration

# The env the consumer tools scope their store reads to (the config's one enabled env).
_ENV = "dev"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FLEET_PACK_PATH = _REPO_ROOT / "examples" / "consumer-fleet-pack" / "pack.py"
_PIPELINE_PACK_PATH = _REPO_ROOT / "examples" / "consumer-pipeline-pack" / "pack.py"


def test_fleet_consumer_tool_answers_over_stdio(
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
) -> None:
    """Injecting the fleet pack, `get_fleet_health` registers AND answers over the real transport.

    Spawns the live stdio server with the fleet pack injected, lists the tools (the injected
    `get_fleet_health` is present alongside the core tools), then calls it — proving the
    consumer #1 tool works end to end. The store is empty, so the fleet counts are zeroed; the
    assertion is the typed `FleetHealth` shape answering over the wire.
    """
    with mcp_stdio_client(_FLEET_PACK_PATH) as client:
        tool_names = client.list_tool_names()
        assert "describe_health" in tool_names, "core tools must be present"
        assert "get_fleet_health" in tool_names, "the injected fleet pack tool must register"

        health = client.call_tool_data("get_fleet_health", {"env": _ENV})
        assert health["env"] == _ENV
        # The FleetHealth shape answers (empty store → zeroed replica counts).
        for field_name in ("ready", "allocated", "reserved"):
            assert isinstance(health[field_name], int | float), (
                f"FleetHealth.{field_name} must be a numeric replica count over the wire"
            )


def test_pipeline_consumer_tool_answers_over_stdio(
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
) -> None:
    """Injecting the pipeline pack, `get_pipeline_lag` registers AND answers over the transport.

    The SECOND, unrelated consumer: spawns the live stdio server with the pipeline pack
    injected and calls `get_pipeline_lag` — proving the consumer #2 tool works end to end with
    NO core change relative to the fleet deploy (only the injected pack dir differs). The store
    is empty, so the lag/queue/freshness values are zeroed; the assertion is the typed
    `PipelineLag` shape answering over the wire.
    """
    with mcp_stdio_client(_PIPELINE_PACK_PATH) as client:
        tool_names = client.list_tool_names()
        assert "describe_health" in tool_names, "core tools must be present"
        assert "get_pipeline_lag" in tool_names, "the injected pipeline pack tool must register"
        # The OTHER consumer's tool is NOT present (only the injected pack's tool registers).
        assert "get_fleet_health" not in tool_names, (
            "only the injected pipeline pack's tool registers — not the fleet pack's"
        )

        lag = client.call_tool_data("get_pipeline_lag", {"env": _ENV})
        assert lag["env"] == _ENV
        # The PipelineLag shape answers (empty store → zeroed lag/queue/freshness).
        for field_name in ("lag_seconds", "queue_depth", "freshness_seconds"):
            assert isinstance(lag[field_name], int | float), (
                f"PipelineLag.{field_name} must be a numeric value over the wire"
            )


def test_each_consumer_injection_adds_only_its_own_tool_over_stdio(
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
) -> None:
    """Over the real transport, each unrelated injection adds ONLY its own tool (no cross-leak).

    The end-to-end half of the genericity proof's additivity clause: the fleet injection's tool
    set and the pipeline injection's tool set, over the live stdio transport, share the SAME core
    tools and differ by EXACTLY their own consumer tool — neither leaks the other's. Proves the
    two unrelated consumers inject identically against one unchanged core.
    """
    with mcp_stdio_client(_FLEET_PACK_PATH) as client:
        fleet_tools = set(client.list_tool_names())
    with mcp_stdio_client(_PIPELINE_PACK_PATH) as client:
        pipeline_tools = set(client.list_tool_names())

    # The core tool set both injections share (everything except each pack's own tool).
    shared_core = fleet_tools - {"get_fleet_health"}
    assert shared_core == pipeline_tools - {"get_pipeline_lag"}, (
        "both unrelated injections must share the SAME core tool set over the wire"
    )
    # Each injection added EXACTLY its own consumer tool — disjoint, no cross-leak.
    assert "get_fleet_health" in fleet_tools and "get_fleet_health" not in pipeline_tools
    assert "get_pipeline_lag" in pipeline_tools and "get_pipeline_lag" not in fleet_tools
