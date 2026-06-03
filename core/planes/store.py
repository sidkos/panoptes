"""The `Store` plug-plane Protocol.

A Store persists derived metrics and answers PromQL range queries. Adapters
self-register on `core.registry.STORES`.
"""

from typing import Protocol, runtime_checkable

from core.model import CanonicalSignal, MetricQuery, MetricSeries


@runtime_checkable
class Store(Protocol):
    """Writes signals and answers metric queries."""

    type: str

    def write(self, signals: list[CanonicalSignal]) -> None:
        """Persist a batch of signals (only derived metrics are stored in v0.1)."""
        ...

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        """Execute a PromQL range query, returning the resolved series."""
        ...
