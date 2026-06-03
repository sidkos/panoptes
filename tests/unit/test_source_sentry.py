"""Phase 3 unit tests for the `sentry` source.

Covers (spec `## Tests` → Sources, sentry bullets):
- a recorded issues payload → **exact** `IncidentSignal`s (id/title/level/seen/count)
  + a derived `panoptes_sentry_incident_count` gauge with the **exact** label set
  `{env, level, project}`;
- `Retry-After` honored — a 429 then 200 reads the header and respects it via the
  injectable `sleep` seam (asserted: the seam was called with the header value, no
  wall-clock sleep);
- failure body surfaced — a non-429 4xx/5xx raises a typed `PanoptesError` carrying
  the upstream response body, NOT an empty incident list;
- `capabilities() == {INCIDENT, METRIC}`;
- `env` stamped on every emitted signal.

All httpx is mocked with `respx` (Risk R3). Timestamps in the fixture are fixed/UTC.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import (
    IncidentLevel,
    IncidentSignal,
    MetricSignal,
    SignalKind,
    TimeWindow,
)
from core.sources.sentry import SentrySource

_ORG = "acme"
_PROJECT = "backend"
_ENV = "dev"
_TOKEN = "sntrytok"
_BASE = "https://sentry.example"
_ISSUES_URL = f"{_BASE}/api/0/projects/{_ORG}/{_PROJECT}/issues/"
_WINDOW = TimeWindow.last(minutes=15)

# A recorded two-issue payload with fixed ISO-8601 timestamps.
_ISSUES_PAYLOAD = [
    {
        "id": "ISSUE-1",
        "title": "NullPointer in handler",
        "level": "error",
        "firstSeen": "2026-01-01T00:00:00Z",
        "lastSeen": "2026-01-01T01:00:00Z",
        "count": "42",
    },
    {
        "id": "ISSUE-2",
        "title": "Slow query warning",
        "level": "warning",
        "firstSeen": "2026-01-02T00:00:00Z",
        "lastSeen": "2026-01-02T00:30:00Z",
        "count": 7,
    },
]


def _source(
    client: httpx.Client | None = None,
    sleep_calls: list[float] | None = None,
) -> SentrySource:
    sleep = (lambda seconds: sleep_calls.append(seconds)) if sleep_calls is not None else None
    return SentrySource(
        {
            "org": _ORG,
            "project": _PROJECT,
            "token": _TOKEN,
            "env": _ENV,
            "base_url": _BASE,
        },
        client=client,
        sleep=sleep,
    )


def test_capabilities_is_incident_and_metric() -> None:
    assert _source().capabilities() == {SignalKind.INCIDENT, SignalKind.METRIC}


def test_requires_org_project_token_env() -> None:
    with pytest.raises(PanoptesError):
        SentrySource({"project": _PROJECT, "token": _TOKEN, "env": _ENV})
    with pytest.raises(PanoptesError):
        SentrySource({"org": _ORG, "token": _TOKEN, "env": _ENV})
    with pytest.raises(PanoptesError):
        SentrySource({"org": _ORG, "project": _PROJECT, "env": _ENV})
    with pytest.raises(PanoptesError):
        SentrySource({"org": _ORG, "project": _PROJECT, "token": _TOKEN})


@respx.mock
def test_issues_normalize_to_exact_incident_signals() -> None:
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=_ISSUES_PAYLOAD))

    signals = _source().fetch(_WINDOW)

    incidents = [s for s in signals if isinstance(s, IncidentSignal)]
    assert len(incidents) == 2

    first = incidents[0]
    assert first.id == "ISSUE-1"
    assert first.title == "NullPointer in handler"
    assert first.level == IncidentLevel.ERROR
    assert first.first_seen == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert first.last_seen == datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    assert first.count == 42
    # env stamped + level/project labels.
    assert first.labels == {"env": _ENV, "level": "error", "project": _PROJECT}

    second = incidents[1]
    assert second.id == "ISSUE-2"
    assert second.level == IncidentLevel.WARNING
    assert second.count == 7
    assert second.labels == {"env": _ENV, "level": "warning", "project": _PROJECT}


@respx.mock
def test_derived_count_metric_is_per_level_with_exact_labels() -> None:
    """The derived count gauge is emitted PER LEVEL (F6) — one series per distinct level.

    The two-issue fixture has one `error` + one `warning`, so two gauges are emitted,
    each with the EXACT `{env, level, project}` label set carrying the ACTUAL level value
    (never the misleading `level="all"` aggregate, which broke the dashboard's
    `sum by (level)` panel).
    """
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=_ISSUES_PAYLOAD))

    signals = _source().fetch(_WINDOW)

    metrics = [s for s in signals if isinstance(s, MetricSignal)]
    # One gauge per distinct level (error + warning), never a single `level="all"`.
    assert {m.name for m in metrics} == {"panoptes_sentry_incident_count"}
    by_level = {m.labels["level"]: m for m in metrics}
    assert set(by_level) == {"error", "warning"}
    assert by_level["error"].value == 1.0
    assert by_level["error"].labels == {"env": _ENV, "level": "error", "project": _PROJECT}
    assert by_level["warning"].value == 1.0
    assert by_level["warning"].labels == {"env": _ENV, "level": "warning", "project": _PROJECT}
    # No misleading aggregate level label is emitted.
    assert "all" not in by_level


@respx.mock
def test_query_filters_by_environment_and_bears_token() -> None:
    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["environment"] = request.url.params.get("environment", "")
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[])

    respx.get(_ISSUES_URL).mock(side_effect=_record)

    _source().fetch(_WINDOW)

    assert captured["environment"] == _ENV
    assert captured["auth"] == f"Bearer {_TOKEN}"


@respx.mock
def test_retry_after_honored_then_succeeds() -> None:
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=_ISSUES_PAYLOAD),
    ]

    signals = _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    # The Retry-After header value was read and respected via the injectable seam.
    assert sleep_calls == [3.0]
    incidents = [s for s in signals if isinstance(s, IncidentSignal)]
    assert len(incidents) == 2


@respx.mock
def test_retry_after_still_429_raises() -> None:
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}, text="rate limited"),
        httpx.Response(429, headers={"Retry-After": "1"}, text="rate limited again"),
    ]

    with pytest.raises(PanoptesError) as excinfo:
        _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    assert sleep_calls == [1.0]
    assert "rate limited again" in str(excinfo.value)


@respx.mock
def test_401_bad_token_raises_with_body_not_empty_list() -> None:
    body = "{'detail': 'Invalid token'}"
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(401, text=body))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    # Failure body surfaced (spec: never a silent empty incident list).
    assert body in str(excinfo.value)
    assert "401" in str(excinfo.value)


@respx.mock
def test_404_wrong_project_raises_with_body() -> None:
    body = "project not found"
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(404, text=body))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    assert body in str(excinfo.value)
    assert "404" in str(excinfo.value)


@respx.mock
def test_connection_error_raises_without_response() -> None:
    respx.get(_ISSUES_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    # No response to read — must not crash on the missing .response; message names it.
    message = str(excinfo.value)
    assert "boom" in message
    assert _ISSUES_URL in message


@respx.mock
def test_retry_after_unparseable_falls_back() -> None:
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "not-a-number"}),
        httpx.Response(200, json=[]),
    ]

    _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    # Unparseable header falls back to a 1-second courtesy delay.
    assert sleep_calls == [1.0]


@respx.mock
def test_retry_after_zero_sleeps_zero() -> None:
    """`Retry-After: 0` sleeps 0.0 — distinct from an unparseable header (→ 1.0) (F2m)."""
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=[]),
    ]

    _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    assert sleep_calls == [0.0]


@respx.mock
def test_retry_after_negative_clamps_to_zero() -> None:
    """A negative `Retry-After` clamps to 0.0 (F2m) — never a negative sleep."""
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "-5"}),
        httpx.Response(200, json=[]),
    ]

    _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    assert sleep_calls == [0.0]


@respx.mock
def test_retry_after_huge_is_capped_at_max() -> None:
    """An absurdly large `Retry-After` is capped at the 60s ceiling (F2m)."""
    from core.sources.sentry import _MAX_RETRY_AFTER_SECONDS

    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "100000"}),
        httpx.Response(200, json=[]),
    ]

    _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    assert sleep_calls == [_MAX_RETRY_AFTER_SECONDS]
    assert _MAX_RETRY_AFTER_SECONDS == 60.0


@respx.mock
@pytest.mark.parametrize("retry_after", ["nan", "inf", "-inf"])
def test_retry_after_non_finite_falls_back_to_courtesy_delay(retry_after: str) -> None:
    """A non-finite `Retry-After` (`nan`/`inf`) falls back to the 1.0 courtesy delay (F2i).

    `float("nan")` parses without raising and the old `max`/`min` clamp neutralized it
    only by ordering accident; an explicit `math.isfinite` guard now falls back to the
    courtesy delay so `time.sleep` is never called with nan/inf.
    """
    sleep_calls: list[float] = []
    route = respx.get(_ISSUES_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": retry_after}),
        httpx.Response(200, json=[]),
    ]

    _source(sleep_calls=sleep_calls).fetch(_WINDOW)

    # A finite, safe value was passed to sleep (the courtesy fallback), never nan/inf.
    assert sleep_calls == [1.0]


@respx.mock
def test_unknown_level_defaults_to_error() -> None:
    payload = [
        {
            "id": "ISSUE-X",
            "title": "weird",
            "level": "verbose",
            "firstSeen": "2026-01-01T00:00:00Z",
            "lastSeen": "2026-01-01T00:00:00Z",
            "count": "1",
        }
    ]
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=payload))

    incidents = [s for s in _source().fetch(_WINDOW) if isinstance(s, IncidentSignal)]

    assert incidents[0].level == IncidentLevel.ERROR


@respx.mock
def test_default_base_url_used_when_not_configured() -> None:
    # No base_url in config → the Sentry SaaS default is used.
    default_url = f"https://sentry.io/api/0/projects/{_ORG}/{_PROJECT}/issues/"
    respx.get(default_url).mock(return_value=httpx.Response(200, json=[]))
    source = SentrySource(
        {"org": _ORG, "project": _PROJECT, "token": _TOKEN, "env": _ENV},
    )

    signals = source.fetch(_WINDOW)

    # Zero issues → no per-level gauge is emitted (F6: gauges are per distinct level, and
    # with no issues there is no level to count). The successful empty fetch + no incident
    # signals proves the default endpoint was hit.
    metrics = [s for s in signals if isinstance(s, MetricSignal)]
    assert metrics == []
    incidents = [s for s in signals if isinstance(s, IncidentSignal)]
    assert incidents == []


@respx.mock
def test_non_array_payload_raises() -> None:
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json={"not": "a list"}))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    assert "not a JSON array" in str(excinfo.value)


@respx.mock
def test_non_object_issue_entry_raises() -> None:
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=["just-a-string"]))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    assert "not an object" in str(excinfo.value)


@respx.mock
def test_unparseable_count_and_timestamp_fall_back() -> None:
    payload = [
        {
            "id": "ISSUE-9",
            "title": "weird counts",
            "level": "error",
            "firstSeen": "not-a-timestamp",
            "lastSeen": "2026-01-01T00:00:00Z",
            "count": "not-a-number",
        }
    ]
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=payload))

    incidents = [s for s in _source().fetch(_WINDOW) if isinstance(s, IncidentSignal)]

    # Unparseable count → 0; unparseable firstSeen → epoch-UTC fallback.
    assert incidents[0].count == 0
    assert incidents[0].first_seen == datetime(1970, 1, 1, tzinfo=UTC)


@respx.mock
def test_boolean_count_treated_as_zero() -> None:
    payload = [
        {
            "id": "ISSUE-B",
            "title": "bool count",
            "level": "info",
            "firstSeen": "2026-01-01T00:00:00Z",
            "lastSeen": "2026-01-01T00:00:00Z",
            "count": True,
        }
    ]
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=payload))

    incidents = [s for s in _source().fetch(_WINDOW) if isinstance(s, IncidentSignal)]

    # bool is an int subclass; the explicit guard avoids treating True as 1.
    assert incidents[0].count == 0


@respx.mock
def test_missing_id_field_raises() -> None:
    payload = [{"title": "no id", "level": "error"}]
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    assert "string 'id'" in str(excinfo.value)


@respx.mock
def test_health_reachable_lists_issue_count() -> None:
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(200, json=_ISSUES_PAYLOAD))

    health = _source().health()

    assert health.reachable is True
    assert "2 issue" in health.detail


@respx.mock
def test_health_unreachable_on_error() -> None:
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(500, text="boom"))

    health = _source().health()

    assert health.reachable is False
    # The detail no longer carries a verbatim upstream body (which could include a
    # reflected token); it is a generic transport/auth-failure summary (F4).
    assert "unreachable" in health.detail.lower()


@respx.mock
def test_health_detail_does_not_leak_reflected_bearer_token() -> None:
    """A reflected `Authorization: Bearer <token>` in the upstream body must NOT reach
    the surfaced `health().detail` (F4). `health()` maps a transport/auth failure to a
    generic 'unreachable' summary rather than a verbatim body that could include the
    bearer token."""
    leaky_body = f"echoing request: Authorization: Bearer {_TOKEN} was rejected"
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(401, text=leaky_body))

    health = _source().health()

    assert health.reachable is False
    assert _TOKEN not in health.detail, "the bearer token must not leak into health detail"


@respx.mock
def test_fetch_failure_message_redacts_reflected_bearer_token() -> None:
    """A reflected bearer token in a non-429 failure body is REDACTED in the surfaced
    PanoptesError message (F4 — the shared `_format_failure` redaction)."""
    leaky_body = f"Authorization: Bearer {_TOKEN} — invalid"
    respx.get(_ISSUES_URL).mock(return_value=httpx.Response(401, text=leaky_body))

    with pytest.raises(PanoptesError) as excinfo:
        _source().fetch(_WINDOW)

    message = str(excinfo.value)
    assert _TOKEN not in message, "the raw bearer token must not leak into the error message"
    assert "[REDACTED]" in message


@respx.mock
def test_fetch_failure_message_strips_base_url_userinfo() -> None:
    """MAJOR-2: a `user:pass@host` base_url does NOT leak its credential in a fetch failure.

    The fetch identifier is the issues URL (built from `base_url`); a userinfo-bearing base_url
    would otherwise surface its credential in the error. `_format_failure` strips `user:pass@`.
    """
    userinfo_base = "https://probe-user:urlsecret@sentry.example"
    issues_url = f"{userinfo_base}/api/0/projects/{_ORG}/{_PROJECT}/issues/"
    respx.get(issues_url).mock(side_effect=httpx.ConnectError("refused"))
    source = SentrySource(
        {"org": _ORG, "project": _PROJECT, "token": _TOKEN, "env": _ENV, "base_url": userinfo_base}
    )
    with pytest.raises(PanoptesError) as excinfo:
        source.fetch(_WINDOW)
    message = str(excinfo.value)
    assert "urlsecret" not in message, "the URL credential must not leak in the failure message"
    assert "probe-user" not in message
    assert "sentry.example" in message  # the host is still a non-secret diagnostic
