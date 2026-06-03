"""Unit tests for the `prometheus` core source — read-only PromQL scrape via respx.

The prometheus source GETs `<url>/api/v1/query_range` (window-aware) for each configured
PromQL query and normalizes the Prometheus JSON envelope into `MetricSignal`s with the
`env` stamped. It is read-only (GET only — documented known-miss; the no-write guard is
boto3-shaped and does not see httpx GETs, same as v0.1 sentry/http-health).

All httpx is mocked with `respx` (the source's `RestClient` default client is intercepted
globally); no `asyncio` anywhere (sync `httpx.Client` only). Covers (spec § New core source
adapters / plan Phase 1):
- a `matrix` (query_range) response → exact `MetricSignal`s with the series labels + the
  stamped `env` + correct value/timestamp;
- a `vector` (instant query) response shape also normalizes (the parser handles both);
- `capabilities() == {METRIC}`;
- an HTTP 4xx/5xx surfaces the upstream response BODY in the raised `PanoptesError`;
- an unreachable endpoint → `health()` `reachable=False` (no raise), generic detail;
- only GET is used (read-only — the request method is asserted).
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import MetricSignal, SignalKind, TimeWindow
from core.sources.prometheus import PrometheusSource

_BASE = "http://prometheus.test:9090"
_ENV = "dev"
_QUERY = "up"
_QUERY_RANGE_URL = f"{_BASE}/api/v1/query_range"

# A fixed window so the request params are deterministic.
_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
)

# A recorded `query_range` (matrix) response: one series with two samples. The
# Prometheus envelope: status/success + data.resultType="matrix" + result[].values.
_MATRIX_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"__name__": "up", "job": "api", "instance": "10.0.0.1:9090"},
                "values": [
                    [1735689600, "1"],  # 2025-01-01 00:00:00 UTC
                    [1735689660, "0"],  # 2025-01-01 00:01:00 UTC
                ],
            }
        ],
    },
}

# A recorded `vector` (instant) response: one series with a single `value` pair. The parser
# must handle this shape too (resultType="vector", result[].value).
_VECTOR_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [
            {
                "metric": {"__name__": "up", "job": "api"},
                "value": [1735689600, "1"],
            }
        ],
    },
}


def _source(
    client: httpx.Client | None = None, queries: list[str] | None = None
) -> PrometheusSource:
    return PrometheusSource(
        {
            "url": _BASE,
            "queries": queries if queries is not None else [_QUERY],
            "env": _ENV,
        },
        client=client,
    )


def test_capabilities_is_exactly_metric() -> None:
    """The prometheus source advertises exactly {METRIC} — no LOG/INCIDENT/TRACE."""
    assert _source().capabilities() == {SignalKind.METRIC}


def test_requires_url_queries_env() -> None:
    """Missing `url`, `queries`, or `env` fails fast at construction."""
    with pytest.raises(PanoptesError):
        PrometheusSource({"queries": [_QUERY], "env": _ENV})  # no url
    with pytest.raises(PanoptesError):
        PrometheusSource({"url": _BASE, "env": _ENV})  # no queries
    with pytest.raises(PanoptesError):
        PrometheusSource({"url": _BASE, "queries": [_QUERY]})  # no env


@respx.mock
def test_matrix_response_normalizes_to_exact_metric_signals() -> None:
    """A `matrix` (query_range) response → one MetricSignal per sample, labels + env stamped."""
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=_MATRIX_PAYLOAD))

    signals = _source().fetch(_WINDOW)
    metrics = [s for s in signals if isinstance(s, MetricSignal)]
    assert len(metrics) == 2  # two samples in the one series

    first = metrics[0]
    assert first.name == "up"  # the series __name__
    assert first.value == 1.0
    # The fixture epoch 1735689600 is 2025-01-01 00:00:00 UTC.
    assert first.timestamp == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    # The series labels (minus __name__) + the stamped env; __name__ is the metric name.
    assert first.labels == {
        "env": _ENV,
        "job": "api",
        "instance": "10.0.0.1:9090",
    }

    second = metrics[1]
    assert second.value == 0.0
    assert second.timestamp == datetime(2025, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert second.labels["env"] == _ENV


@respx.mock
def test_vector_response_shape_also_normalizes() -> None:
    """A `vector` (instant) response also normalizes (the parser handles both shapes)."""
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=_VECTOR_PAYLOAD))

    metrics = [s for s in _source().fetch(_WINDOW) if isinstance(s, MetricSignal)]
    assert len(metrics) == 1
    assert metrics[0].name == "up"
    assert metrics[0].value == 1.0
    assert metrics[0].labels == {"env": _ENV, "job": "api"}


@respx.mock
def test_env_is_authoritative_over_a_clashing_series_label() -> None:
    """A series carrying its OWN `env` label does NOT overwrite the stamped (authoritative) env."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"__name__": "up", "env": "WRONG"}, "value": [1735689600, "1"]}],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))

    metric = next(s for s in _source().fetch(_WINDOW) if isinstance(s, MetricSignal))
    # The stamped env wins — the source's env is authoritative (model invariant).
    assert metric.labels["env"] == _ENV


