"""Phase 2 unit tests for the `passthrough` store (`core/stores/passthrough.py`).

The passthrough store is the deliberately-trivial test/source-only store
(spec `## API Surface` → Store adapters): `write` persists nothing but records
the last batch in-memory (so a test can assert what would have been written), and
`query` fails explicitly with `CapabilityError` rather than returning a silent
empty result (spec "Fail explicitly … never a silent empty result").
"""

from datetime import UTC, datetime

import pytest
from core.errors import CapabilityError
from core.model import CanonicalSignal, MetricQuery, MetricSignal, TimeWindow
from core.stores.passthrough import PassthroughStore

_FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _metric_signal(name: str, value: float) -> MetricSignal:
    """Build a minimal `env`-labelled metric signal for the store tests."""
    return MetricSignal(
        name=name,
        value=value,
        timestamp=_FIXED_TIMESTAMP,
        labels={"env": "dev"},
    )


def test_type_discriminator_is_passthrough() -> None:
    store = PassthroughStore({})
    assert store.type == "passthrough"


def test_write_is_noop_and_records_last_batch() -> None:
    store = PassthroughStore({})
    batch: list[CanonicalSignal] = [
        _metric_signal("panoptes_health_up", 1.0),
        _metric_signal("panoptes_health_latency_ms", 12.5),
    ]
    store.write(batch)
    # The write itself persists nothing; it only records the batch for inspection.
    assert store.last_batch == batch


def test_last_batch_starts_empty_before_any_write() -> None:
    store = PassthroughStore({})
    assert store.last_batch == []


def test_query_raises_capability_error() -> None:
    store = PassthroughStore({})
    query = MetricQuery(
        expr="panoptes_health_up",
        window=TimeWindow(start=_FIXED_TIMESTAMP, end=_FIXED_TIMESTAMP),
        step_seconds=60,
    )
    with pytest.raises(CapabilityError) as excinfo:
        store.query(query)
    # The error must name the store so an operator understands why no data came back.
    assert "passthrough" in str(excinfo.value)
