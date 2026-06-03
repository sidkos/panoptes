"""Phase 3 unit tests for the `cloudwatch` source.

Covers (spec `## Tests` → Sources, cloudwatch bullets):
- recorded `GetMetricData` / `FilterLogEvents` payloads → **exact** normalized
  `MetricSignal`s / `LogSignal`s, every signal `env`-stamped;
- a derived `panoptes_log_error_rate` gauge per log group with the **exact** label
  set `{env, log_group}`;
- **pagination** — multi-page `NextToken` (metrics) and `nextToken` (logs) are
  followed to exhaustion;
- **assume-role precedence over profile** — `assume_role_arn` set ⇒ STS `assume_role`
  is called (with `ExternalId`) rather than the profile being used directly;
- **AssumeRole denial via the injectable sts seam** — `Stubber.add_client_error(
  "assume_role", "AccessDenied")` on the injected sts client surfaces a clear auth
  error through `health()` and does NOT crash the cycle;
- `capabilities() == {METRIC, LOG}`.

boto3 is exercised entirely through `botocore.stub.Stubber` against injected clients
(NO live AWS calls). Timestamps are fixed/UTC for deterministic assertions.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.stub import Stubber
from core.errors import PanoptesError
from core.model import LogLevel, LogSignal, MetricSignal, SignalKind, TimeWindow
from core.registry import ConfigValue
from core.sources.cloudwatch import CloudWatchSource, _paginate

if TYPE_CHECKING:
    # Type-stub-only imports (boto3-stubs is a dev dep): present at type-check time,
    # never at slim-CI runtime — guarded so a bare runtime import can't crash.
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_sts import STSClient

_REGION = "us-east-1"
_NAMESPACE = "AWS/ApplicationELB"
_ENV = "dev"
_METRIC_NAMES = ["RequestCount", "TargetResponseTime"]
_LOG_GROUPS = ["/app/api", "/app/worker"]
_ROLE_ARN = "arn:aws:iam::123456789012:role/PanoptesReadRole"
_EXTERNAL_ID = "panoptes-ext-id"

_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
)
_SAMPLE_TS = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)


def _config(**overrides: ConfigValue) -> dict[str, ConfigValue]:
    base: dict[str, ConfigValue] = {
        "region": _REGION,
        "namespace": _NAMESPACE,
        "metric_names": _METRIC_NAMES,
        "log_groups": _LOG_GROUPS,
        "env": _ENV,
    }
    base.update(overrides)
    return base


def _cloudwatch_client() -> "CloudWatchClient":
    return boto3.client("cloudwatch", region_name=_REGION)


def _logs_client() -> "CloudWatchLogsClient":
    return boto3.client("logs", region_name=_REGION)


def _sts_client() -> "STSClient":
    return boto3.client("sts", region_name=_REGION)


def test_capabilities_is_metric_and_log() -> None:
    source = CloudWatchSource(_config())
    assert source.capabilities() == {SignalKind.METRIC, SignalKind.LOG}


def test_requires_core_config_fields() -> None:
    with pytest.raises(PanoptesError):
        CloudWatchSource({"namespace": _NAMESPACE, "env": _ENV})


def test_metrics_normalize_with_pagination() -> None:
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    # Page 1 of GetMetricData → one sample, with a NextToken to force a second page.
    cw_stub.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "q0",
                    "Label": "RequestCount",
                    "Timestamps": [_SAMPLE_TS],
                    "Values": [100.0],
                    "StatusCode": "Complete",
                }
            ],
            "NextToken": "page-2",
        },
    )
    # Page 2 → another sample, no NextToken (pagination terminates).
    cw_stub.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "q0",
                    "Label": "RequestCount",
                    "Timestamps": [_SAMPLE_TS],
                    "Values": [200.0],
                    "StatusCode": "Complete",
                }
            ],
        },
    )
    # No log events for either group (logs feed empty here).
    for _ in _LOG_GROUPS:
        logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    signals = source.fetch(_WINDOW)

    metrics = [s for s in signals if isinstance(s, MetricSignal) and s.name == "RequestCount"]
    assert [m.value for m in metrics] == [100.0, 200.0]
    for metric in metrics:
        assert metric.labels == {"env": _ENV, "metric": "RequestCount"}
        assert metric.timestamp == _SAMPLE_TS
    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_logs_normalize_with_pagination_and_error_rate() -> None:
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    # Metrics empty so the test focuses on the logs path.
    cw_stub.add_response("get_metric_data", {"MetricDataResults": []})

    event_ts_millis = int(_SAMPLE_TS.timestamp() * 1000)
    # First log group: two pages. Page 1 = one ERROR event + nextToken; page 2 = one
    # INFO event, no token. Error rate over the group = 1/2 = 0.5.
    logs_stub.add_response(
        "filter_log_events",
        {
            "events": [{"message": "ERROR boom in handler", "timestamp": event_ts_millis}],
            "nextToken": "logs-page-2",
        },
    )
    logs_stub.add_response(
        "filter_log_events",
        {"events": [{"message": "request served ok", "timestamp": event_ts_millis}]},
    )
    # Second log group: single page, no events → error rate 0.0.
    logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    signals = source.fetch(_WINDOW)

    log_signals = [s for s in signals if isinstance(s, LogSignal)]
    assert len(log_signals) == 2
    levels = sorted(s.level.value for s in log_signals)
    assert levels == [LogLevel.ERROR.value, LogLevel.INFO.value]
    for log_signal in log_signals:
        assert log_signal.labels["env"] == _ENV
        assert log_signal.labels["log_group"] == "/app/api"
        assert log_signal.timestamp == _SAMPLE_TS

    error_rates = {
        s.labels["log_group"]: s.value
        for s in signals
        if isinstance(s, MetricSignal) and s.name == "panoptes_log_error_rate"
    }
    assert error_rates == {"/app/api": 0.5, "/app/worker": 0.0}
    # Exact derived-metric label set (spec): {env, log_group}.
    rate_signals = [
        s for s in signals if isinstance(s, MetricSignal) and s.name == "panoptes_log_error_rate"
    ]
    for rate in rate_signals:
        assert set(rate.labels) == {"env", "log_group"}
        assert rate.labels["env"] == _ENV

    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_assume_role_precedence_over_profile_calls_sts_with_external_id() -> None:
    sts = _sts_client()
    sts_stub = Stubber(sts)
    sts_stub.add_response(
        "assume_role",
        {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE123456",
                "SecretAccessKey": "stubbed-secret-access-key",
                "SessionToken": "stubbed-session-token",
                "Expiration": _SAMPLE_TS,
            },
            "AssumedRoleUser": {"AssumedRoleId": "ARID:sess", "Arn": _ROLE_ARN},
        },
        expected_params={
            "RoleArn": _ROLE_ARN,
            "RoleSessionName": "panoptes-cloudwatch",
            "ExternalId": _EXTERNAL_ID,
        },
    )
    sts_stub.activate()

    source = CloudWatchSource(
        _config(profile="some-profile", assume_role_arn=_ROLE_ARN, external_id=_EXTERNAL_ID),
        sts_client=sts,
    )
    health = source.health()

    # assume_role was called with ExternalId (precedence over profile); health OK.
    assert health.reachable is True
    sts_stub.assert_no_pending_responses()


def test_assume_role_denial_surfaces_through_health_without_crashing() -> None:
    sts = _sts_client()
    sts_stub = Stubber(sts)
    sts_stub.add_client_error("assume_role", "AccessDenied", "not authorized")
    sts_stub.activate()

    source = CloudWatchSource(
        _config(assume_role_arn=_ROLE_ARN, external_id=_EXTERNAL_ID),
        sts_client=sts,
    )
    # Must NOT raise — the denial is surfaced as an unreachable health result so the
    # collector's per-source try/continue boundary keeps the cycle running.
    health = source.health()

    assert health.reachable is False
    assert "AccessDenied" in health.detail or "credential" in health.detail


def test_health_reachable_when_no_assume_role_configured() -> None:
    # Default-chain auth: no assume-role attempt, health is reachable without touching
    # STS (no sts client injected, none built).
    source = CloudWatchSource(_config())
    health = source.health()
    assert health.reachable is True


def test_lazy_client_builders_construct_offline() -> None:
    # boto3 client construction is offline (no network until a call is made), so the
    # lazy builders are exercisable without AWS. Profile-less = default-chain session.
    source = CloudWatchSource(_config())

    cw = source._cloudwatch()
    logs = source._logs()
    sts = source._sts()

    # Built once and cached — a second call returns the same instance.
    assert source._cloudwatch() is cw
    assert source._logs() is logs
    assert source._sts() is sts


def test_lazy_session_honors_profile_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A configured profile routes through a profile-named session. boto3 validates the
    # profile at session construction, so a real (temp) AWS config file defines it — no
    # network, no real credentials, just the profile-branch exercised genuinely.
    config_file = tmp_path / "aws_config"
    config_file.write_text("[profile panoptes-readonly]\nregion = us-east-1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

    source = CloudWatchSource(_config(profile="panoptes-readonly"))
    session = source._session()

    assert session.profile_name == "panoptes-readonly"


def test_malformed_metric_results_are_skipped() -> None:
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    # A result with mismatched/absent arrays must be skipped, not crash the fetch.
    cw_stub.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                # Missing Timestamps/Values → skipped.
                {"Id": "q0", "Label": "RequestCount", "StatusCode": "Complete"},
                # Valid single sample → kept.
                {
                    "Id": "q1",
                    "Label": "TargetResponseTime",
                    "Timestamps": [_SAMPLE_TS],
                    "Values": [0.5],
                    "StatusCode": "Complete",
                },
            ]
        },
    )
    for _ in _LOG_GROUPS:
        logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    metrics = [s for s in source.fetch(_WINDOW) if isinstance(s, MetricSignal)]

    kept = [m for m in metrics if m.name == "TargetResponseTime"]
    assert len(kept) == 1
    assert kept[0].value == 0.5
    # The malformed RequestCount result produced no metric signal.
    assert not [m for m in metrics if m.name == "RequestCount"]
    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_malformed_log_event_is_skipped() -> None:
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    cw_stub.add_response("get_metric_data", {"MetricDataResults": []})
    event_ts_millis = int(_SAMPLE_TS.timestamp() * 1000)
    # First group: one well-formed event + one missing its timestamp (skipped).
    logs_stub.add_response(
        "filter_log_events",
        {
            "events": [
                {"message": "ok line", "timestamp": event_ts_millis},
                {"message": "no timestamp here"},
            ]
        },
    )
    logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    log_signals = [s for s in source.fetch(_WINDOW) if isinstance(s, LogSignal)]

    # Only the well-formed event becomes a LogSignal; the malformed one is dropped.
    assert len(log_signals) == 1
    assert log_signals[0].message == "ok line"
    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_classify_level_maps_markers_to_true_levels() -> None:
    """`_classify_level` maps each marker to its TRUE `LogLevel` (F2h), not a lossy ERROR.

    The old classifier collapsed CRITICAL/FATAL to ERROR and never emitted WARNING/DEBUG.
    Each severity marker must now map to its real level so a downstream consumer sees the
    actual severity.
    """
    classify = CloudWatchSource._classify_level
    assert classify("CRITICAL kernel panic") == LogLevel.CRITICAL
    assert classify("a FATAL crash occurred") == LogLevel.CRITICAL
    assert classify("ERROR boom in handler") == LogLevel.ERROR
    assert classify("EXCEPTION raised") == LogLevel.ERROR
    assert classify("WARNING disk almost full") == LogLevel.WARNING
    assert classify("DEBUG verbose trace here") == LogLevel.DEBUG
    assert classify("request served ok") == LogLevel.INFO


def test_critical_and_warning_log_lines_keep_their_level() -> None:
    """A CRITICAL log line becomes a CRITICAL LogSignal; a WARNING line stays WARNING (F2h)."""
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    cw_stub.add_response("get_metric_data", {"MetricDataResults": []})
    event_ts_millis = int(_SAMPLE_TS.timestamp() * 1000)
    # First group: a CRITICAL + a WARNING line; second group empty.
    logs_stub.add_response(
        "filter_log_events",
        {
            "events": [
                {"message": "CRITICAL kernel panic", "timestamp": event_ts_millis},
                {"message": "WARNING disk almost full", "timestamp": event_ts_millis},
            ]
        },
    )
    logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    log_signals = [s for s in source.fetch(_WINDOW) if isinstance(s, LogSignal)]

    by_message = {s.message: s.level for s in log_signals}
    assert by_message["CRITICAL kernel panic"] == LogLevel.CRITICAL
    assert by_message["WARNING disk almost full"] == LogLevel.WARNING
    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_error_rate_counts_error_and_above_levels() -> None:
    """`panoptes_log_error_rate` counts error-ish levels: ERROR + CRITICAL (F2h).

    With one ERROR, one CRITICAL, and one WARNING line in a group, the error rate counts
    the two error-and-above lines → 2/3.
    """
    cw = _cloudwatch_client()
    logs = _logs_client()
    cw_stub = Stubber(cw)
    logs_stub = Stubber(logs)

    cw_stub.add_response("get_metric_data", {"MetricDataResults": []})
    event_ts_millis = int(_SAMPLE_TS.timestamp() * 1000)
    logs_stub.add_response(
        "filter_log_events",
        {
            "events": [
                {"message": "ERROR boom", "timestamp": event_ts_millis},
                {"message": "CRITICAL panic", "timestamp": event_ts_millis},
                {"message": "WARNING almost full", "timestamp": event_ts_millis},
            ]
        },
    )
    logs_stub.add_response("filter_log_events", {"events": []})

    cw_stub.activate()
    logs_stub.activate()

    source = CloudWatchSource(_config(), cloudwatch_client=cw, logs_client=logs)
    error_rates = {
        s.labels["log_group"]: s.value
        for s in source.fetch(_WINDOW)
        if isinstance(s, MetricSignal) and s.name == "panoptes_log_error_rate"
    }
    # 2 of 3 lines are error-and-above (ERROR + CRITICAL); WARNING does not count.
    assert error_rates["/app/api"] == pytest.approx(2 / 3)
    cw_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()


def test_paginate_walks_token_to_exhaustion_yielding_every_page() -> None:
    """The shared `_paginate` helper follows a token across pages until it is absent.

    Drives the helper directly (independent of either boto3 fetch) to prove the
    NextToken walk is owned in one place: a three-page stream where each page carries
    a token to the next, the third carrying none. The helper must request each page
    with the prior page's token and yield all three pages in order.
    """
    pages: dict[str | None, dict[str, object]] = {
        None: {"items": ["a"], "token": "p2"},
        "p2": {"items": ["b"], "token": "p3"},
        "p3": {"items": ["c"], "token": None},
    }
    requested_tokens: list[str | None] = []

    def call_page(token: str | None) -> dict[str, object]:
        requested_tokens.append(token)
        return pages[token]

    def read_token(page: dict[str, object]) -> str | None:
        raw = page.get("token")
        return raw if isinstance(raw, str) else None

    collected = list(_paginate(call_page, read_token))

    # Every page yielded in order, and each was requested with the prior page's token.
    assert [page["items"] for page in collected] == [["a"], ["b"], ["c"]]
    assert requested_tokens == [None, "p2", "p3"]


def test_paginate_single_page_when_first_page_has_no_token() -> None:
    """A first page with no token terminates the walk after exactly one request."""
    request_count = 0

    def call_page(token: str | None) -> dict[str, object]:
        nonlocal request_count
        request_count += 1
        return {"items": ["only"], "token": None}

    def read_token(page: dict[str, object]) -> str | None:
        raw = page.get("token")
        return raw if isinstance(raw, str) else None

    collected = list(_paginate(call_page, read_token))

    assert len(collected) == 1
    assert request_count == 1
