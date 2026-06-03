"""The `Notifier` plug-plane Protocol.

A Notifier renders an `Alert` to some destination. Adapters self-register on
`core.registry.NOTIFIERS`. v0.1 ships only the `logging` notifier.
"""

from typing import Protocol, runtime_checkable

from core.model import Alert


@runtime_checkable
class Notifier(Protocol):
    """Delivers an `Alert`."""

    type: str

    def notify(self, alert: Alert) -> None:
        """Render/deliver the alert."""
        ...
