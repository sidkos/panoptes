"""Phase 2 unit tests for the `victoriametrics` store.

Covers (spec `## Tests` → Stores):
- `write` serializes only `MetricSignal`s to the VM JSON-line import format
  (`/api/v1/import`) with the exact line shape — name, value, millisecond
  timestamp, and every label (incl. `env`) carried into the `metric` object;
- `query` GETs `/api/v1/query_range` and parses the Prometheus matrix response
  into the exact `list[MetricSeries]`;
- `env` label fidelity round-trips (written with `env=dev`, read back retains it);
- an empty 200 matrix response yields an **empty list**, not an error;
- both `_format_failure` branches raise and surface a useful message: a
  5xx-with-body (response present, body text appears) and a connection error
  (no `.response` to read — must not crash on the missing response).

All httpx is mocked with `respx` (Risk R3). Timestamps are fixed and UTC so the
asserted bytes are deterministic.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import (
    CanonicalSignal,
    IncidentLevel,
    IncidentSignal,
    MetricQuery,
    MetricSignal,
    TimeWindow,
)
from core.stores.victoriametrics import VictoriaMetricsStore

_BASE_URL = "http://victoriametrics:8428"
# 2026-01-01T00:00:00Z == 1767225600 epoch seconds == 1767225600000 epoch millis.
_FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_FIXED_EPOCH_SECONDS = 1767225600
_FIXED_EPOCH_MILLIS = 1767225600000


def _store() -> VictoriaMetricsStore:
    return VictoriaMetricsStore({"url": _BASE_URL})


def _metric_signal(name: str, value: float, env: str = "dev") -> MetricSignal:
    return MetricSignal(
        name=name,
        value=value,
        timestamp=_FIXED_TIMESTAMP,
        labels={"env": env, "url": "http://app/health"},
    )


def _query(expr: str = "panoptes_health_up") -> MetricQuery:
    return MetricQuery(
        expr=expr,
        window=TimeWindow(start=_FIXED_TIMESTAMP, end=_FIXED_TIMESTAMP),
        step_seconds=60,
    )


@respx.mock
def test_write_emits_exact_vm_import_line() -> None:
    route = respx.post(f"{_BASE_URL}/api/v1/import").mock(return_value=httpx.Response(204))
    store = _store()

    store.write([_metric_signal("panoptes_health_up", 1.0)])

    assert route.called
    posted_body = route.calls.last.request.content.decode("utf-8")
    lines = [line for line in posted_body.split("\n") if line]
    assert len(lines) == 1
    emitted = json.loads(lines[0])
    assert emitted == {
        "metric": {
            "__name__": "panoptes_health_up",
            "env": "dev",
            "url": "http://app/health",
        },
        "values": [1.0],
        "timestamps": [_FIXED_EPOCH_MILLIS],
    }


@respx.mock
def test_write_serializes_only_metric_signals() -> None:
    """Logs/incidents/traces are not persisted in v0.1 — only derived gauge metrics."""
    route = respx.post(f"{_BASE_URL}/api/v1/import").mock(return_value=httpx.Response(204))
    store = _store()
    incident = IncidentSignal(
        id="ISSUE-1",
        title="boom",
        level=IncidentLevel.ERROR,
        first_seen=_FIXED_TIMESTAMP,
        last_seen=_FIXED_TIMESTAMP,
        count=3,
        labels={"env": "dev"},
    )
    batch: list[CanonicalSignal] = [_metric_signal("panoptes_health_up", 1.0), incident]

    store.write(batch)

    posted_body = route.calls.last.request.content.decode("utf-8")
    lines = [line for line in posted_body.split("\n") if line]
    # Only the MetricSignal is serialized; the IncidentSignal is dropped.
    assert len(lines) == 1
    assert json.loads(lines[0])["metric"]["__name__"] == "panoptes_health_up"


@respx.mock
def test_query_parses_matrix_into_metric_series() -> None:
    matrix_response = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "panoptes_health_up", "env": "dev", "url": "http://a"},
                    "values": [[_FIXED_EPOCH_SECONDS, "1"], [_FIXED_EPOCH_SECONDS + 60, "0"]],
                }
            ],
        },
    }
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=matrix_response)
    )
    store = _store()

    series = store.query(_query())

    assert len(series) == 1
    only = series[0]
    assert only.metric == "panoptes_health_up"
    assert only.labels == {"env": "dev", "url": "http://a"}
    assert only.points == [
        (datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC), 1.0),
        (datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC), 0.0),
    ]


@respx.mock
def test_query_sends_expected_range_params() -> None:
    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": []}},
        )

    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(side_effect=_record)
    store = _store()

    store.query(_query(expr="panoptes_log_error_rate"))

    assert captured["query"] == "panoptes_log_error_rate"
    assert captured["start"] == str(_FIXED_EPOCH_SECONDS)
    assert captured["end"] == str(_FIXED_EPOCH_SECONDS)
    assert captured["step"] == "60"


@respx.mock
def test_env_label_fidelity_round_trip() -> None:
    """A signal written with `env=dev` reads back retaining `env`."""
    captured_lines: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        captured_lines.extend(line for line in body.split("\n") if line)
        return httpx.Response(204)

    respx.post(f"{_BASE_URL}/api/v1/import").mock(side_effect=_capture)
    store = _store()
    store.write([_metric_signal("panoptes_health_up", 1.0, env="dev")])

    # The written `metric` object carries env=dev faithfully.
    written_metric = json.loads(captured_lines[0])["metric"]
    assert written_metric["env"] == "dev"

    # And a read-back of the same series surfaces env=dev in the parsed labels.
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "panoptes_health_up", "env": "dev"},
                            "values": [[_FIXED_EPOCH_SECONDS, "1"]],
                        }
                    ],
                },
            },
        )
    )
    series = store.query(_query())
    assert series[0].labels["env"] == "dev"


@respx.mock
def test_query_empty_matrix_returns_empty_list_not_error() -> None:
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": []}},
        )
    )
    store = _store()

    series = store.query(_query())

    assert series == []


# --- F2l: malformed-but-200 responses each raise a clear PanoptesError -----------


@respx.mock
def test_query_data_not_an_object_raises() -> None:
    """A 200 with `data` not an object raises a clear PanoptesError (F2l)."""
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": "not-an-object"})
    )
    with pytest.raises(PanoptesError, match="data"):
        _store().query(_query())


@respx.mock
def test_query_result_not_a_list_raises() -> None:
    """A 200 whose `data.result` is not a list raises a clear PanoptesError (F2l)."""
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": "nope"}},
        )
    )
    with pytest.raises(PanoptesError, match="result"):
        _store().query(_query())


@respx.mock
def test_query_non_numeric_sample_value_raises() -> None:
    """A sample value that is not a numeric string raises a clear PanoptesError (F2l)."""
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "panoptes_health_up", "env": "dev"},
                            # A non-string sample value (an int, not the VM `"1"` string form).
                            "values": [[_FIXED_EPOCH_SECONDS, 1]],
                        }
                    ],
                },
            },
        )
    )
    with pytest.raises(PanoptesError, match="value"):
        _store().query(_query())


@respx.mock
def test_query_malformed_value_pair_raises() -> None:
    """A malformed `values` pair (wrong arity) raises a clear PanoptesError (F2l)."""
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "panoptes_health_up", "env": "dev"},
                            # A single-element pair (should be [ts, value]).
                            "values": [[_FIXED_EPOCH_SECONDS]],
                        }
                    ],
                },
            },
        )
    )
    with pytest.raises(PanoptesError, match="pair"):
        _store().query(_query())


@respx.mock
def test_query_status_error_raises() -> None:
    """A 200 with `status: error` raises a clear PanoptesError (F2l).

    VictoriaMetrics can return HTTP 200 with a Prometheus-style `{"status":"error",...}`
    envelope. `raise_for_status` does NOT catch it (it is a 200), so the store must
    inspect the envelope and surface the error rather than silently returning empty.
    """
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "error",
                "errorType": "422",
                "error": "promql parse error: unexpected token",
            },
        )
    )
    with pytest.raises(PanoptesError, match="error"):
        _store().query(_query())


@respx.mock
def test_query_wrong_result_type_raises() -> None:
    """A 200 whose `resultType` is not `matrix` raises a clear PanoptesError (F2l).

    The store issues a RANGE query, whose result is always a `matrix`. A `vector`/`scalar`
    resultType means the response does not match the expected range shape and must fail
    loudly rather than be mis-parsed.
    """
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "vector", "result": []}},
        )
    )
    with pytest.raises(PanoptesError, match=r"resultType|matrix"):
        _store().query(_query())


@respx.mock
def test_write_only_non_metric_signals_makes_zero_http_calls() -> None:
    """A write of only non-MetricSignals issues ZERO HTTP calls (F2l empty-body skip)."""
    route = respx.post(f"{_BASE_URL}/api/v1/import").mock(return_value=httpx.Response(204))
    store = _store()
    incident = IncidentSignal(
        id="ISSUE-1",
        title="boom",
        level=IncidentLevel.ERROR,
        first_seen=_FIXED_TIMESTAMP,
        last_seen=_FIXED_TIMESTAMP,
        count=1,
        labels={"env": "dev"},
    )

    store.write([incident])

    # No MetricSignal in the batch → empty import body → no round-trip to the server.
    assert not route.called


@respx.mock
def test_query_degenerate_start_equals_end_window_returns_one_point() -> None:
    """A degenerate `start == end` window issues one clean range request → one point (F2l)."""
    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "panoptes_health_up", "env": "dev"},
                            "values": [[_FIXED_EPOCH_SECONDS, "1"]],
                        }
                    ],
                },
            },
        )

    route = respx.get(f"{_BASE_URL}/api/v1/query_range").mock(side_effect=_record)
    store = _store()

    # `_query()` uses a TimeWindow with start == end (both _FIXED_TIMESTAMP).
    series = store.query(_query())

    # Exactly one clean request; start == end in the params; one returned point.
    assert route.call_count == 1
    assert captured["start"] == captured["end"] == str(_FIXED_EPOCH_SECONDS)
    assert len(series) == 1
    assert series[0].points == [(datetime(2026, 1, 1, tzinfo=UTC), 1.0)]


@respx.mock
def test_write_5xx_with_body_raises_and_surfaces_body() -> None:
    rejection_body = "field 'env' rejected: cannot be empty"
    respx.post(f"{_BASE_URL}/api/v1/import").mock(
        return_value=httpx.Response(500, text=rejection_body)
    )
    store = _store()

    with pytest.raises(PanoptesError) as excinfo:
        store.write([_metric_signal("panoptes_health_up", 1.0)])
    # _format_failure branch (a): the response body must appear in the message.
    assert rejection_body in str(excinfo.value)


@respx.mock
def test_query_5xx_with_body_raises_and_surfaces_body() -> None:
    rejection_body = "promql parse error: unexpected token"
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(
        return_value=httpx.Response(500, text=rejection_body)
    )
    store = _store()

    with pytest.raises(PanoptesError) as excinfo:
        store.query(_query())
    assert rejection_body in str(excinfo.value)


@respx.mock
def test_write_connection_error_raises_without_response() -> None:
    respx.post(f"{_BASE_URL}/api/v1/import").mock(side_effect=httpx.ConnectError("boom"))
    store = _store()

    with pytest.raises(PanoptesError) as excinfo:
        store.write([_metric_signal("panoptes_health_up", 1.0)])
    # _format_failure branch (b): no `.response` to read — must not crash, must
    # still raise a clear message that names the failure.
    message = str(excinfo.value)
    assert "boom" in message
    assert "/api/v1/import" in message


@respx.mock
def test_query_connection_error_raises_without_response() -> None:
    respx.get(f"{_BASE_URL}/api/v1/query_range").mock(side_effect=httpx.ConnectError("boom"))
    store = _store()

    with pytest.raises(PanoptesError) as excinfo:
        store.query(_query())
    message = str(excinfo.value)
    assert "boom" in message
    assert "/api/v1/query_range" in message
