"""Integration: the MCP streamable-HTTP server, end-to-end over the real transport.

Brings up the REAL `core.mcp.http.run_http` in a background thread bound to a free
localhost port (against the live VictoriaMetrics store), connects with a SYNCHRONOUS
streamable-HTTP MCP client wrapper (no `asyncio` in any test body — the wrapper confines
it to the conftest), and proves TRANSPORT-LEVEL PARITY: a tool called over HTTP returns the
SAME value a direct store `/query_range` returns. This is the "two faces, one store" proof
at the transport level — the HTTP face is the SAME `build_server(config)` registration as
stdio, only the transport differs.

**Bind note (load-bearing — same as `core/mcp/http.py`).** The server binds a container
port (here `127.0.0.1:<free>` for the test; `0.0.0.0:8080` in the pod). Binding a
non-loopback address inside the pod is acceptable because on Kubernetes the network
boundary is the `ClusterIP` Service + the nginx ingress (the only public path), NOT the
server's listen address. The GitHub auth gate is enforced at the ingress + oauth2-proxy,
not by this bind — the server NEVER validates a token. The Phase-7 Helm render test asserts
the Service is `ClusterIP` + the ingress forward-auth annotations; THAT render is where the
boundary is verified. This e2e simulates oauth2-proxy's header injection (an identity header
the ingress would forward) and asserts the transport carries it, NOT that the server gates
on it.

Synthetic-only (Risk R10): the config the server loads has one `http-health` source pointed
at the live VM's own `/health` and the `victoriametrics` store pointed at the live VM; no
AWS/Sentry. Every server round-trip is driven synchronously via the `mcp_http_server`
fixture's sync client wrapper.
"""

from datetime import timedelta

import httpx
import pytest

from .conftest import (
    DASHBOARD_QUERY_MINUTES,
    DASHBOARD_QUERY_STEP_SECONDS,
    VictoriaMetricsHandle,
    _HttpServerHandle,
    make_import_line,
    now_utc,
)

pytestmark = pytest.mark.integration

# The env the synthetic samples + the query scope use.
_ENV = "dev"
# The parity metric + the exact synthetic value both faces must agree on.
_PARITY_METRIC = "panoptes_health_up"
_PARITY_VALUE = 1.0

# A mocked oauth2-proxy identity header — the kind the nginx ingress + oauth2-proxy would
# forward to the upstream after a successful GitHub auth. The server does NOT gate on it
# (the ingress is the gate); the test asserts the transport carries it for audit.
_PROXY_IDENTITY_HEADER = {"X-Forwarded-User": "octocat", "X-Auth-Request-User": "octocat"}


def test_http_transport_lists_tools_over_the_wire(
    mcp_http_server: _HttpServerHandle,
) -> None:
    """The live HTTP server answers `list_tools` over the real streamable-HTTP transport.

    Proves the HTTP face serves the SAME registered tool table as stdio — the core
    read-only tools are all present over the wire (two faces, one store).
    """
    client = mcp_http_server.client()
    tool_names = client.list_tool_names()
    assert "describe_signal_catalog" in tool_names
    assert "describe_health" in tool_names
    assert "query_metric" in tool_names


