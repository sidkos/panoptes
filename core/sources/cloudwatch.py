"""The `cloudwatch` source — CloudWatch metrics + Logs into canonical signals.

`fetch` does two paged reads and normalizes both:

- **`GetMetricData`** (paged via `NextToken`) → one `MetricSignal` per `(metric,
  timestamp)` sample.
- **`FilterLogEvents`** (paged via `nextToken`) → one `LogSignal` per log event, plus
  a derived `panoptes_log_error_rate` gauge `MetricSignal` per configured log group
  (the fraction of events at ERROR level) — exact label set `{env, log_group}` (spec
  `## Data Model` → Derived metrics).

Capability set: `{METRIC, LOG}` (alarm→incident is v0.2).

**AWS auth via an injectable seam (required for `botocore.stub.Stubber`).** Assume-role
is attempted inside **`health()`** because credential resolution is a reachability
concern — an `AssumeRole` denial therefore surfaces through the same `health()`
try/continue boundary the collector honors, and does NOT crash the cycle. The `sts`,
cloudwatch, and logs clients are all obtained via overridable constructor params
(default `None`, so the registry's `cls(config)` still works); a test injects a
stubbed client so `Stubber` attaches to the exact instance. `assume_role_arn` takes
precedence over `profile`; when set, `external_id` is passed through to the STS
`assume_role` call as `ExternalId` (IAM.md confused-deputy guard).

This source is read-only w.r.t. AWS: only `get_metric_data` / `filter_log_events` /
`get_paginator` / `assume_role` are called — none are in the no-write guard's
mutation-verb set.

Type-stub-only imports (`mypy_boto3_*`) are guarded behind `if TYPE_CHECKING:` — they
are NOT installed in slim CI, so a bare runtime import would crash; the runtime
`boto3` import is unconditional.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from core.errors import PanoptesError
from core.model import (
    CanonicalSignal,
    LogLevel,
    LogSignal,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.sources._config import (
    optional_str_field,
    require_str_field,
    require_str_list_field,
)

if TYPE_CHECKING:
    # Type-stub-only imports — present at type-check time (boto3-stubs is a dev dep)
    # but NOT installed in slim CI, so they must never run at import time.
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_sts import STSClient

# Derived-metric name (spec `## Data Model` — `panoptes_` prefix avoids PromQL
# collisions with native upstream metric names).
_METRIC_LOG_ERROR_RATE = "panoptes_log_error_rate"

# Substrings that mark a log event as ERROR-level for the error-rate derivation.
# CloudWatch log events carry no structured level, so the message text is inspected.
_ERROR_MARKERS = ("ERROR", "CRITICAL", "FATAL", "EXCEPTION", "TRACEBACK")


@SOURCES.register("cloudwatch")
class CloudWatchSource:
    """Reads CloudWatch metrics + Logs into `MetricSignal`s and `LogSignal`s."""

    type = "cloudwatch"

    def __init__(
        self,
        config: ConfigBlock,
        sts_client: "STSClient | None" = None,
        cloudwatch_client: "CloudWatchClient | None" = None,
        logs_client: "CloudWatchLogsClient | None" = None,
    ) -> None:
        """Read flat config fields; accept injectable boto3 client seams (all optional).

        The three client params default to `None`, so the registry's single-positional
        `cls(config)` still constructs the source; a test injects a stubbed client so
        `botocore.stub.Stubber` attaches to the exact instance the source uses. Real
        runs leave them `None` and the clients are lazily built from `region`/`profile`
        on first use.
        """
        self._region = require_str_field(config, "region", self.type)
        self._namespace = require_str_field(config, "namespace", self.type)
        self._metric_names = require_str_list_field(config, "metric_names", self.type)
        self._log_groups = require_str_list_field(config, "log_groups", self.type)
        # `env` is mandatory: stamped on every emitted signal (model invariant).
        self._env = require_str_field(config, "env", self.type)
        # Optional auth fields; `assume_role_arn` takes precedence over `profile`.
        self._profile = optional_str_field(config, "profile")
        self._assume_role_arn = optional_str_field(config, "assume_role_arn")
        self._external_id = optional_str_field(config, "external_id")
        # Injected seams (None in production; a stubbed client in tests).
        self._sts_client = sts_client
        self._cloudwatch_client = cloudwatch_client
        self._logs_client = logs_client

    def capabilities(self) -> set[SignalKind]:
        """cloudwatch emits metric samples and log lines (incident is v0.2)."""
        return {SignalKind.METRIC, SignalKind.LOG}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Page CloudWatch metrics + Logs over `window`, normalizing both feeds.

        Returns the metric samples, then the log lines, then the per-log-group derived
        `panoptes_log_error_rate` gauges. Both upstream reads are fully paginated so a
        result spanning multiple pages is never truncated.
        """
        signals: list[CanonicalSignal] = []
        signals.extend(self._fetch_metrics(window))
        log_signals, error_rates = self._fetch_logs(window)
        signals.extend(log_signals)
        signals.extend(error_rates)
        return signals

    def health(self) -> SourceHealth:
        """Resolve credentials (assume-role if configured) as the reachability probe.

        Credential resolution is the reachability concern for an AWS source, so the
        assume-role attempt lives here. An `AssumeRole` denial (expired/denied) is
        caught and surfaced as `reachable=False` with a clear auth message — it does
        NOT propagate, so the collector's per-source try/continue boundary keeps the
        rest of the cycle running.
        """
        checked_at = datetime.now(UTC)
        try:
            self._resolve_credentials()
        except (ClientError, BotoCoreError) as exc:
            return SourceHealth(
                reachable=False,
                detail=f"cloudwatch credential resolution failed: {exc}",
                checked_at=checked_at,
            )
        except PanoptesError as exc:
            return SourceHealth(reachable=False, detail=str(exc), checked_at=checked_at)
        return SourceHealth(
            reachable=True,
            detail=f"cloudwatch credentials resolved for region {self._region}",
            checked_at=checked_at,
        )

    def _resolve_credentials(self) -> None:
        """Attempt assume-role when configured; a no-op when only a profile is used.

        `assume_role_arn` takes precedence over `profile`. When set, `external_id` is
        passed through to STS as `ExternalId` (the cross-account trust policy's
        confused-deputy guard, IAM.md §A). A denial raises `ClientError`, surfaced by
        `health()`.
        """
        if self._assume_role_arn is None:
            # Profile-only (or default-chain) auth — nothing to assume; reachability is
            # implicitly the ability to construct the clients, which boto3 does lazily.
            return
        self._assume_role()

    def _assume_role(self) -> None:
        """Call STS `assume_role` on the (possibly injected) sts client.

        Isolated into its own method so a test can monkeypatch it OR attach a
        `botocore.stub.Stubber` to the injected sts client and
        `add_client_error("assume_role", "AccessDenied")`. `external_id` is forwarded
        as `ExternalId` only when supplied.
        """
        assert self._assume_role_arn is not None  # guarded by _resolve_credentials
        sts = self._sts()
        # Explicit keyword args (not `**dict`) so the boto3-stubs `assume_role`
        # overload types match — `ExternalId` is the cross-account confused-deputy
        # guard, forwarded only when an external id is configured.
        if self._external_id is not None:
            sts.assume_role(
                RoleArn=self._assume_role_arn,
                RoleSessionName="panoptes-cloudwatch",
                ExternalId=self._external_id,
            )
        else:
            sts.assume_role(
                RoleArn=self._assume_role_arn,
                RoleSessionName="panoptes-cloudwatch",
            )

    def _sts(self) -> "STSClient":
        """Return the injected sts client, or lazily build one from the profile/region."""
        if self._sts_client is not None:
            return self._sts_client
        session = self._session()
        self._sts_client = session.client("sts", region_name=self._region)
        return self._sts_client

    def _cloudwatch(self) -> "CloudWatchClient":
        """Return the injected cloudwatch client, or lazily build one."""
        if self._cloudwatch_client is not None:
            return self._cloudwatch_client
        session = self._session()
        self._cloudwatch_client = session.client("cloudwatch", region_name=self._region)
        return self._cloudwatch_client

    def _logs(self) -> "CloudWatchLogsClient":
        """Return the injected logs client, or lazily build one."""
        if self._logs_client is not None:
            return self._logs_client
        session = self._session()
        self._logs_client = session.client("logs", region_name=self._region)
        return self._logs_client

    def _session(self) -> "boto3.session.Session":
        """Build a boto3 session honoring the configured `profile` when set."""
        if self._profile is not None:
            return boto3.session.Session(profile_name=self._profile, region_name=self._region)
        return boto3.session.Session(region_name=self._region)

    def _fetch_metrics(self, window: TimeWindow) -> list[MetricSignal]:
        """Page `GetMetricData` over the window and normalize every sample.

        One `MetricData` query is issued per configured metric name; the response's
        `MetricDataResults` carry parallel `Timestamps`/`Values` arrays that are zipped
        into one `MetricSignal` each. Pagination follows `NextToken` until absent.
        """
        client = self._cloudwatch()
        queries = [
            {
                "Id": f"q{index}",
                "MetricStat": {
                    "Metric": {"Namespace": self._namespace, "MetricName": name},
                    "Period": 60,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }
            for index, name in enumerate(self._metric_names)
        ]

        signals: list[MetricSignal] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, object] = {
                "MetricDataQueries": queries,
                "StartTime": window.start,
                "EndTime": window.end,
            }
            if next_token is not None:
                kwargs["NextToken"] = next_token
            response = client.get_metric_data(**kwargs)  # type: ignore[arg-type]
            signals.extend(self._normalize_metric_results(response.get("MetricDataResults", [])))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return signals

    def _normalize_metric_results(self, results: object) -> list[MetricSignal]:
        """Normalize a page of `MetricDataResults` into `MetricSignal`s.

        Each result's `Label` becomes the metric name; its parallel `Timestamps` and
        `Values` arrays are zipped into one sample each, every signal stamped with the
        configured `env` plus the metric `label`.
        """
        if not isinstance(results, list):
            return []
        signals: list[MetricSignal] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            label = result.get("Label")
            metric_name = label if isinstance(label, str) else "unknown"
            timestamps = result.get("Timestamps")
            values = result.get("Values")
            if not isinstance(timestamps, list) or not isinstance(values, list):
                continue
            for timestamp, value in zip(timestamps, values, strict=False):
                if not isinstance(timestamp, datetime) or not isinstance(value, int | float):
                    continue
                signals.append(
                    MetricSignal(
                        name=metric_name,
                        value=float(value),
                        timestamp=self._as_utc(timestamp),
                        labels={"env": self._env, "metric": metric_name},
                    )
                )
        return signals

    def _fetch_logs(self, window: TimeWindow) -> tuple[list[LogSignal], list[MetricSignal]]:
        """Page `FilterLogEvents` per log group, normalizing events + deriving error rate.

        Returns `(log_signals, error_rate_metrics)`. For each configured log group every
        event becomes a `LogSignal`; the fraction of ERROR-marked events becomes one
        `panoptes_log_error_rate` gauge with exact labels `{env, log_group}`.
        """
        client = self._logs()
        log_signals: list[LogSignal] = []
        error_rates: list[MetricSignal] = []
        sample_time = window.end

        for log_group in self._log_groups:
            events = self._page_log_events(client, log_group, window)
            error_count = 0
            for event in events:
                level = self._classify_level(event.message)
                if level == LogLevel.ERROR:
                    error_count += 1
                log_signals.append(
                    LogSignal(
                        timestamp=event.timestamp,
                        message=event.message,
                        level=level,
                        labels={"env": self._env, "log_group": log_group},
                    )
                )
            error_rate = (error_count / len(events)) if events else 0.0
            error_rates.append(
                MetricSignal(
                    name=_METRIC_LOG_ERROR_RATE,
                    value=error_rate,
                    timestamp=sample_time,
                    # Exact derived-metric label set (spec): `{env, log_group}`.
                    labels={"env": self._env, "log_group": log_group},
                )
            )
        return log_signals, error_rates

    def _page_log_events(
        self, client: "CloudWatchLogsClient", log_group: str, window: TimeWindow
    ) -> list["_LogEvent"]:
        """Page `FilterLogEvents` for one log group, following `nextToken` to exhaustion."""
        events: list[_LogEvent] = []
        next_token: str | None = None
        start_millis = int(window.start.timestamp() * 1000)
        end_millis = int(window.end.timestamp() * 1000)
        while True:
            kwargs: dict[str, object] = {
                "logGroupName": log_group,
                "startTime": start_millis,
                "endTime": end_millis,
            }
            if next_token is not None:
                kwargs["nextToken"] = next_token
            response = client.filter_log_events(**kwargs)  # type: ignore[arg-type]
            for raw_event in response.get("events", []):
                parsed = self._parse_log_event(raw_event)
                if parsed is not None:
                    events.append(parsed)
            next_token = response.get("nextToken")
            if not next_token:
                break
        return events

    def _parse_log_event(self, raw_event: object) -> "_LogEvent | None":
        """Parse one raw FilterLogEvents event into a `(timestamp, message)` pair.

        Skips a malformed event (missing message/timestamp) rather than aborting the
        page — one bad event should not lose the rest of the batch.
        """
        if not isinstance(raw_event, dict):
            return None
        message = raw_event.get("message")
        timestamp_millis = raw_event.get("timestamp")
        if not isinstance(message, str) or not isinstance(timestamp_millis, int | float):
            return None
        timestamp = datetime.fromtimestamp(float(timestamp_millis) / 1000.0, tz=UTC)
        return _LogEvent(timestamp=timestamp, message=message)

    @staticmethod
    def _classify_level(message: str) -> LogLevel:
        """Classify a log message as ERROR (matches an error marker) or INFO otherwise.

        CloudWatch log events carry no structured level, so the message text is matched
        against `_ERROR_MARKERS` (case-insensitive). The error rate counts only ERROR.
        """
        upper = message.upper()
        if any(marker in upper for marker in _ERROR_MARKERS):
            return LogLevel.ERROR
        return LogLevel.INFO

    @staticmethod
    def _as_utc(timestamp: datetime) -> datetime:
        """Ensure a datetime is timezone-aware UTC (boto3 returns aware datetimes)."""
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)


class _LogEvent:
    """A parsed CloudWatch log event — just the fields the source normalizes.

    A tiny internal value type (not a `CanonicalSignal`) so `_page_log_events` can
    return a typed list without leaking boto3's loosely-typed event dicts upward.
    """

    __slots__ = ("message", "timestamp")

    def __init__(self, timestamp: datetime, message: str) -> None:
        self.timestamp = timestamp
        self.message = message
