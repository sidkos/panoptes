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

When `cost_budget_name` is configured the source ALSO reads Cost Explorer +
budgets (v0.3): `ce:GetCostAndUsage` (grouped by service) → one
`panoptes_cost_spend{env,service}` gauge per service, and `budgets:DescribeBudget`
→ one `panoptes_cost_budget_burn{env}` gauge (actual/limit). These calls are rate-
limited to at most once per `cost_poll_interval_seconds` (default 3600) via an
injectable clock seam (G3 — CE bills per request), while the cheaper metric/log
feeds run every cycle. The CE/budgets IAM grant is consumer-side IaC (a Phase-6
IAM.md note), NOT part of this adapter's IRSA.

This source is read-only w.r.t. AWS: only `get_metric_data` / `filter_log_events` /
`get_paginator` / `assume_role` / `get_caller_identity` / `get_cost_and_usage` /
`describe_budget` are called — all read actions, none in the no-write guard's
mutation-verb set.

Type-stub-only imports (`mypy_boto3_*`) are guarded behind `if TYPE_CHECKING:` — they
are NOT installed in slim CI, so a bare runtime import would crash; the runtime
`boto3` import is unconditional.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

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
from core.sources.probe import probe_health
from core.validation import optional_int_field

if TYPE_CHECKING:
    # Type-stub-only imports — present at type-check time (boto3-stubs is a dev dep)
    # but NOT installed in slim CI, so they must never run at import time.
    from mypy_boto3_budgets import BudgetsClient
    from mypy_boto3_ce import CostExplorerClient
    from mypy_boto3_cloudwatch import CloudWatchClient
    from mypy_boto3_cloudwatch.type_defs import GetMetricDataOutputTypeDef
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_logs.type_defs import FilterLogEventsResponseTypeDef
    from mypy_boto3_sts import STSClient

# Derived-metric name (spec `## Data Model` — `panoptes_` prefix avoids PromQL
# collisions with native upstream metric names).
_METRIC_LOG_ERROR_RATE = "panoptes_log_error_rate"

# Cost gauges (v0.3) — the `get_cost` MCP tool + the Cost dashboard both render these from
# the store (two-faces-one-store parity). `panoptes_cost_spend{env,service}` carries the
# unblended spend per AWS service over the cost window; `panoptes_cost_budget_burn{env}` is
# the configured budget's actual/limit burn fraction.
_METRIC_COST_SPEND = "panoptes_cost_spend"
_METRIC_COST_BUDGET_BURN = "panoptes_cost_budget_burn"

# The cost window the CE `GetCostAndUsage` read spans, in days (a rolling trailing window —
# matched by the Cost dashboard's `30d` range). Hard-coded rather than configurable to keep
# the flat-config surface small; the burn gauge is independent of this (budget-period scoped).
_COST_WINDOW_DAYS = 30

# CE `GetCostAndUsage` granularity + metric. MONTHLY over the trailing window keeps the call
# cheap (CE bills per request); UnblendedCost is the spend figure the budget alerts track.
# `_COST_GRANULARITY` is annotated `Literal["MONTHLY"]` (not a bare `str`) so it matches the
# boto3-stubs `get_cost_and_usage` `Granularity` overload without an `# type: ignore`.
_COST_GRANULARITY: Literal["MONTHLY"] = "MONTHLY"
_COST_METRIC = "UnblendedCost"

# The default cost-read cadence (G3): the CE/budgets calls fire at most once per this many
# seconds even though `fetch` runs every poll cycle. 3600 s (hourly) keeps CE request spend
# negligible while the cheap metric/log feeds stay real-time. Overridable per source via the
# flat `cost_poll_interval_seconds` config field.
_DEFAULT_COST_POLL_INTERVAL_SECONDS = 3600

