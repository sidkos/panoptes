"""Unit tests for the `loki` core log source — read-only LogQL scrape via respx.

The loki source GETs `<url>/loki/api/v1/query_range` (window-aware) for each configured
LogQL query and normalizes the Loki `streams` envelope into `LogSignal`s with the `env`
stamped. It is read-only (GET only — documented known-miss; the no-write guard is
boto3-shaped and does not see httpx GETs, same as v0.1 sentry/http-health + the prometheus
source).

All httpx is mocked with `respx` (the source's `RestClient` default client is intercepted
globally); no `asyncio` anywhere (sync `httpx.Client` only). Covers (spec § New core source
adapters / plan Phase 2):
- a `streams` response → exact `LogSignal`s with the right message/timestamp/level/env+labels;
- a multi-stream response;
- the nanosecond-ts parse (Loki serializes timestamps as nanosecond-precision strings);
- the level parse (structured `level`/`detected_level` stream label first, then the line text);
- an HTTP 4xx/5xx surfaces the upstream response BODY in the raised `PanoptesError`;
- an unreachable endpoint → `health()` `reachable=False` (no raise), generic detail;
- only GET is used (read-only — the request method is asserted);
- `capabilities() == {LOG}`; missing-config fails fast.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import LogLevel, LogSignal, SignalKind, TimeWindow
from core.sources.loki import LokiSource

_BASE = "http://loki.test:3100"
_ENV = "dev"
_QUERY = '{job="api"}'
_QUERY_RANGE_URL = f"{_BASE}/loki/api/v1/query_range"

# A fixed window so the request params are deterministic.
_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
)

# Nanosecond-precision Loki timestamps (strings). 1735689600000000000 ns = 2025-01-01 00:00:00 UTC.
_TS_0 = "1735689600000000000"
_TS_1 = "1735689660000000000"  # +60s

# A recorded `streams` response: one stream with two log lines. The stream carries a
# structured `level` label (Loki's detected/structured level).
_STREAMS_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {"job": "api", "level": "error"},
                "values": [
                    [_TS_0, "database connection refused"],
                    [_TS_1, "retrying connection"],
                ],
            }
        ],
    },
}


def _source(client: httpx.Client | None = None, queries: list[str] | None = None) -> LokiSource:
    return LokiSource(
        {
            "url": _BASE,
            "queries": queries if queries is not None else [_QUERY],
            "env": _ENV,
        },
        client=client,
    )


def test_capabilities_is_exactly_log() -> None:
    """The loki source advertises exactly {LOG} — no METRIC/INCIDENT/TRACE."""
    assert _source().capabilities() == {SignalKind.LOG}


def test_requires_url_queries_env() -> None:
    """Missing `url`, `queries`, or `env` fails fast at construction."""
    with pytest.raises(PanoptesError):
        LokiSource({"queries": [_QUERY], "env": _ENV})  # no url
    with pytest.raises(PanoptesError):
        LokiSource({"url": _BASE, "env": _ENV})  # no queries
    with pytest.raises(PanoptesError):
        LokiSource({"url": _BASE, "queries": [_QUERY]})  # no env


@respx.mock
def test_streams_response_normalizes_to_exact_log_signals() -> None:
    """A `streams` response → one LogSignal per line, message/timestamp/level/env+labels."""
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=_STREAMS_PAYLOAD))

    signals = _source().fetch(_WINDOW)
    logs = [s for s in signals if isinstance(s, LogSignal)]
    assert len(logs) == 2  # two lines in the one stream

    first = logs[0]
    assert first.message == "database connection refused"
    # 1735689600000000000 ns = 2025-01-01 00:00:00 UTC.
    assert first.timestamp == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    # The structured `level` stream label drives the level.
    assert first.level == LogLevel.ERROR
    # The stream labels (the `level` label dropped — it became the LEVEL) + the stamped env.
    assert first.labels == {"env": _ENV, "job": "api"}

    second = logs[1]
    assert second.message == "retrying connection"
    assert second.timestamp == datetime(2025, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert second.level == LogLevel.ERROR
    assert second.labels["env"] == _ENV


@respx.mock
def test_multiple_streams_each_yield_their_lines() -> None:
    """A multi-stream response yields each stream's lines, each with its own labels/level."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "api", "level": "warning"},
                    "values": [[_TS_0, "slow query"]],
                },
                {
                    "stream": {"job": "worker", "level": "info"},
                    "values": [[_TS_1, "job completed"]],
                },
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))

    logs = [s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal)]
    by_message = {log.message: log for log in logs}
    assert set(by_message) == {"slow query", "job completed"}
    assert by_message["slow query"].level == LogLevel.WARNING
    assert by_message["slow query"].labels == {"env": _ENV, "job": "api"}
    assert by_message["job completed"].level == LogLevel.INFO
    assert by_message["job completed"].labels == {"env": _ENV, "job": "worker"}


