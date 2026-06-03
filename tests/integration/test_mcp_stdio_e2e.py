"""Integration: the MCP stdio server, end-to-end over the real transport.

Three SEPARATE test functions (the anti-rot floor is keyed to assertion count, not
file count — spec Phase 8 exit criterion):

1. `test_discovery_and_health` — the live stdio server answers `describe_signal_catalog`
   + `describe_health` + the injected demo pack's `get_demo_signal` against the live
   VM store (spec `## Tests` → Integration, bullet 3 + the injection proof).
2. `test_parity` — Grafana↔MCP parity: one synthetic sample at a fixed timestamp, then
   a direct PromQL `/query_range` and `get_dashboard_data(...)` over stdio return the
   SAME value for one panel. Both callers are pinned to the SAME `/query_range`
   endpoint family with the identical `TimeWindow` + `step` (Risk R15); the PRIMARY
   assertion is the scalar last-value being exactly equal across both faces.
3. `test_no_trace_source` — asking the live server for traces over stdio surfaces the
   explicit "no trace source" `CapabilityError` over the real transport (negative path).

Every server round-trip is driven synchronously via the `mcp_stdio_client` factory's
sync wrapper (spec § stdio MCP launch contract) — no `asyncio` in any test body.

Synthetic-only (Risk R10): the config the spawned server loads has one `http-health`
source pointed at the live VM's own `/health` and the `victoriametrics` store pointed
at the live VM; no AWS/Sentry.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from .conftest import (
    DASHBOARD_QUERY_MINUTES,
    DASHBOARD_QUERY_STEP_SECONDS,
    VictoriaMetricsHandle,
    _StdioClientContext,
    make_import_line,
    now_utc,
)

pytestmark = pytest.mark.integration

# The env the synthetic samples + the dashboard `$env` substitution use.
_ENV = "dev"
# The demo dashboard's panel-1 metric (`panoptes_health_up{env=~"$env"}`).
_PARITY_METRIC = "panoptes_health_up"
# The exact synthetic value both faces must agree on.
_PARITY_VALUE = 1.0


def test_discovery_and_health(
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
    demo_pack_path: Path,
) -> None:
    """The live stdio server answers discovery + health + the injected demo tool.

    Spawns `python -m core.mcp.server` over stdio with the demo pack injected, then
    over the real transport: lists the catalog, rolls up `dev` health (the live
    http-health source pointed at the VM is reachable), and calls the injected
    `get_demo_signal` — proving both the core tools AND the consumer-pack tool work
    end to end.
    """
    with mcp_stdio_client(demo_pack_path) as client:
        # The injected pack tool is present alongside the core tools.
        tool_names = client.list_tool_names()
        assert "describe_signal_catalog" in tool_names
        assert "describe_health" in tool_names
        assert "get_demo_signal" in tool_names, "the injected demo pack tool must register"

        catalog = client.call_tool_data("describe_signal_catalog", {})
        assert _ENV in _as_list(catalog["environments"])
        # The http-health source is configured for the enabled env.
        source_types = {_as_dict(source)["type"] for source in _as_list(catalog["sources"])}
        assert "http-health" in source_types

        health = client.call_tool_data("describe_health", {"env": _ENV})
        assert health["env"] == _ENV
        # The live VM `/health` the http-health source probes is reachable.
        reachable = {
            _as_dict(source)["type"]: _as_dict(source)["reachable"]
            for source in _as_list(health["sources"])
        }
        assert reachable.get("http-health") is True

        demo = client.call_tool_data("get_demo_signal", {"env": _ENV, "window": "15m"})
        assert demo["env"] == _ENV
        assert demo["window"] == "15m"
        # The demo signal's shape is present (metrics list + a sample-count rollup).
        assert isinstance(demo["metrics"], list)
        assert isinstance(demo["sample_count"], int)


def test_parity(
    victoriametrics: VictoriaMetricsHandle,
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
    demo_pack_path: Path,
) -> None:
    """Grafana↔MCP parity: a direct PromQL read and `get_dashboard_data` agree.

    Writes ONE synthetic `panoptes_health_up{env="dev"}` sample, then queries both
    faces pinned to the SAME `/query_range` endpoint family with the identical
    `TimeWindow` + `step` `get_dashboard_data` uses (Risk R15). The PRIMARY assertion
    is the scalar LAST-VALUE — robust against VictoriaMetrics staleness/last-value
    carry-forward, which can repeat the one sample across interior grid points and
    would break a full rounded-tuple-SET comparison.
    """
    # A single synthetic sample at a fixed, recent timestamp (inside the trailing
    # DASHBOARD_QUERY_MINUTES window both faces use). Labels mirror what the panel's
    # `panoptes_health_up{env=~"$env"}` (with $env -> dev) matches.
    sample_time = now_utc() - timedelta(seconds=DASHBOARD_QUERY_STEP_SECONDS)
    line = make_import_line(
        _PARITY_METRIC,
        _PARITY_VALUE,
        sample_time,
        {"env": _ENV, "url": f"{victoriametrics.base_url}/health"},
    )
    victoriametrics.import_samples([line])
    expr = f'{_PARITY_METRIC}{{env="{_ENV}"}}'
    victoriametrics.wait_for_series(expr)

    # Face A — the MCP face: get_dashboard_data over stdio for the `overview` CORE pack
    # (its json_path points straight at a dashboard.json get_dashboard_data can read;
    # the demo pack's json_path is the mounted DIR the Grafana provider globs, not a
    # single file). The overview pack's first panptes_health_up panel target is
    # `min by (env) (panoptes_health_up{env=~"$env"})`; get_dashboard_data builds
    # TimeWindow.last(60) + step 60 internally and executes it with $env -> dev.
    with mcp_stdio_client(demo_pack_path) as client:
        dashboard = client.call_tool_data(
            "get_dashboard_data", {"dashboard_id": "overview", "env": _ENV}
        )
    mcp_expr, mcp_last_value = _mcp_panel_target(dashboard, _PARITY_METRIC)

    # Face B — the Grafana face: the EXACT SAME panel PromQL (the aggregated expr the
    # MCP face executed) via a DIRECT /query_range, pinned to the identical window +
    # step get_dashboard_data uses (R15 — same endpoint family, never an instant
    # /query). Using the MCP-resolved expr verbatim guarantees both faces ran the
    # identical PromQL against the one store.
    grafana_last_value = _direct_query_range_last_value(victoriametrics.base_url, mcp_expr)

    # PRIMARY assertion (R15): the scalar last-value at the sample's own grid point is
    # exactly equal across both faces — two faces, one store.
    assert mcp_last_value == pytest.approx(_PARITY_VALUE)
    assert grafana_last_value == pytest.approx(_PARITY_VALUE)
    assert mcp_last_value == pytest.approx(grafana_last_value)


def test_no_trace_source(
    mcp_stdio_client: Callable[[Path | None], _StdioClientContext],
    trace_probe_pack_path: Path,
) -> None:
    """Asking the live server for traces over stdio surfaces 'no trace source'.

    The trace-probe consumer pack registers a `search_traces` tool (the same documented
    injection hook a consumer uses) that delegates to the real core
    `core.mcp.tools_query.search_traces`. Over the real stdio transport that
    capability-negotiation raises an explicit `CapabilityError` which propagates as a
    FastMCP `ToolError`; the test asserts the explicit "no trace source" wording
    surfaced over the wire (not just in a unit fake).
    """
    with mcp_stdio_client(trace_probe_pack_path) as client:
        error_text = client.expect_tool_error("search_traces", {"env": _ENV, "window": "15m"})
    lowered = error_text.lower()
    assert "trace" in lowered
    assert "no trace source" in lowered or "no configured source provides trace" in lowered


def _as_list(value: object) -> list[object]:
    """Narrow an `object` (a structured-content field) to a list, or fail loudly."""
    assert isinstance(value, list), f"expected a list, got {type(value).__name__}: {value!r}"
    return value


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an `object` (a structured-content field) to a dict, or fail loudly."""
    assert isinstance(value, dict), f"expected a dict, got {type(value).__name__}: {value!r}"
    return value


