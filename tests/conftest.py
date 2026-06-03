"""Root test fixtures — global registry isolation across every test (F8).

The four plane registries (`SOURCES`/`STORES`/`NOTIFIERS`/`DASHBOARD_PROVIDERS`) are
module singletons populated by the `@REGISTRY.register(...)` self-registration
decorators. Several tests import the demo consumer pack into these REAL globals
(`test_demo_pack`, `test_source_capabilities`, `test_core_purity_guard`), which leaks a
`demo-synthetic` store registration across the session. That made the additive-injection
invariant in `test_core_purity_guard` order-fragile: it snapshots `STORES.available()`
"before import", but if a sibling test imported the pack first, the snapshot already
contained `demo-synthetic` and the additive-delta assertion silently weakened to its `or`
escape (asserting nothing).

This autouse fixture snapshots each registry's internal name→factory table BEFORE every
test and RESTORES it after, so:
- a global registration made inside a test cannot leak into a later test, and
- the "pre-import baseline" each additive-injection test snapshots is REAL regardless of
  test execution order (verified by re-running the suite in reverse file order).

It mutates the existing singletons in place (rather than swapping them) because the
self-registration decorators and `core.config`'s `from_globals()` default both bind the
singleton identities at import time; replacing the objects would orphan those bindings.
"""

from collections.abc import Callable, Iterator

import pytest
from core.registry import (
    DASHBOARD_PROVIDERS,
    NOTIFIERS,
    SOURCES,
    STORES,
    ConfigBlock,
    Registry,
)


def _restore_table[PlaneT](
    registry: Registry[PlaneT], original: dict[str, Callable[[ConfigBlock], PlaneT]]
) -> None:
    """Replace a registry's internal name→factory table with a snapshot copy.

    Generic over the plane Protocol (PEP 695) so each registry is restored at its own
    precise type — `Registry[T]` is invariant, so there is no shared supertype to unify
    them under. The snapshot only touches the `_adapters` mapping (the factories are never
    mutated, only the name→factory bindings).
    """
    registry._adapters.clear()
    registry._adapters.update(original)


@pytest.fixture(autouse=True)
def _isolate_global_registries() -> Iterator[None]:
    """Snapshot the four global registries before a test; restore them after (F8).

    A shallow copy of each registry's `_adapters` table is enough: the factories
    themselves are never mutated, only the name→factory MAPPING is (by registration). On
    teardown the original mapping is restored, so any registration a test performed (e.g.
    importing the demo pack) is rolled back and cannot leak across tests.
    """
    sources_snapshot = dict(SOURCES._adapters)
    stores_snapshot = dict(STORES._adapters)
    notifiers_snapshot = dict(NOTIFIERS._adapters)
    dashboards_snapshot = dict(DASHBOARD_PROVIDERS._adapters)
    try:
        yield
    finally:
        _restore_table(SOURCES, sources_snapshot)
        _restore_table(STORES, stores_snapshot)
        _restore_table(NOTIFIERS, notifiers_snapshot)
        _restore_table(DASHBOARD_PROVIDERS, dashboards_snapshot)
