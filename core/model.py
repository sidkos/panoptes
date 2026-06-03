"""The canonical signal model — Panoptes' single normalized view of monitoring data.

Every upstream tool's data is folded into one of the four `CanonicalSignal`
variants (metric / log / incident / trace), each carrying an explicit `kind`
discriminator so a `CanonicalSignal` union can be branched without isinstance
chains. `env` is mandatory on every signal — a `__post_init__` validator raises a
`PanoptesError` subclass if it is absent, because every downstream face (Grafana's
`env` template variable, the MCP tools' `env` argument) keys on it.

Greenfield, precisely typed: no `Any`, no bare `dict`. Labels are an open-ended
`dict[str, str]` alias (label *keys* are open, values are always strings).
"""

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from core.errors import PanoptesError

# Label maps are open-ended string->string. A TypedDict would over-constrain the
# keys (sources add arbitrary upstream-specific labels); the one invariant — `env`
# must be present — is enforced by each signal's `__post_init__`, not the type.
Labels = dict[str, str]

_ENV_LABEL = "env"


class SignalKind(enum.Enum):
    """Discriminator for the `CanonicalSignal` union."""

    METRIC = "metric"
    LOG = "log"
    # TRACE's contract is present so adapters can target it, but no v0.1 source
    # produces trace signals (spec `## Data Model`).
    TRACE = "trace"
    INCIDENT = "incident"


class LogLevel(enum.Enum):
    """Severity of a `LogSignal`."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class IncidentLevel(enum.Enum):
    """Severity of an `IncidentSignal` (Sentry-shaped: adds FATAL above ERROR)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


def _require_env_label(labels: Labels) -> None:
    """Raise if the mandatory `env` label is missing from a signal's labels.

    Centralized so every signal's `__post_init__` enforces the same invariant with
    one consistent, named error message.
    """
    if _ENV_LABEL not in labels:
        raise PanoptesError(
            f"Every signal must carry an 'env' label; got labels with keys {sorted(labels.keys())}."
        )


@dataclass(frozen=True)
class MetricSignal:
    """A single metric sample. `kind` is fixed to `SignalKind.METRIC`."""

    name: str
    value: float
    timestamp: datetime
    labels: Labels
    kind: SignalKind = field(default=SignalKind.METRIC, init=False)

    def __post_init__(self) -> None:
        _require_env_label(self.labels)


@dataclass(frozen=True)
class LogSignal:
    """A single log line."""

    timestamp: datetime
    message: str
    level: LogLevel
    labels: Labels
    kind: SignalKind = field(default=SignalKind.LOG, init=False)

    def __post_init__(self) -> None:
        _require_env_label(self.labels)


@dataclass(frozen=True)
class IncidentSignal:
    """An aggregated incident/issue (e.g. a Sentry issue), with a seen-count."""

    id: str
    title: str
    level: IncidentLevel
    first_seen: datetime
    last_seen: datetime
    count: int
    labels: Labels
    kind: SignalKind = field(default=SignalKind.INCIDENT, init=False)

    def __post_init__(self) -> None:
        _require_env_label(self.labels)


@dataclass(frozen=True)
class Span:
    """One span within a `TraceSignal` (element type of `TraceSignal.spans`)."""

    name: str
    start: datetime
    duration_ms: float
    parent_id: str | None


@dataclass(frozen=True)
class TraceSignal:
    """A distributed trace. Declared in v0.1; no source produces it yet."""

    trace_id: str
    spans: list[Span]
    duration_ms: float
    labels: Labels
    kind: SignalKind = field(default=SignalKind.TRACE, init=False)

    def __post_init__(self) -> None:
        _require_env_label(self.labels)


# The discriminated union every source produces and every store consumes.
CanonicalSignal = MetricSignal | LogSignal | IncidentSignal | TraceSignal


@dataclass(frozen=True)
class TimeWindow:
    """A half-open `[start, end)` query window."""

    start: datetime
    end: datetime

    @staticmethod
    def last(minutes: int) -> "TimeWindow":
        """A window ending 'now' (UTC) and spanning the trailing `minutes`."""
        end = datetime.now(UTC)
        return TimeWindow(start=end - timedelta(minutes=minutes), end=end)


@dataclass(frozen=True)
class MetricQuery:
    """A PromQL range query against the store."""

    expr: str
    window: TimeWindow
    step_seconds: int


@dataclass(frozen=True)
class MetricSeries:
    """A resolved metric series: one label-set's `(timestamp, value)` points."""

    metric: str
    labels: Labels
    points: list[tuple[datetime, float]]


@dataclass(frozen=True)
class SourceHealth:
    """The result of a source reachability probe."""

    reachable: bool
    detail: str
    checked_at: datetime


@dataclass(frozen=True)
class Alert:
    """A notifier payload."""

    name: str
    severity: str
    message: str
    labels: Labels


@dataclass(frozen=True)
class DashboardPack:
    """A resolved dashboard pack reference.

    `tier` distinguishes always-shipped core packs (resolved under
    `core/dashboards/`) from injected consumer packs (resolved under the mounted
    `dashboards.consumer_pack.path`); `json_path` is the resolved location the
    Grafana provider globs/syncs.
    """

    id: str
    tier: Literal["core", "consumer"]
    json_path: Path