# Message-substring markers → their TRUE `LogLevel` (F2h). CloudWatch log events carry no
# structured level, so the message text is inspected. Ordered MOST-SEVERE FIRST so a line
# carrying multiple markers classifies at its highest severity (e.g. a CRITICAL line that
# also mentions "error" stays CRITICAL). The previous classifier collapsed CRITICAL/FATAL
# to ERROR and never emitted WARNING/DEBUG — a lossy mapping this list fixes.
_LEVEL_MARKERS: tuple[tuple[tuple[str, ...], LogLevel], ...] = (
    (("CRITICAL", "FATAL"), LogLevel.CRITICAL),
    (("ERROR", "EXCEPTION", "TRACEBACK"), LogLevel.ERROR),
    (("WARNING", "WARN"), LogLevel.WARNING),
    (("DEBUG",), LogLevel.DEBUG),
)

# The levels that count toward `panoptes_log_error_rate` (F2h): ERROR and above
# (ERROR + CRITICAL). WARNING/DEBUG/INFO are NOT errors, so they do not inflate the rate.
_ERROR_RATE_LEVELS = frozenset({LogLevel.ERROR, LogLevel.CRITICAL})


def _default_clock() -> datetime:
    """Wall-clock `now` in UTC — the production `clock` seam for the cost cadence gate."""
    return datetime.now(UTC)


class _PollGate:
    """A once-per-interval cadence gate for an expensive gated read (the cost-path seam).

    Concentrates the "fire at most once per N seconds, but only advance the marker on a
    CONFIRMED success" discipline the cost path needs (G3 — CE bills per request, so the read
    must not fire every poll cycle). The cost path calls `is_due()` to decide whether to issue
    the CE/budgets read this cycle, and `mark_done()` ONLY after that read succeeds — a failed
    read leaves the marker un-advanced so the next cycle retries rather than blacking out cost
    for a whole interval.

    Module-private to `cloudwatch.py` (YAGNI): the metrics/logs/cost paths share one
    assume-role seam, so the cost path stays in this source; the gate is its named cadence
    concern, not a shared module — promote it only when a second consumer appears.

    Invariants:
        - The FIRST `is_due()` (no prior `mark_done`) is ALWAYS due.
        - After a `mark_done`, `is_due()` is true only once `clock() - last_done >= interval`
          (the boundary is inclusive).
        - `last_done` advances ONLY on an explicit `mark_done()` — a failed sub-fetch that
          skips it leaves the gate due next cycle (the retry contract).
        - Time comes ONLY from the injected `clock` seam — never a direct wall-clock call — so
          a test drives cadence with a fake clock and no `sleep`.
    """

    def __init__(self, interval_seconds: int, clock: Callable[[], datetime]) -> None:
        """Build a gate over `interval_seconds`, reading time from the injected `clock`.

        Args:
            interval_seconds: The minimum gap between two `is_due()`-true cycles. MUST be
                positive — a zero/negative interval is a misconfiguration and raises
                `PanoptesError` at construction (fail fast, never silently disable the gate).
            clock: The time source (`() -> datetime`); the gate never calls wall-clock time
                directly, so a test injects a fake clock to assert cadence without sleeping.
        """
        if interval_seconds <= 0:
            raise PanoptesError(
                f"_PollGate interval_seconds must be positive; got {interval_seconds}."
            )
        self._interval = timedelta(seconds=interval_seconds)
        self._clock = clock
        # `None` until the first `mark_done` — so the first `is_due()` is always due.
        self._last_done_at: datetime | None = None

    def is_due(self) -> bool:
        """True when no read has completed yet, or the cadence interval has fully elapsed.

        The first call (`_last_done_at is None`) is always due; thereafter a read is due only
        once `clock() - last_done >= interval`.
        """
        if self._last_done_at is None:
            return True
        return self._clock() - self._last_done_at >= self._interval

    def mark_done(self) -> None:
        """Advance the marker to `clock()` — call ONLY after a confirmed-successful read.

        A failed read MUST NOT call this, so the gate stays due next cycle (the retry
        contract): blacking out the gated read for a full interval on a transient failure is
        exactly what this seam avoids.
        """
        self._last_done_at = self._clock()


def _extract_amount(amount_block: object) -> float | None:
    """Pull a stringified float `Amount` out of a CE/budgets `{Amount, Unit}` block.

    Both CE results and budgets payloads model money as `{"Amount": "12.34", "Unit":
    "USD"}`. Returns the parsed float, or `None` when the block is missing/malformed so the
    caller skips it rather than crashing.
    """
    if not isinstance(amount_block, dict):
        return None
    amount = amount_block.get("Amount")
    if not isinstance(amount, str):
        return None
    try:
        return float(amount)
    except ValueError:
        return None


