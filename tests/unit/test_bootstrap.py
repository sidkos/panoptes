"""Unit tests for the shared adapter-registration bootstrap (`core.bootstrap`).

`register_core_adapters()` imports every core adapter module for its
`@REGISTRY.register(...)` side effect, so the two runnable entrypoints
(`core.collector.main` / `core.mcp.server.main`) can build REAL adapters from a config
without their adapter sets drifting (F2j). These tests pin that contract:

- after `register_core_adapters()`, every core adapter `type` is present on its plane
  registry — `cloudwatch` / `sentry` / `http-health` in SOURCES, `victoriametrics` /
  `passthrough` in STORES, `logging` in NOTIFIERS;
- it is IDEMPOTENT — calling it twice leaves each registry's table unchanged (a
  re-import is a no-op), so an entrypoint that calls it more than once is harmless.

These run inside the root conftest's autouse registry-isolation window, which snapshots
and restores each plane registry around every test — so registering the core adapters
here cannot leak into a sibling test that asserts a pristine registry.
"""

import sys
from collections.abc import Iterator

import pytest
from core.bootstrap import _CORE_ADAPTER_MODULES, register_core_adapters
from core.registry import NOTIFIERS, SOURCES, STORES

# The exact core adapter `type` discriminators each plane must carry after bootstrap.
_EXPECTED_SOURCE_TYPES = {"cloudwatch", "sentry", "http-health"}
_EXPECTED_STORE_TYPES = {"victoriametrics", "passthrough"}
_EXPECTED_NOTIFIER_TYPES = {"logging"}


@pytest.fixture
def _cold_adapter_import_cache() -> Iterator[None]:
    """Evict the core adapter modules from `sys.modules` so bootstrap re-registers them.

    `register_core_adapters()` re-registers a `type` only when its module body actually
    runs. Under pytest the modules are typically already imported (during collection of
    sibling adapter tests), so `import_module` would be a warm-cache no-op and — combined
    with the root conftest restoring each registry's table around every test — the bootstrap
    under test would observe an EMPTY registry. Popping the modules here forces a genuine
    cold re-import (the real process-start condition), so the test exercises the actual
    registration effect rather than a cached no-op. The conftest still restores the global
    registry tables after the test, so this eviction does not leak.
    """
    evicted = {name: sys.modules.pop(name) for name in _CORE_ADAPTER_MODULES if name in sys.modules}
    try:
        yield
    finally:
        # Restore the original module objects so a sibling test's cached import is unaffected.
        sys.modules.update(evicted)


def test_register_core_adapters_registers_every_core_type(_cold_adapter_import_cache: None) -> None:
    """Every core adapter type is present on its plane registry after bootstrap (F2j)."""
    register_core_adapters()

    assert _EXPECTED_SOURCE_TYPES.issubset(SOURCES.available())
    assert _EXPECTED_STORE_TYPES.issubset(STORES.available())
    assert _EXPECTED_NOTIFIER_TYPES.issubset(NOTIFIERS.available())


def test_register_core_adapters_is_idempotent(_cold_adapter_import_cache: None) -> None:
    """Calling bootstrap twice leaves each registry's table unchanged (F2j).

    A re-import of an already-loaded module is a no-op, so a second call must not add,
    drop, or duplicate any registration. The factory IDENTITY for each type must also be
    stable (the same class), proving the second call did not re-register a fresh object.
    The first call (from a cold cache) does the registration; the second must change nothing.
    """
    register_core_adapters()
    sources_after_first = dict(SOURCES._adapters)
    stores_after_first = dict(STORES._adapters)
    notifiers_after_first = dict(NOTIFIERS._adapters)

    register_core_adapters()

    # The name→factory tables are byte-for-byte identical (same keys, same factory objects).
    assert SOURCES._adapters == sources_after_first
    assert STORES._adapters == stores_after_first
    assert NOTIFIERS._adapters == notifiers_after_first
    # And the expected core types are still exactly present (no drop, no duplicate-key churn).
    assert _EXPECTED_SOURCE_TYPES.issubset(SOURCES.available())
    assert _EXPECTED_STORE_TYPES.issubset(STORES.available())
    assert _EXPECTED_NOTIFIER_TYPES.issubset(NOTIFIERS.available())


def test_core_adapters_are_already_registered_at_test_start() -> None:
    """REGRESSION (MAJOR-1): the core adapters are present WITHOUT this test registering them.

    The session-scoped autouse `_register_core_adapters_for_the_session` fixture (root conftest)
    registers the core adapters ONCE before any per-test registry snapshot, so every test —
    including this one, which calls NO registration itself — observes the populated baseline.
    This pins the fix for the order-fragility where an in-fixture registration on the FIRST test
    was wiped by the function-scoped snapshot/restore, leaving later tests with an empty registry
    (`UnknownAdapterError: ... (none registered)`). If the session fixture regresses, this fails.
    """
    # NO register_core_adapters() call here — the session fixture already populated the tables.
    assert _EXPECTED_SOURCE_TYPES.issubset(SOURCES.available()), (
        "core source adapters must be registered at test start (session autouse fixture)"
    )
    assert _EXPECTED_STORE_TYPES.issubset(STORES.available())
    assert _EXPECTED_NOTIFIER_TYPES.issubset(NOTIFIERS.available())
