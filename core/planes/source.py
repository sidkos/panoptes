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

    # Whether the collector should still run `fetch()` when `health()` reports
    # `reachable=False`. Defaults to False — for most sources (cloudwatch/sentry) an
    # unreachable result means "no usable credentials/transport", so fetching is
    # pointless and its signals must NOT reach the store. http-health is the deliberate
    # exception: it maps `reachable=False` to "the MONITORED endpoint is down" and its
    # fetch is purpose-built to emit `panoptes_health_up=0` in exactly that state, so it
    # sets this True — the outage IS the signal and must reach the store (F3a).
    fetch_when_unreachable: bool

    def capabilities(self) -> set[SignalKind]:
        """The signal kinds this source can emit (its plane contract)."""
        ...

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Pull and normalize signals observed within `window`."""
        ...

    def health(self) -> SourceHealth:
        """Probe upstream reachability (read-only)."""
        ...
