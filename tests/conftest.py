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
from core.bootstrap import register_core_adapters
from core.registry import (
    DASHBOARD_PROVIDERS,
    NOTIFIERS,
    SOURCES,
    STORES,
    ConfigBlock,
    Registry,
)


@pytest.fixture(scope="session", autouse=True)
def _register_core_adapters_for_the_session() -> None:
    """Populate the plane registries with the core adapters ONCE, before any per-test snapshot.

    Root cause of the order-fragility (MAJOR-1, cycle 3): `register_core_adapters()` registers
    a `type` only when its module body RUNS, which under Python's import cache happens exactly
    once per process. A fixture that called it INSIDE a test (e.g. `mcp_http_server`) would —
    on a cold run where that test is FIRST — register into a table the function-scoped
    `_isolate_global_registries` had ALREADY snapshotted EMPTY, so teardown would restore the
    table to empty and wipe the core adapters for every later test (the integration suite then
    failed when `test_mcp_http_e2e.py` ran in isolation: 2 passed, 4 `UnknownAdapterError`).

    Running registration HERE, in a SESSION-scoped autouse fixture, fixes it structurally:
    pytest sets up higher-scoped autouse fixtures BEFORE lower-scoped ones, so this runs once
    before the FIRST function-scoped `_isolate_global_registries` snapshot — every per-test
    snapshot therefore captures the POPULATED baseline, and the restore preserves it. No
    integration fixture needs to register adapters itself any more, and single-file +
    full-suite runs are both stable regardless of order. (`test_bootstrap` still exercises the
    genuine cold-import registration via its own `_cold_adapter_import_cache` module eviction.)
    """
    register_core_adapters()


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
