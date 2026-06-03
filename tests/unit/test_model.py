"""Phase 1 unit tests for the canonical signal model (`core/model.py`).

Covers (spec `## Data Model` / playbook Phase 1 table):
- every signal type constructs from valid fields;
- the **mandatory `env` label** validator raises when `env` is absent;
- union discrimination by the explicit `kind` discriminator;
- enum round-trips (value <-> member);
- `TimeWindow.last(minutes=...)` math (end - start == the requested span).

All tests are deterministic and fully typed (mypy `--strict` includes `tests/`).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core.errors import PanoptesError
from core.model import (
    Alert,
    CanonicalSignal,
    DashboardPack,
    IncidentLevel,
    IncidentSignal,
    LogLevel,
    LogSignal,
    MetricQuery,
    MetricSeries,
    MetricSignal,
    SignalKind,
    SourceHealth,
    Span,
    TimeWindow,
    TraceSignal,
)

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


def _labels_with_env() -> dict[str, str]:
    """A minimal valid label map (every signal requires `env`)."""
    return {"env": "dev"}


def test_metric_signal_constructs_with_env_label() -> None:
    signal = MetricSignal(
        name="panoptes_health_up",
        value=1.0,
        timestamp=_NOW,
        labels=_labels_with_env(),
    )
    assert signal.kind is SignalKind.METRIC
    assert signal.name == "panoptes_health_up"
    assert signal.value == 1.0
    assert signal.labels["env"] == "dev"


def test_log_signal_constructs_with_env_label() -> None:
    signal = LogSignal(
        timestamp=_NOW,
        message="something happened",
        level=LogLevel.ERROR,
        labels=_labels_with_env(),
    )
    assert signal.kind is SignalKind.LOG
    assert signal.level is LogLevel.ERROR


def test_incident_signal_constructs_with_env_label() -> None:
    signal = IncidentSignal(
        id="abc123",
        title="NullPointer",
        level=IncidentLevel.ERROR,
        first_seen=_NOW,
        last_seen=_NOW,
        count=7,
        labels=_labels_with_env(),
    )
    assert signal.kind is SignalKind.INCIDENT
    assert signal.count == 7


def test_trace_signal_constructs_with_env_label() -> None:
    span = Span(name="db.query", start=_NOW, duration_ms=12.5, parent_id=None)
    signal = TraceSignal(
        trace_id="t-1",
        spans=[span],
        duration_ms=12.5,
        labels=_labels_with_env(),
    )
    assert signal.kind is SignalKind.TRACE
    assert signal.spans[0].name == "db.query"
    assert signal.spans[0].parent_id is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MetricSignal(name="m", value=0.0, timestamp=_NOW, labels={}),
        lambda: LogSignal(timestamp=_NOW, message="x", level=LogLevel.INFO, labels={}),
        lambda: IncidentSignal(
            id="i",
            title="t",
            level=IncidentLevel.INFO,
            first_seen=_NOW,
            last_seen=_NOW,
            count=1,
            labels={},
        ),
        lambda: TraceSignal(trace_id="t", spans=[], duration_ms=1.0, labels={}),
    ],
)
def test_missing_env_label_raises(factory: object) -> None:
    """Every signal's `__post_init__` rejects a label map lacking `env`."""
    assert callable(factory)
    with pytest.raises(PanoptesError) as excinfo:
        factory()
    assert "env" in str(excinfo.value)


def test_union_discrimination_by_kind() -> None:
    signals: list[CanonicalSignal] = [
        MetricSignal(name="m", value=1.0, timestamp=_NOW, labels=_labels_with_env()),
        LogSignal(timestamp=_NOW, message="x", level=LogLevel.WARNING, labels=_labels_with_env()),
        IncidentSignal(
            id="i",
            title="t",
            level=IncidentLevel.FATAL,
            first_seen=_NOW,
            last_seen=_NOW,
            count=2,
            labels=_labels_with_env(),
        ),
        TraceSignal(trace_id="t", spans=[], duration_ms=3.0, labels=_labels_with_env()),
    ]
    kinds = {signal.kind for signal in signals}
    assert kinds == {
        SignalKind.METRIC,
        SignalKind.LOG,
        SignalKind.INCIDENT,
        SignalKind.TRACE,
    }


def test_signal_kind_round_trip() -> None:
    for member in SignalKind:
        assert SignalKind(member.value) is member


def test_log_level_round_trip() -> None:
    for member in LogLevel:
        assert LogLevel(member.value) is member


def test_incident_level_round_trip() -> None:
    for member in IncidentLevel:
        assert IncidentLevel(member.value) is member


def test_time_window_last_span_is_exact() -> None:
    window = TimeWindow.last(minutes=15)
    assert window.end - window.start == timedelta(minutes=15)


def test_time_window_last_end_is_now_ish() -> None:
    before = datetime.now(UTC)
    window = TimeWindow.last(minutes=5)
    after = datetime.now(UTC)
    assert before <= window.end <= after


def test_query_and_aggregation_types_construct() -> None:
    window = TimeWindow.last(minutes=10)
    query = MetricQuery(expr="up", window=window, step_seconds=15)
    series = MetricSeries(
        metric="panoptes_health_up",
        labels=_labels_with_env(),
        points=[(_NOW, 1.0)],
    )
    health = SourceHealth(reachable=True, detail="ok", checked_at=_NOW)
    alert = Alert(name="down", severity="critical", message="m", labels=_labels_with_env())
    pack = DashboardPack(id="overview", tier="core", json_path=Path("overview/dashboard.json"))
    assert query.step_seconds == 15
    assert series.points[0][1] == 1.0
    assert health.reachable is True
    assert alert.severity == "critical"
    assert pack.tier == "core"
