"""The `passthrough` store — a deliberately-trivial, no-persistence store.

Used for tests and source-only runs (spec `## API Surface` → Store adapters):
`write` persists nothing but records the last batch in memory so a caller/test can
inspect what *would* have been written, and `query` fails explicitly with
`CapabilityError` rather than returning a silent empty result — because a store
that cannot answer queries must say so, not pretend the metric had no data
(spec "Fail explicitly … never a silent empty result").
"""

from core.errors import CapabilityError
from core.model import CanonicalSignal, MetricQuery, MetricSeries
from core.registry import STORES, ConfigBlock


@STORES.register("passthrough")
class PassthroughStore:
    """A no-op store that records its last write batch and refuses queries."""

    type = "passthrough"

    def __init__(self, config: ConfigBlock) -> None:
        # The config block is unused (the passthrough store needs no endpoint), but
        # the single-positional-`ConfigBlock` constructor signature is the locked
        # registry construction convention — every adapter takes it uniformly.
        self._config = config
        # Records what the most recent `write` was handed; starts empty so a caller
        # can distinguish "nothing written yet" ([]) from a real recorded batch.
        self.last_batch: list[CanonicalSignal] = []

    def write(self, signals: list[CanonicalSignal]) -> None:
        """Record the batch in memory; persist nothing."""
        self.last_batch = signals

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        """Always fail: the passthrough store has no backing data to query."""
        raise CapabilityError(
            f"The 'passthrough' store does not persist metrics and cannot answer "
            f"queries (requested expr: {query.expr!r}). Configure the "
            f"'victoriametrics' store to query metric series."
        )
