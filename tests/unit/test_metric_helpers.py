"""Unit tests for the leaf metric-query helpers (`core/mcp/_metric_helpers.py`).

These pin the defensive branches of the moved helpers that the higher-level tool tests only
exercise indirectly: `_latest_value`'s ORDER-INDEPENDENT max-timestamp logic (otherwise
indistinguishable from naive last-wins) and `_window_minutes`'s bare-integer + non-positive
fallback paths.
"""

import logging
from datetime import UTC, datetime

import pytest
from core.mcp._metric_helpers import (
    _DEFAULT_WINDOW_MINUTES,
    _latest_value,
    _window_minutes,
)
from core.model import MetricSeries


def _ts(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, 0, 0, tzinfo=UTC)


# --- _latest_value: order-independent (max-timestamp wins, not list-last) --------------------


def test_latest_value_picks_max_timestamp_from_descending_points() -> None:
    """With DESCENDING-timestamp points, the value at the MAX timestamp wins (not list-last).

    A naive last-wins would return the final list element (the OLDEST here); the helper must
    return the value at the latest timestamp regardless of list order.
    """
    series = MetricSeries(
        metric="panoptes_health_up",
        labels={"env": "dev"},
        # Points DESCENDING by timestamp: the list-last is the OLDEST.
        points=[(_ts(3), 30.0), (_ts(2), 20.0), (_ts(1), 10.0)],
    )
    assert _latest_value([series]) == 30.0


def test_latest_value_picks_max_timestamp_from_shuffled_points() -> None:
    """SHUFFLED timestamps → the max-timestamp value still wins (order-independent)."""
    series = MetricSeries(
        metric="panoptes_health_up",
        labels={"env": "dev"},
        points=[(_ts(2), 20.0), (_ts(5), 50.0), (_ts(1), 10.0), (_ts(3), 30.0)],
    )
    assert _latest_value([series]) == 50.0


def test_latest_value_picks_max_timestamp_across_interleaved_series() -> None:
    """The latest value wins ACROSS series too — not just within one series' point list."""
    older = MetricSeries(
        metric="panoptes_health_up",
        labels={"env": "dev", "url": "a"},
        points=[(_ts(4), 40.0)],
    )
    newer = MetricSeries(
        metric="panoptes_health_up",
        labels={"env": "dev", "url": "b"},
        points=[(_ts(7), 70.0)],
    )
    # The newer series is listed FIRST and SECOND to prove order does not decide the winner.
    assert _latest_value([newer, older]) == 70.0
    assert _latest_value([older, newer]) == 70.0


def test_latest_value_none_when_no_points() -> None:
    """No series / empty-point series → None (the caller omits the metric, never invents 0.0)."""
    assert _latest_value([]) is None
    empty = MetricSeries(metric="m", labels={"env": "dev"}, points=[])
    assert _latest_value([empty]) is None


# --- _window_minutes: bare-integer + non-positive fallback paths ----------------------------


def test_window_minutes_parses_a_bare_integer_string_as_minutes() -> None:
    """A bare integer string (`'30'`) is interpreted as 30 minutes (forward-compatible path)."""
    assert _window_minutes("30") == 30


def test_window_minutes_zero_falls_back_to_default_with_non_positive_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare `'0'` is non-positive → default fallback + a DISTINCT 'non-positive' warning (NIT-2).

    A recognized-but-non-positive integer gets a precise message, not the generic 'unrecognized'
    one (which would be misleading — 0 IS a number, it is just out of range).
    """
    with caplog.at_level(logging.WARNING):
        result = _window_minutes("0")
    assert result == _DEFAULT_WINDOW_MINUTES
    messages = " ".join(record.getMessage().lower() for record in caplog.records)
    assert "non-positive" in messages, "a non-positive int must surface the distinct message"


def test_window_minutes_negative_falls_back_to_default_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A negative `'-5'` is unrecognized → default fallback AND the offending value surfaced."""
    with caplog.at_level(logging.WARNING):
        result = _window_minutes("-5")
    assert result == _DEFAULT_WINDOW_MINUTES
    assert any("-5" in record.getMessage() for record in caplog.records)
