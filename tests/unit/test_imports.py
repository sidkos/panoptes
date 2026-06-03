"""Phase 0 smoke test: the package imports and exposes the four plane registries.

Asserts the registries exist, are distinct ``Registry`` instances, and are keyed
by their plane discriminator. Everything below the spine imports these.
"""

from core import DASHBOARD_PROVIDERS, NOTIFIERS, SOURCES, STORES
from core.registry import Registry


def test_four_registries_are_registry_instances() -> None:
    for registry in (SOURCES, STORES, NOTIFIERS, DASHBOARD_PROVIDERS):
        assert isinstance(registry, Registry)


def test_four_registries_are_distinct_instances() -> None:
    registries = [SOURCES, STORES, NOTIFIERS, DASHBOARD_PROVIDERS]
    assert len({id(registry) for registry in registries}) == 4


def test_registries_are_keyed_by_plane() -> None:
    assert SOURCES.kind == "source"
    assert STORES.kind == "store"
    assert NOTIFIERS.kind == "notifier"
    assert DASHBOARD_PROVIDERS.kind == "dashboard"
