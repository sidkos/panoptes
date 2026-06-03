"""The `DashboardProvider` plug-plane Protocol.

A DashboardProvider provisions both tiers of dashboard packs (core + injected
consumer). Adapters self-register on `core.registry.DASHBOARD_PROVIDERS`. v0.1
ships only the file-provisioning `grafana` provider.
"""

from typing import Protocol, runtime_checkable

from core.model import DashboardPack


@runtime_checkable
class DashboardProvider(Protocol):
    """Provisions a set of `DashboardPack`s (read-only — no MCP write path)."""

    type: str

    def provision(self, packs: list[DashboardPack]) -> None:
        """Sync the given packs into the provisioning target."""
        ...