def _paginate[PageT](
    call_page: Callable[[str | None], PageT],
    read_token: Callable[[PageT], str | None],
) -> Iterator[PageT]:
    """Walk a token-paged API, yielding each page until the token is exhausted.

    Owns the one NextToken-style paginate loop that `GetMetricData` (`NextToken`) and
    `FilterLogEvents` (`nextToken`) both hand-rolled identically. The only things that
    differed between them were the client call, the request token key, and the
    response token key — all three are now the caller's concern:

    - `call_page(token)` builds the per-page request (adding the continuation token
      under the API's own key when it is not `None`) and returns the raw page.
    - `read_token(page)` extracts the next continuation token from a page, or `None`
      when the API signals there are no further pages.

    The helper itself is boto3-agnostic (no client, no token-key knowledge), so it
    stays a module-private of this AWS adapter rather than a public seam. The first
    page is always requested with `None`; the walk stops as soon as `read_token`
    returns a falsy token, preserving the original `if not token: break` semantics.
    """
    next_token: str | None = None
    while True:
        page = call_page(next_token)
        yield page
        next_token = read_token(page)
        if not next_token:
            return


@SOURCES.register("cloudwatch")
class CloudWatchSource:
    """Reads CloudWatch metrics + Logs into `MetricSignal`s and `LogSignal`s."""

    type = "cloudwatch"

    # An unreachable cloudwatch source means credentials/transport are unusable, so a
    # fetch is pointless and its signals must NOT reach the store — keep the collector's
    # default skip-on-unreachable behavior (F3a).
    fetch_when_unreachable = False

    def __init__(
        self,
        config: ConfigBlock,
        sts_client: "STSClient | None" = None,
        cloudwatch_client: "CloudWatchClient | None" = None,
        logs_client: "CloudWatchLogsClient | None" = None,
        ce_client: "CostExplorerClient | None" = None,
        budgets_client: "BudgetsClient | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Read flat config fields; accept injectable boto3 client + clock seams (all optional).

        The client params default to `None`, so the registry's single-positional
        `cls(config)` still constructs the source; a test injects a stubbed client so
        `botocore.stub.Stubber` attaches to the exact instance the source uses. Real
        runs leave them `None` and the clients are lazily built from `region`/`profile`
        on first use.

        The cost read path (CE `GetCostAndUsage` + budgets `DescribeBudget`) is OPT-IN: it
        activates only when `cost_budget_name` is configured (a flat string field). When set,
        every `fetch` MAY emit `panoptes_cost_*` gauges — but the CE/budgets calls themselves
        are rate-limited to at most once per `cost_poll_interval_seconds` (default 3600) via
        the injectable `clock` seam (G3: CE bills per request, so an every-cycle call would
        be wasteful). The `clock` defaults to `datetime.now(UTC)`; a test injects a fake clock
        to assert the cadence without a wall-clock `sleep`.
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
        # Cost path (v0.3) — opt-in via `cost_budget_name`; cadence via the int field.
        self._cost_budget_name = optional_str_field(config, "cost_budget_name")
        self._cost_poll_interval_seconds = optional_int_field(
            config, "cost_poll_interval_seconds", _DEFAULT_COST_POLL_INTERVAL_SECONDS
        )
        # Injected seams (None in production; a stubbed client in tests).
        self._sts_client = sts_client
        self._cloudwatch_client = cloudwatch_client
        self._logs_client = logs_client
        self._ce_client = ce_client
        self._budgets_client = budgets_client
        # The clock seam (G3 cadence gate). `None` → wall-clock `datetime.now(UTC)`. Used by
        # the cost gate AND for the budget-burn gauge timestamp.
        self._clock = clock if clock is not None else _default_clock
        # The once-per-interval cadence gate for the CE/budgets cost read (G3). It owns the
        # "fire at most once per interval, advance only on success" discipline; the source
        # just calls `is_due()` / `mark_done()`.
        self._cost_gate = _PollGate(self._cost_poll_interval_seconds, self._clock)

    def capabilities(self) -> set[SignalKind]:
        """cloudwatch emits metric samples and log lines (incident is v0.2)."""
        return {SignalKind.METRIC, SignalKind.LOG}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Page CloudWatch metrics + Logs over `window`, normalizing all feeds.

        Returns the metric samples, then the log lines, then the per-log-group derived
        `panoptes_log_error_rate` gauges, then (when cost is configured AND the
        once-per-interval cadence gate is open) the `panoptes_cost_*` gauges. The metric
        and log reads are fully paginated so a multi-page result is never truncated; the
        cost read is gated to at most once per `cost_poll_interval_seconds` (G3).
        """
        signals: list[CanonicalSignal] = []
        signals.extend(self._fetch_metrics(window))
        log_signals, error_rates = self._fetch_logs(window)
        signals.extend(log_signals)
        signals.extend(error_rates)
        signals.extend(self._fetch_cost(window))
        return signals

    def health(self) -> SourceHealth:
        """Resolve credentials (assume-role if configured) as the reachability probe.

        Credential resolution is the reachability concern for an AWS source, so the
        assume-role attempt lives here. The no-raise + no-`str(exc)`-leak discipline lives in
        `core.sources.probe.probe_health`: an `AssumeRole` denial (or any transport/auth
        failure) becomes `reachable=False` with a generic class-name-only detail (never a
        verbatim `str(ClientError/BotoCoreError)`, which can echo the role ARN / account id /
        external id into the MCP-visible `describe_health` rollup, F3c). The seam's broad
        `except Exception` UNIFIES what was previously a two-branch
        `(ClientError, BotoCoreError)` + `PanoptesError` catch — all credential-resolution
        failures funnel to the same leak-free summary. The `source_label` carries "credential"
        so the detail reads `"cloudwatch credential resolution unreachable (...)"`.
        """
        return probe_health(
            "cloudwatch credential resolution",
            self._resolve_credentials,
            success_detail_factory=lambda _result: (
                f"cloudwatch credentials resolved for region {self._region}"
            ),
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

        def call_page(token: str | None) -> "GetMetricDataOutputTypeDef":
            kwargs: dict[str, object] = {
                "MetricDataQueries": queries,
                "StartTime": window.start,
                "EndTime": window.end,
            }
            # `GetMetricData`'s continuation key is `NextToken` (capitalized).
            if token is not None:
                kwargs["NextToken"] = token
            return client.get_metric_data(**kwargs)  # type: ignore[arg-type]

        signals: list[MetricSignal] = []
        for response in _paginate(call_page, lambda page: page.get("NextToken")):
            signals.extend(self._normalize_metric_results(response.get("MetricDataResults", [])))
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
        event becomes a `LogSignal` at its classified level; the fraction of
        ERROR-and-above events (ERROR + CRITICAL, F2h) becomes one
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
                # The error rate counts ERROR-and-above (ERROR + CRITICAL), F2h — WARNING/
                # DEBUG/INFO are not errors and must not inflate the gauge.
                if level in _ERROR_RATE_LEVELS:
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
        start_millis = int(window.start.timestamp() * 1000)
        end_millis = int(window.end.timestamp() * 1000)

        def call_page(token: str | None) -> "FilterLogEventsResponseTypeDef":
            kwargs: dict[str, object] = {
                "logGroupName": log_group,
                "startTime": start_millis,
                "endTime": end_millis,
            }
            # `FilterLogEvents`' continuation key is `nextToken` (lowercase) — the only
            # divergence from the metrics walk, now isolated to this per-page builder.
            if token is not None:
                kwargs["nextToken"] = token
            return client.filter_log_events(**kwargs)  # type: ignore[arg-type]

        events: list[_LogEvent] = []
        for response in _paginate(call_page, lambda page: page.get("nextToken")):
            for raw_event in response.get("events", []):
                parsed = self._parse_log_event(raw_event)
                if parsed is not None:
                    events.append(parsed)
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

    def _fetch_cost(self, window: TimeWindow) -> list[MetricSignal]:
        """Read CE spend + budget burn into `panoptes_cost_*` gauges (gated, opt-in).

        A no-op (empty list) unless `cost_budget_name` is configured AND the once-per-
        `cost_poll_interval_seconds` cadence gate is open (G3 — CE bills per request, so the
        call must not fire every poll cycle). When the gate opens, issues exactly two read
        calls — CE `GetCostAndUsage` (grouped by service) and budgets `DescribeBudget` — and
        normalizes them into one `panoptes_cost_spend{env,service}` gauge per service plus one
        `panoptes_cost_budget_burn{env}` gauge. A CE/budgets error is swallowed (logged via
        the empty-list return) the same way an unreachable feed is — it must NOT crash the
        cycle, since cost is an auxiliary signal.
        """
        if self._cost_budget_name is None:
            # Cost path not configured — never touch CE/budgets.
            return []
        if not self._cost_gate.is_due():
            # Within the cadence window — skip the CE/budgets calls this cycle (G3). The
            # store keeps serving the gauges from the previous read.
            return []
        try:
            spend = self._fetch_cost_spend(window)
            burn = self._fetch_budget_burn()
        except (ClientError, BotoCoreError):
            # An auth/transport failure on the cost feed is non-fatal: leave the cadence
            # marker un-advanced (do NOT call mark_done) so the next cycle retries, and emit
            # nothing this cycle.
            return []
        # Only advance the cadence marker on a SUCCESSFUL read, so a failed call retries
        # next cycle rather than blacking out cost for a full interval.
        self._cost_gate.mark_done()
        return spend + burn

    def _fetch_cost_spend(self, window: TimeWindow) -> list[MetricSignal]:
        """Read CE `GetCostAndUsage` grouped by service → one spend gauge per service.

        The CE window is a trailing `_COST_WINDOW_DAYS` ending at the fetch window's end, at
        MONTHLY granularity (cheap + matches the budget period). Each `ResultsByTime` group's
        unblended-cost amount becomes one `panoptes_cost_spend{env,service}` gauge stamped at
        the window end.
        """
        client = self._cost_explorer()
        end = window.end
        start = end - timedelta(days=_COST_WINDOW_DAYS)
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start.date().isoformat(), "End": end.date().isoformat()},
            Granularity=_COST_GRANULARITY,
            Metrics=[_COST_METRIC],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        return self._normalize_cost_results(response.get("ResultsByTime", []), end)

    def _normalize_cost_results(self, results: object, sample_time: datetime) -> list[MetricSignal]:
        """Normalize CE `ResultsByTime` groups into per-service spend gauges.

        Each group carries `Keys` (the service name) and a `Metrics` map whose
        `UnblendedCost.Amount` is the spend string. A malformed group is skipped rather than
        aborting the batch. Spend across multiple time periods for the same service is summed
        so the gauge is one value per service over the whole window.
        """
        if not isinstance(results, list):
            return []
        spend_by_service: dict[str, float] = {}
        for period in results:
            if not isinstance(period, dict):
                continue
            for group in period.get("Groups", []):
                service, amount = self._parse_cost_group(group)
                if service is None or amount is None:
                    continue
                spend_by_service[service] = spend_by_service.get(service, 0.0) + amount
        return [
            MetricSignal(
                name=_METRIC_COST_SPEND,
                value=amount,
                timestamp=sample_time,
                # Exact cost-gauge label set: `{env, service}` (mirrors the get_cost reader).
                labels={"env": self._env, "service": service},
            )
            for service, amount in spend_by_service.items()
        ]

    @staticmethod
    def _parse_cost_group(group: object) -> tuple[str | None, float | None]:
        """Parse one CE group into `(service, unblended_amount)`; `(None, None)` if malformed.

        A group's `Keys[0]` is the SERVICE dimension value; `Metrics[UnblendedCost][Amount]`
        is the spend (a stringified float in CE responses). Anything missing or unparseable
        collapses to `(None, None)` so the caller skips it.
        """
        if not isinstance(group, dict):
            return None, None
        keys = group.get("Keys")
        if not isinstance(keys, list) or not keys or not isinstance(keys[0], str):
            return None, None
        metrics = group.get("Metrics")
        if not isinstance(metrics, dict):
            return None, None
        unblended = metrics.get(_COST_METRIC)
        if not isinstance(unblended, dict):
            return None, None
        amount = unblended.get("Amount")
        if not isinstance(amount, str):
            return None, None
        try:
            return keys[0], float(amount)
        except ValueError:
            return None, None

    def _fetch_budget_burn(self) -> list[MetricSignal]:
        """Read budgets `DescribeBudget` → one `panoptes_cost_budget_burn{env}` gauge.

        The burn fraction is `actual_spend / budget_limit` for the configured budget. A zero
        or missing limit yields no gauge (a divide-by-zero guard) rather than a crash. The
        gauge is stamped at the clock's `now` (budget state is period-scoped, not window-tied).
        """
        budget_name = self._cost_budget_name
        assert budget_name is not None  # guarded by _fetch_cost
        client = self._budgets()
        account_id = self._resolve_account_id()
        response = client.describe_budget(AccountId=account_id, BudgetName=budget_name)
        burn = self._compute_budget_burn(response.get("Budget"))
        if burn is None:
            return []
        return [
            MetricSignal(
                name=_METRIC_COST_BUDGET_BURN,
                value=burn,
                timestamp=self._clock(),
                labels={"env": self._env},
            )
        ]

    @staticmethod
    def _compute_budget_burn(budget: object) -> float | None:
        """Compute `actual / limit` from a budgets `Budget` payload; `None` if not derivable.

        `BudgetLimit.Amount` is the limit and `CalculatedSpend.ActualSpend.Amount` is the
        actual, both stringified floats. A missing/zero limit or unparseable amount yields
        `None` (no gauge) — never a divide-by-zero or a crash.
        """
        if not isinstance(budget, dict):
            return None
        limit = _extract_amount(budget.get("BudgetLimit"))
        calculated = budget.get("CalculatedSpend")
        actual = (
            _extract_amount(calculated.get("ActualSpend")) if isinstance(calculated, dict) else None
        )
        if limit is None or actual is None or limit <= 0.0:
            return None
        return actual / limit

    def _resolve_account_id(self) -> str:
        """Resolve the AWS account id (required by `DescribeBudget`) via STS GetCallerIdentity.

        Uses the same injectable sts client seam as assume-role, so a test stubs it. The
        account id is the budgets API's mandatory partition key.
        """
        identity = self._sts().get_caller_identity()
        account = identity.get("Account")
        if not isinstance(account, str) or not account:
            raise PanoptesError(
                "cloudwatch cost path could not resolve the AWS account id "
                "(STS GetCallerIdentity returned no Account)."
            )
        return account

    def _cost_explorer(self) -> "CostExplorerClient":
        """Return the injected Cost Explorer client, or lazily build one.

        CE is a global service (always `us-east-1` regardless of the source's region), so the
        lazily-built client pins that region; an injected client is used as-is.
        """
        if self._ce_client is not None:
            return self._ce_client
        session = self._session()
        # CE has a single global endpoint in us-east-1; the source's region does not apply.
        self._ce_client = session.client("ce", region_name="us-east-1")
        return self._ce_client

    def _budgets(self) -> "BudgetsClient":
        """Return the injected budgets client, or lazily build one (global, us-east-1)."""
        if self._budgets_client is not None:
            return self._budgets_client
        session = self._session()
        # The budgets API is global with a single us-east-1 endpoint.
        self._budgets_client = session.client("budgets", region_name="us-east-1")
        return self._budgets_client

    @staticmethod
    def _classify_level(message: str) -> LogLevel:
        """Classify a log message to its TRUE `LogLevel` from its severity marker (F2h).

        CloudWatch log events carry no structured level, so the message text is matched
        (case-insensitive) against `_LEVEL_MARKERS`, which is ordered most-severe-first so
        a line carrying multiple markers classifies at its highest severity. CRITICAL/FATAL
        → CRITICAL, ERROR/EXCEPTION/TRACEBACK → ERROR, WARNING/WARN → WARNING, DEBUG →
        DEBUG; anything unmatched → INFO. (Previously CRITICAL/FATAL lossily collapsed to
        ERROR and WARNING/DEBUG were never emitted.)
        """
        upper = message.upper()
        for markers, level in _LEVEL_MARKERS:
            if any(marker in upper for marker in markers):
                return level
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