def test_http_transport_parity_with_direct_query_range(
    victoriametrics: VictoriaMetricsHandle,
    mcp_http_server: _HttpServerHandle,
) -> None:
    """Transport-level parity: a tool over HTTP returns the SAME value as a direct VM read.

    Writes ONE synthetic `panoptes_health_up{env="dev"}` sample, then queries both faces:
    `query_metric` over the streamable-HTTP transport, and a DIRECT PromQL `/query_range`
    against the VM. The PRIMARY assertion is the scalar last-value being exactly equal
    across both faces — the HTTP transport returns the same store data as a direct read
    (the same builder, only the transport differs).
    """
    # A single synthetic sample at a fixed, recent timestamp inside the trailing window.
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

    # Face A — the MCP HTTP face: `query_metric` over streamable-HTTP for the same metric.
    client = mcp_http_server.client()
    result = client.call_tool_data(
        "query_metric", {"env": _ENV, "metric": _PARITY_METRIC, "window": "15m"}
    )
    mcp_last_value = _last_value_from_query_metric(result)

    # Face B — the direct store read: the SAME metric via a direct `/query_range`.
    direct_last_value = _direct_query_range_last_value(victoriametrics.base_url, expr)

    # PRIMARY assertion: the scalar last-value is exactly equal across both faces.
    assert mcp_last_value == pytest.approx(_PARITY_VALUE)
    assert direct_last_value == pytest.approx(_PARITY_VALUE)
    assert mcp_last_value == pytest.approx(direct_last_value)


def test_http_transport_carries_proxy_identity_header_for_audit(
    mcp_http_server: _HttpServerHandle,
) -> None:
    """A request carrying a mocked oauth2-proxy identity header reaches a tool over HTTP.

    The nginx ingress + oauth2-proxy forward an identity header after a successful GitHub
    auth; the server does NOT gate on it (the ingress is the gate — the server validates no
    token), but the transport must CARRY it (it is logged for audit at the server). This
    asserts a request WITH the mocked proxy header succeeds end-to-end over the real
    transport — the header rides along and the tool still answers.
    """
    # The client attaches the mocked proxy identity header to every request.
    client = mcp_http_server.client(headers=_PROXY_IDENTITY_HEADER)
    # The tool answers normally — the proxy header rides along (server does not reject it).
    health = client.call_tool_data("describe_health", {"env": _ENV})
    assert health["env"] == _ENV


def _last_value_from_query_metric(result: dict[str, object]) -> float:
    """Extract the most-recent sample value from a `query_metric` structured result.

    `query_metric` returns a list of `MetricSeries`; FastMCP serializes it under a wrapping
    key. The series carry `points` as `[epoch_seconds, value]` pairs; this returns the value
    of the most recent point across the first series that has one.
    """
    series_list = _series_list_from_result(result)
    for series in series_list:
        points = series.get("points")
        if isinstance(points, list) and points:
            last_point = points[-1]
            assert isinstance(last_point, list) and len(last_point) == 2, (
                f"expected a [timestamp, value] point, got {last_point!r}"
            )
            return float(_as_number(last_point[1]))
    raise AssertionError(f"query_metric returned no series with points: {result!r}")


def _series_list_from_result(result: dict[str, object]) -> list[dict[str, object]]:
    """Narrow a `query_metric` structured result to its list of series dicts.

    FastMCP wraps a tool's list return in a single-key structured dict (e.g. `{"result":
    [...]}`); this finds the first list-valued field and narrows its dict entries.
    """
    for value in result.values():
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    raise AssertionError(f"query_metric structured result has no series list: {result!r}")


def _as_number(value: object) -> float:
    assert isinstance(value, int | float), f"expected a number, got {value!r}"
    return float(value)


def _direct_query_range_last_value(vm_base_url: str, expr: str) -> float:
    """Run a DIRECT PromQL `/query_range` and return the most-recent sample value.

    Pins the direct caller to the same trailing window + step the MCP tools use, then
    returns the value of the most recent sample in the matrix result (the scalar last-value
    — robust against VM last-value carry-forward).
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
    assert isinstance(payload, dict)
    data = payload.get("data")
    assert isinstance(data, dict)
    result = data.get("result")
    assert isinstance(result, list) and result, f"no series for {expr!r}"
    first = result[0]
    assert isinstance(first, dict)
    values = first.get("values")
    assert isinstance(values, list) and values, f"no samples for {expr!r}"
    last_pair = values[-1]
    assert isinstance(last_pair, list) and len(last_pair) == 2
    value_text = last_pair[1]
    assert isinstance(value_text, str), f"expected a string sample value, got {value_text!r}"
    return float(value_text)