@respx.mock
def test_detected_level_label_is_honored() -> None:
    """A stream's `detected_level` label (Loki's auto-detected level) drives the level."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": {"job": "api", "detected_level": "critical"}, "values": [[_TS_0, "OOM"]]}
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    log = next(s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal))
    assert log.level == LogLevel.CRITICAL
    # The `detected_level` label is consumed as the LEVEL, not carried as a label.
    assert log.labels == {"env": _ENV, "job": "api"}


@respx.mock
def test_level_falls_back_to_line_text_then_info() -> None:
    """With no structured level label, the level is scanned from the line, defaulting INFO."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "api"},
                    "values": [
                        [_TS_0, "ERROR: handler blew up"],  # scanned from the line → ERROR
                        [_TS_1, "request served in 12ms"],  # no marker → INFO
                    ],
                }
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    logs = [s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal)]
    by_message = {log.message: log.level for log in logs}
    assert by_message["ERROR: handler blew up"] == LogLevel.ERROR
    assert by_message["request served in 12ms"] == LogLevel.INFO


@respx.mock
def test_env_is_authoritative_over_a_clashing_stream_label() -> None:
    """A stream carrying its OWN `env` label does NOT overwrite the stamped (authoritative) env."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"job": "api", "env": "WRONG"}, "values": [[_TS_0, "hello"]]}],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    log = next(s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal))
    assert log.labels["env"] == _ENV  # the stamped env wins (authoritative)


@respx.mock
def test_http_error_surfaces_the_response_body() -> None:
    """A 4xx/5xx surfaces the upstream response BODY in the raised PanoptesError.

    The body text deliberately avoids the literal word "token" — `core.rest`'s credential
    redaction treats a `token <word>` sequence as a possible reflected secret and `[REDACTED]`s
    it, which would mask the assertion (a correct defense, not a source bug). A realistic Loki
    LogQL parse error carries no such word.
    """
    respx.get(_QUERY_RANGE_URL).mock(
        return_value=httpx.Response(400, text="parse error: unexpected identifier in LogQL")
    )
    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)
    assert "parse error: unexpected identifier in LogQL" in str(excinfo.value)


@respx.mock
def test_only_get_is_used_read_only() -> None:
    """The source issues only GET requests (read-only — no mutating HTTP verb)."""
    methods: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=_STREAMS_PAYLOAD)

    respx.route(host="loki.test").mock(side_effect=_record)
    _source().fetch(_WINDOW)
    assert methods, "the source must issue at least one request"
    assert set(methods) == {"GET"}, f"loki source must be read-only (GET only); saw {methods}"


@respx.mock
def test_health_unreachable_when_endpoint_down_does_not_raise() -> None:
    """An unreachable endpoint → `health()` reports reachable=False, raising nothing."""
    respx.get(f"{_BASE}/ready").mock(side_effect=httpx.ConnectError("connection refused"))
    health = _source().health()
    assert health.reachable is False
    assert "loki" in health.detail.lower()


@respx.mock
def test_health_failure_detail_does_not_leak_str_exc() -> None:
    """A probe exception whose `str()` embeds a sensitive token does NOT reach health.detail."""
    secret = "super-secret-probe-token-xyz"
    respx.get(f"{_BASE}/ready").mock(side_effect=httpx.ConnectError(f"refused: {secret}"))
    health = _source().health()
    assert health.reachable is False
    assert secret not in health.detail, "str(exc) must not leak into the loki health detail"
    assert "ConnectError" in health.detail


@respx.mock
def test_health_reachable_when_endpoint_responds() -> None:
    """A responsive `/ready` (plain text) → `health()` reports reachable=True."""
    respx.get(f"{_BASE}/ready").mock(return_value=httpx.Response(200, text="ready"))
    assert _source().health().reachable is True


@respx.mock
def test_health_success_detail_strips_url_userinfo() -> None:
    """NIT-3: an embedded `user:pass@` credential is stripped from the reachable health detail."""
    url = "http://probe-user:supersecret@loki.test:3100"
    respx.get(f"{url}/ready").mock(return_value=httpx.Response(200, text="ready"))
    source = LokiSource({"url": url, "queries": [_QUERY], "env": _ENV})
    health = source.health()
    assert health.reachable is True
    assert "supersecret" not in health.detail
    assert "probe-user" not in health.detail
    assert "loki.test" in health.detail


@respx.mock
def test_multiple_queries_are_each_scraped() -> None:
    """Each configured query is scraped (one request per query)."""
    routes = respx.get(_QUERY_RANGE_URL).mock(
        return_value=httpx.Response(200, json=_STREAMS_PAYLOAD)
    )
    _source(queries=['{job="api"}', '{job="worker"}']).fetch(_WINDOW)
    assert routes.call_count == 2, "each configured query must be scraped once"


@respx.mock
def test_non_success_status_in_envelope_yields_no_signals() -> None:
    """A Loki `status: error` envelope (HTTP 200) yields no signals (no crash)."""
    payload = {"status": "error", "errorType": "bad_data", "error": "invalid query"}
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
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
    payload = {"status": "success", "data": {"resultType": "streams", "result": "not-a-list"}}
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    assert _source().fetch(_WINDOW) == []


@respx.mock
def test_bad_value_pair_and_non_str_message_lines_are_skipped() -> None:
    """A short value pair AND a non-str message line are SKIPPED; a valid line survives."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "api", "level": "info"},
                    "values": [
                        [_TS_0],  # too-short pair (no message) → skipped
                        [_TS_0, 12345],  # non-str message → skipped
                        [_TS_1, "valid line"],  # the one good line
                    ],
                }
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    logs = [s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal)]
    assert [log.message for log in logs] == ["valid line"]


