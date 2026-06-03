"""Integration: a synthetic `panoptes_*` write round-trips through the live VM store.

Proves the store-to-VictoriaMetrics-to-PromQL read-back leg of the pipeline (spec
`## Tests` → Integration, bullet 1). It writes synthetic `panoptes_*` metric signals
through the REAL `VictoriaMetricsStore.write` (the same `/api/v1/import` path the
collector uses), polls VM readiness + ingest-visibility (Risk R13 — never an
immediate assertion), then reads them back with the store's own PromQL
`/api/v1/query_range` and asserts the series + the `env` label round-trip exactly.

Synthetic-only (Risk R10): no live AWS/Sentry. The collector's per-source fetch path
is covered by the unit suite (mocked upstreams); here the proof under test is the
store↔VM transport + PromQL read-back against a real container, which is exactly what
`collector.run_once()` hands its store, so writing via the store directly exercises
the identical write code path with deterministic synthetic data.
"""

from datetime import timedelta

import pytest
from core.model import CanonicalSignal, MetricQuery, MetricSignal, TimeWindow
from core.stores.victoriametrics import VictoriaMetricsStore

from .conftest import VictoriaMetricsHandle, now_utc

pytestmark = pytest.mark.integration

# A unique synthetic series so a re-run (or a parallel suite) never reads a stale or
# foreign sample — keyed on a dedicated env label value.
_SERIES_ENV = "integration-collector"
_METRIC_NAME = "panoptes_health_up"


def test_collector_writes_round_trip_through_victoriametrics(
    victoriametrics: VictoriaMetricsHandle,
) -> None:
    """Synthetic `panoptes_*` signals written to the live VM read back via PromQL.

    Writes two timestamped samples through the real store, polls VM until the series
    is visible, then range-queries them back and asserts both the values and the
    mandatory `env` label survived the round-trip.
    """
    store = VictoriaMetricsStore({"url": victoriametrics.base_url})

    sample_time = now_utc()
    labels = {"env": _SERIES_ENV, "url": f"{victoriametrics.base_url}/health"}
    signals: list[CanonicalSignal] = [
        MetricSignal(
            name=_METRIC_NAME,
            value=1.0,
            timestamp=sample_time - timedelta(seconds=60),
            labels=dict(labels),
        ),
        MetricSignal(
            name=_METRIC_NAME,
            value=1.0,
            timestamp=sample_time,
            labels=dict(labels),
        ),
    ]
    store.write(signals)

    # Poll VM until the just-written series is queryable (absorbs ingest lag, R13).
    expr = f'{_METRIC_NAME}{{env="{_SERIES_ENV}"}}'
    victoriametrics.wait_for_series(expr)

    # PromQL range read-back over a window that brackets both samples.
    window = TimeWindow(
        start=sample_time - timedelta(minutes=5),
        end=sample_time + timedelta(minutes=5),
    )
    series_list = store.query(MetricQuery(expr=expr, window=window, step_seconds=60))

    assert series_list, "expected at least one series back from VictoriaMetrics"
    series = series_list[0]
    assert series.metric == _METRIC_NAME
    # The mandatory `env` label round-tripped intact (the model invariant the whole
    # design keys on).
    assert series.labels.get("env") == _SERIES_ENV
    # The synthetic value (1.0) is present at every returned grid point.
    assert series.points, "expected sample points in the returned series"
    assert all(value == pytest.approx(1.0) for _timestamp, value in series.points)