def _mcp_panel_target(dashboard: dict[str, object], metric: str) -> tuple[str, float]:
    """Return the `(executed_expr, last_value)` of the first panel target naming `metric`.

    `get_dashboard_data` returns `panels[].targets[].{expr, series[].points}` with
    points as `[epoch_seconds, value]` float lists. This finds the first target whose
    executed PromQL `expr` references `metric` and returns BOTH that exact expr (so
    Face B can run the identical PromQL — the literal "two faces, one store" proof) and
    the value of its most recent point.
    """
    for raw_panel in _as_list(dashboard["panels"]):
        panel = _as_dict(raw_panel)
        for raw_target in _as_list(panel["targets"]):
            target = _as_dict(raw_target)
            expr = target["expr"]
            if not isinstance(expr, str) or metric not in expr:
                continue
            for raw_series in _as_list(target["series"]):
                series = _as_dict(raw_series)
                points = _as_list(series["points"])
                if points:
                    last_point = _as_list(points[-1])
                    return expr, float(_as_float(last_point[1]))
    raise AssertionError(f"no panel target with metric {metric!r} returned a value")


def _as_float(value: object) -> float:
    """Narrow an `object` numeric to a float, or fail loudly."""
    assert isinstance(value, int | float), f"expected a number, got {value!r}"
    return float(value)


def _direct_query_range_last_value(vm_base_url: str, expr: str) -> float:
    """Run a DIRECT PromQL `/query_range` (R15: same endpoint family) and return last-value.

    Pins the direct caller to the identical trailing-`DASHBOARD_QUERY_MINUTES` window +
    `DASHBOARD_QUERY_STEP_SECONDS` step `get_dashboard_data` uses, then returns the
    value of the most recent sample in the matrix result (the scalar last-value — the
    R15 primary parity comparison, robust against last-value carry-forward).
    """
    end = now_utc()
    start = end - timedelta(minutes=DASHBOARD_QUERY_MINUTES)
    params = {
        "query": expr,
        "start": str(int(start.timestamp())),
        "end": str(int(end.timestamp())),
        "step": str(DASHBOARD_QUERY_STEP_SECONDS),
    }
    with httpx.Client(timeout=10.0) as http_client:
        response = http_client.get(f"{vm_base_url}/api/v1/query_range", params=params)
        response.raise_for_status()
        payload: object = response.json()
    result = _as_list(_as_dict(_as_dict(payload)["data"])["result"])
    assert result, f"direct /query_range returned no series for {expr!r}"
    values = _as_list(_as_dict(result[0])["values"])
    assert values, f"direct /query_range returned no samples for {expr!r}"
    # The last [epoch_seconds, "value"] pair — VM serializes the value as a STRING.
    last_pair = _as_list(values[-1])
    value_text = last_pair[1]
    assert isinstance(value_text, str), f"expected string sample value, got {value_text!r}"
    return float(value_text)