@respx.mock
def test_name_falls_back_to_the_query_when_no_metric_name() -> None:
    """A series with no `__name__` falls back to the query expr as the metric name."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"job": "api"}, "value": [1735689600, "0.5"]}],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))

    metric = next(
        s
        for s in _source(queries=["rate(http_requests_total[5m])"]).fetch(_WINDOW)
        if isinstance(s, MetricSignal)
    )
    assert metric.name == "rate(http_requests_total[5m])"
    assert metric.value == 0.5


@respx.mock
def test_http_error_surfaces_the_response_body() -> None:
    """A 4xx/5xx surfaces the upstream response BODY in the raised PanoptesError."""
    respx.get(_QUERY_RANGE_URL).mock(
        return_value=httpx.Response(400, text="parse error: unexpected identifier")
    )
    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)
    assert "parse error: unexpected identifier" in str(excinfo.value)


@respx.mock
def test_only_get_is_used_read_only() -> None:
    """The source issues only GET requests (read-only — no mutating HTTP verb)."""
    methods: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=_VECTOR_PAYLOAD)

    # Match ANY method on the endpoint so a stray POST/PUT would be recorded (and assert
    # only GET was seen).
    respx.route(host="prometheus.test").mock(side_effect=_record)
    _source().fetch(_WINDOW)
    assert methods, "the source must issue at least one request"
    assert set(methods) == {"GET"}, f"prometheus source must be read-only (GET only); saw {methods}"


@respx.mock
def test_health_unreachable_when_endpoint_down_does_not_raise() -> None:
    """An unreachable endpoint → `health()` reports reachable=False, raising nothing."""
    respx.get(f"{_BASE}/-/healthy").mock(side_effect=httpx.ConnectError("connection refused"))
    health = _source().health()
    assert health.reachable is False
    # Generic detail — names the source, not a verbatim leak.
    assert "prometheus" in health.detail.lower()


@respx.mock
def test_health_reachable_when_endpoint_responds() -> None:
    """A responsive `/-/healthy` → `health()` reports reachable=True."""
    respx.get(f"{_BASE}/-/healthy").mock(
        return_value=httpx.Response(200, text="Prometheus is Healthy.")
    )
    assert _source().health().reachable is True


@respx.mock
def test_multiple_queries_are_each_scraped() -> None:
    """Each configured query is scraped (one request per query)."""
    routes = respx.get(_QUERY_RANGE_URL).mock(
        return_value=httpx.Response(200, json=_VECTOR_PAYLOAD)
    )
    _source(queries=["up", "rate(http_requests_total[5m])"]).fetch(_WINDOW)
    assert routes.call_count == 2, "each configured query must be scraped once"


@respx.mock
def test_non_success_status_in_envelope_yields_no_signals() -> None:
    """A Prometheus `status: error` envelope (200 HTTP) yields no signals (no crash)."""
    payload = {"status": "error", "errorType": "bad_data", "error": "invalid expression"}
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    # No samples to emit; the source returns nothing for that query rather than crashing.
    assert _source().fetch(_WINDOW) == []


# --- malformed-payload defensive paths (the normalizer skips bad data, never raises) --------


@respx.mock
def test_non_dict_payload_yields_no_signals() -> None:
    """A non-dict top-level payload (a JSON list) is skipped, not crashed."""
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    assert _source().fetch(_WINDOW) == []


@respx.mock
def test_non_dict_data_yields_no_signals() -> None:
    """A `success` envelope whose `data` is not a dict yields no signals."""
    payload = {"status": "success", "data": "not-a-dict"}
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    assert _source().fetch(_WINDOW) == []


@respx.mock
def test_non_list_result_yields_no_signals() -> None:
    """A `data.result` that is not a list yields no signals."""
    payload = {"status": "success", "data": {"resultType": "matrix", "result": "not-a-list"}}
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    assert _source().fetch(_WINDOW) == []


@respx.mock
def test_bad_value_pair_and_unparseable_sample_are_skipped() -> None:
    """A matrix series with a bad ts AND a bad value sample SKIPS the bad ones, keeps the good.

    A non-numeric timestamp and a non-numeric value each fail `_parse_sample` and are skipped;
    the one well-formed `[unix, "value"]` sample survives — `fetch()` never raises.
    """
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "up", "job": "api"},
                    "values": [
                        ["not-a-number", "1"],  # non-numeric ts → skipped
                        [1735689660, "not-a-float"],  # non-numeric value → skipped
                        [1735689720, "0.5"],  # the one good sample
                    ],
                }
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    metrics = [s for s in _source().fetch(_WINDOW) if isinstance(s, MetricSignal)]
    assert [m.value for m in metrics] == [0.5]