@respx.mock
def test_unparseable_ns_timestamp_line_is_skipped() -> None:
    """A line whose ns timestamp is unparseable is SKIPPED (no crash), a valid line survives."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "api", "level": "info"},
                    "values": [
                        ["not-a-number", "bad ts line"],  # unparseable ns ts → skipped
                        [_TS_1, "good ts line"],
                    ],
                }
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    logs = [s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal)]
    assert [log.message for log in logs] == ["good ts line"]


@respx.mock
def test_out_of_range_ns_timestamp_line_is_skipped() -> None:
    """A valid-INTEGER but out-of-range ns timestamp is SKIPPED (OverflowError, not raised).

    `int(raw)` succeeds but `datetime.fromtimestamp` raises OverflowError/OSError for a year far
    outside datetime's range — that must be caught (loki is on a DIRECT un-backstopped MCP path),
    skipping only the bad line while a sibling good line in the same stream survives.
    """
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"job": "api", "level": "info"},
                    "values": [
                        # A valid integer string, but billions of years in the future (out of
                        # range for datetime) → OverflowError inside the parse guard → skipped.
                        ["999999999999999999999999999999", "out-of-range ts line"],
                        [_TS_1, "good ts line"],
                    ],
                }
            ],
        },
    }
    respx.get(_QUERY_RANGE_URL).mock(return_value=httpx.Response(200, json=payload))
    logs = [s for s in _source().fetch(_WINDOW) if isinstance(s, LogSignal)]
    assert [log.message for log in logs] == ["good ts line"]
