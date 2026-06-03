"""Collector resilience: a CloudWatch assume-role denial never aborts the cycle (F2k).

This is the integration-shaped proof that the collector's per-source resilience boundary
holds for the REAL `CloudWatchSource` credential path — not just for the fake sources in
`test_collector`. A `CloudWatchSource` configured with an `assume_role_arn` and an injected
STS client stubbed to return `AccessDenied` for `assume_role` surfaces the denial through
`health()` as `reachable=False` (the source maps a credential failure to an unreachable
health result rather than raising). Run via `Collector.run_once()` alongside a healthy
sibling source, the contract is:

- NO exception propagates out of `run_once()` (one bad source never aborts the cycle);
- the healthy sibling's signals DO reach the store;
- the CloudWatch failure is error-logged (the per-source failure record);
- the CloudWatch source's OWN signals do NOT reach the store (it is skipped on the
  unreachable health result, so its fetch — which would attempt upstream calls with no
  usable credentials — never runs).

boto3 is exercised entirely through `botocore.stub.Stubber` against an injected STS client
(NO live AWS). The healthy sibling is a tiny in-test fake `Source`.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.stub import Stubber
from core.collector import Collector
from core.config import ResolvedConfig, ResolvedEnvironment, ResolvedSource
from core.model import (
    Alert,
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.sources.cloudwatch import CloudWatchSource

if TYPE_CHECKING:
    # Type-stub-only import (boto3-stubs is a dev dep) — guarded so a bare runtime import
    # can never crash in slim CI.
    from mypy_boto3_sts import STSClient

_REGION = "us-east-1"
_ENV = "dev"
_ROLE_ARN = "arn:aws:iam::123456789012:role/PanoptesReadRole"
_EXTERNAL_ID = "panoptes-ext-id"


def _now() -> datetime:
    return datetime.now(UTC)


class _HealthySiblingSource:
    """A tiny healthy fake `Source` emitting one metric, alongside the denied cloudwatch."""

    type = "sentry"

    def __init__(self) -> None:
        self.fetch_calls = 0

    def capabilities(self) -> set[SignalKind]:
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        self.fetch_calls += 1
        return [
            MetricSignal(
                name="panoptes_sentry_count", value=1.0, timestamp=_now(), labels={"env": _ENV}
            )
        ]

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _RecordingStore:
    """A `Store` recording every batch handed to `write` (so we can assert what landed)."""

    type = "recording"

    def __init__(self) -> None:
        self.batches: list[list[CanonicalSignal]] = []

    def write(self, signals: list[CanonicalSignal]) -> None:
        self.batches.append(signals)

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []

    def written_names(self) -> set[str]:
        names: set[str] = set()
        for batch in self.batches:
            for signal in batch:
                if isinstance(signal, MetricSignal):
                    names.add(signal.name)
        return names


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _sts_client() -> "STSClient":
    return boto3.client("sts", region_name=_REGION)


def _cloudwatch_config() -> dict[str, str | list[str]]:
    return {
        "region": _REGION,
        "namespace": "AWS/ApplicationELB",
        "metric_names": ["RequestCount"],
        "log_groups": ["/app/api"],
        "env": _ENV,
        "assume_role_arn": _ROLE_ARN,
        "external_id": _EXTERNAL_ID,
    }


def test_assume_role_denial_does_not_abort_cycle_and_sibling_is_stored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An AssumeRole denial keeps the cycle running; only the sibling's signals are stored."""
    sts = _sts_client()
    sts_stub = Stubber(sts)
    sts_stub.add_client_error("assume_role", "AccessDenied", "not authorized")
    sts_stub.activate()

    denied_cloudwatch = CloudWatchSource(_cloudwatch_config(), sts_client=sts)
    healthy_sibling = _HealthySiblingSource()
    store = _RecordingStore()

    config = ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    ResolvedSource(
                        source=denied_cloudwatch, fetch_timeout_seconds=30, poll_interval_seconds=60
                    ),
                    ResolvedSource(
                        source=healthy_sibling, fetch_timeout_seconds=30, poll_interval_seconds=60
                    ),
                ],
            )
        },
        store=store,
        notifiers=[_NoopNotifier()],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )
    collector = Collector(config)

    # No exception propagates — one denied source must never abort the cycle.
    with caplog.at_level(logging.ERROR, logger="core.collector"):
        collector.run_once()

    # The healthy sibling's signals reached the store.
    assert healthy_sibling.fetch_calls == 1
    assert "panoptes_sentry_count" in store.written_names()
    # The cloudwatch source was skipped on its unreachable health result — none of its
    # derived metrics (e.g. panoptes_log_error_rate) reached the store.
    assert "panoptes_log_error_rate" not in store.written_names()
    # The cloudwatch failure was error-logged.
    assert "cloudwatch" in caplog.text

    sts_stub.assert_no_pending_responses()
