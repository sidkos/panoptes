"""The `Source` plug-plane Protocol.

A Source reads from one upstream monitoring tool and normalizes its data into
`CanonicalSignal`s. It is read-only with respect to observed systems (spec
`## Authorization Rules`). Adapters self-register on `core.registry.SOURCES`.
"""

from typing import Protocol, runtime_checkable

from core.model import CanonicalSignal, SignalKind, SourceHealth, TimeWindow


@runtime_checkable
class Source(Protocol):
    """Reads + normalizes one upstream into `CanonicalSignal`s."""

    type: str

    def capabilities(self) -> set[SignalKind]:
        """The signal kinds this source can emit (its plane contract)."""
        ...

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Pull and normalize signals observed within `window`."""
        ...

    def health(self) -> SourceHealth:
        """Probe upstream reachability (read-only)."""
        ...
