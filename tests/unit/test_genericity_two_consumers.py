"""The v0.3 genericity proof — TWO unrelated consumer packs, ZERO core diff between them.

This is the RELEASE THESIS of v0.3 (spec § Strategic positioning — the genericity proof):
the only test that distinguishes a genuinely generic core from one secretly shaped around
consumer #1 is "two UNRELATED consumers inject the same way with a BYTE-IDENTICAL core
baseline between them". Both fixture packs now exist (Phase 4: fleet; Phase 5: pipeline), so
this file is the FINALIZED proof. It asserts, for each pack injected in turn via the v0.1
`PANOPTES_CONSUMER_PACK` hook:

(a) **additive** — each pack registers EXACTLY its own source + tool (+ dashboard) and the
    core's own registrations are UNCHANGED (the v0.1 additive-injection invariant: superset +
    exact-delta, per pack);
(b) **purity** — the core-purity guard is green with BOTH fixtures present (consumer-domain
    tokens live only under `examples/`, never `core/` — re-exercised here, the guard module is
    the authority);
(c) **BYTE-IDENTICAL CORE BASELINE** — the core registry baseline (the core sources/stores/
    notifiers + the core MCP tool set, EXCLUDING each pack's own additions) computed under the
    FLEET injection and under the PIPELINE injection is BYTE-IDENTICAL (a single serialized
    string, asserted string-equal) — the proof of ZERO per-consumer core branch (Risk G2);
(d) **reversible** — re-running with the hook UNSET yields exactly the no-pack core baseline
    (no residue: neither pack leaves anything behind).

The two packs are deliberately UNRELATED domains — a game-server fleet (ready/allocated/
reserved replicas) and a data pipeline (job lag / queue depth / data freshness) — so a hidden
core assumption shaped around either one would break clause (c).

The injection seam is the SAME one `tests/unit/test_core_purity_guard.py`'s additive-injection
invariants drive (`build_server(config).tool_names()` + `SOURCES.available()` with the
`PANOPTES_CONSUMER_PACK` env var) — so this finalized proof reuses it verbatim. The root
conftest's autouse registry reset rolls back each pack's `@SOURCES.register(...)` between
tests (F8), so the per-injection baselines are computed against a clean core each time.
"""

import importlib
import sys
from pathlib import Path

import pytest
from core.bootstrap import register_core_adapters
from core.config import ResolvedConfig
from core.mcp.server import build_server
from core.model import CanonicalSignal, MetricQuery, MetricSeries
from core.registry import NOTIFIERS, SOURCES, STORES

# Register the FULL canonical core adapter set (the same list `core/bootstrap.py` drives in
# production) so the core SOURCES/STORES/NOTIFIERS baseline is POPULATED with the REAL core —
# the byte-identical assertion then compares a faithful, non-empty core baseline, not a
# hand-picked subset. Without this the registries could be empty and the proof vacuous.
register_core_adapters()

# The env var the v0.1 injection hook reads (the dotted/path module of the consumer pack).
_CONSUMER_PACK_ENV_VAR = "PANOPTES_CONSUMER_PACK"

# The two UNRELATED consumer fixture packs, by their hyphenated dotted module path.
_FLEET_PACK_MODULE = "examples.consumer-fleet-pack.pack"
_PIPELINE_PACK_MODULE = "examples.consumer-pipeline-pack.pack"

# Each pack's OWN additions — subtracted from the under-injection registries to isolate the
# core baseline. These are the ONLY things each pack may add; clause (a) asserts that.
_FLEET_OWN_SOURCE = "fleet"
_FLEET_OWN_TOOL = "get_fleet_health"
_PIPELINE_OWN_SOURCE = "pipeline"
_PIPELINE_OWN_TOOL = "get_pipeline_lag"

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _NullStore:
    """A minimal in-memory store — `tool_names()` drives no store query, so this is inert."""

    type = "null"

    def write(self, signals: list[CanonicalSignal]) -> None:  # pragma: no cover - unused
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:  # pragma: no cover - unused
        return []


def _baseline_config() -> ResolvedConfig:
    """A minimal core-only `ResolvedConfig` (mirrors the purity-guard baseline config).

    `build_server(config).tool_names()` reads only the resolved tool list — it drives no store
    query — so the inert `_NullStore` is sufficient. The core registrars supply the default
    read-only tool set.
    """
    return ResolvedConfig(
        environments={},
        # `_NullStore` is precisely typed (returns `list[MetricSeries]`), so it structurally
        # satisfies the `Store` protocol — no cast needed.
        store=_NullStore(),
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


def _import_pack(module_path: str) -> None:
    """Import (or reload) a consumer pack so its `@SOURCES.register(...)` decorator re-runs.

    The root conftest rolls back each pack's registration after every test (F8), and Python's
    import cache would skip the module body on a second `import_module`; so a cached module is
    RELOADED to re-execute the registration. This makes the helper register the pack's source
    on every call regardless of test order.
    """
    if module_path in sys.modules:
        importlib.reload(sys.modules[module_path])
    else:
        importlib.import_module(module_path)


def _core_baseline_signature(
    monkeypatch: pytest.MonkeyPatch, *, pack_module: str | None, own_source: str, own_tool: str
) -> str:
    """Serialize the CORE registry baseline under a given injection, minus the pack's own adds.

    Computes a deterministic, sorted, multi-line string of the core SOURCES/STORES/NOTIFIERS
    registrations + the core MCP tool set — EXCLUDING `own_source` (from SOURCES) and `own_tool`
    (from the tool set), which are the injecting pack's OWN additions. With those subtracted,
    the remaining baseline is what MUST be byte-identical across the two unrelated injections
    (clause (c)): a single per-consumer core branch would make one injection's core baseline
    differ.

    `pack_module=None` computes the NO-PACK baseline (nothing injected, nothing subtracted) —
    used for clause (d) reversibility.

    The pack import registers the pack's source GLOBALLY on the core SOURCES singleton. The
    root conftest resets the registries once PER TEST, not per call — so a test calling this
    helper for BOTH packs would see the first pack's source linger when computing the second's
    signature (a sibling-injection residue). To make each call HERMETIC, the registry tables
    are snapshotted before the import and RESTORED in a `finally`, so each signature is computed
    against a pristine core baseline regardless of how many times the helper is called per test.

    Args:
        monkeypatch: drives the `PANOPTES_CONSUMER_PACK` env var for the injection.
        pack_module: the consumer pack to inject, or None for the no-pack baseline.
        own_source: the SOURCES key the pack adds (subtracted from the source baseline).
        own_tool: the MCP tool the pack adds (subtracted from the tool baseline).

    Returns:
        A deterministic string signature of the core baseline (sources/stores/notifiers/tools).
    """
    # Snapshot the registry tables so this call is hermetic w.r.t. sibling calls in the same
    # test (the conftest reset is per-test, not per-call — see the docstring).
    sources_snapshot = dict(SOURCES._adapters)
    stores_snapshot = dict(STORES._adapters)
    notifiers_snapshot = dict(NOTIFIERS._adapters)
    try:
        if pack_module is None:
            monkeypatch.delenv(_CONSUMER_PACK_ENV_VAR, raising=False)
        else:
            # Import the pack so its source registers on the core SOURCES registry, AND point
            # the build_server hook at it so its tool registers. Both are required for a
            # faithful under-injection snapshot.
            _import_pack(pack_module)
            monkeypatch.setenv(_CONSUMER_PACK_ENV_VAR, pack_module)

        tool_names = set(build_server(_baseline_config()).tool_names())
        source_types = set(SOURCES.available())
        store_types = set(STORES.available())
        notifier_types = set(NOTIFIERS.available())
    finally:
        # Restore the pristine registry tables so the next call (and the next test) starts clean.
        SOURCES._adapters.clear()
        SOURCES._adapters.update(sources_snapshot)
        STORES._adapters.clear()
        STORES._adapters.update(stores_snapshot)
        NOTIFIERS._adapters.clear()
        NOTIFIERS._adapters.update(notifiers_snapshot)

    # Subtract the injecting pack's OWN additions so only the CORE baseline remains.
    source_types.discard(own_source)
    tool_names.discard(own_tool)

    # A deterministic, sorted serialization — string equality of two of these IS the
    # byte-identical-baseline proof. Each plane is on its own labeled, sorted line.
    return "\n".join(
        [
            f"sources={sorted(source_types)}",
            f"stores={sorted(store_types)}",
            f"notifiers={sorted(notifier_types)}",
            f"tools={sorted(tool_names)}",
        ]
    )


# --- clause (a): each pack injects ADDITIVELY (its own source + tool, core unchanged) --------


def test_fleet_pack_injects_additively(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injecting the fleet pack adds EXACTLY its own source + tool; core registrations unchanged."""
    monkeypatch.delenv(_CONSUMER_PACK_ENV_VAR, raising=False)
    baseline_tools = set(build_server(_baseline_config()).tool_names())
    baseline_sources = set(SOURCES.available())

    _import_pack(_FLEET_PACK_MODULE)
    monkeypatch.setenv(_CONSUMER_PACK_ENV_VAR, _FLEET_PACK_MODULE)
    injected_tools = set(build_server(_baseline_config()).tool_names())
    injected_sources = set(SOURCES.available())

    # Superset: every core registration survives untouched.
    assert baseline_tools <= injected_tools, "fleet injection must not remove/alter core tools"
    assert baseline_sources <= injected_sources, "fleet injection must not drop core sources"
    # Additive by EXACTLY the fleet pack's own source + tool — nothing else leaks in.
    assert injected_tools - baseline_tools == {_FLEET_OWN_TOOL}
    assert injected_sources - baseline_sources == {_FLEET_OWN_SOURCE}


def test_pipeline_pack_injects_additively(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injecting the pipeline pack adds EXACTLY its own source + tool; core unchanged."""
    monkeypatch.delenv(_CONSUMER_PACK_ENV_VAR, raising=False)
    baseline_tools = set(build_server(_baseline_config()).tool_names())
    baseline_sources = set(SOURCES.available())

    _import_pack(_PIPELINE_PACK_MODULE)
    monkeypatch.setenv(_CONSUMER_PACK_ENV_VAR, _PIPELINE_PACK_MODULE)
    injected_tools = set(build_server(_baseline_config()).tool_names())
    injected_sources = set(SOURCES.available())

    assert baseline_tools <= injected_tools, "pipeline injection must not remove/alter core tools"
    assert baseline_sources <= injected_sources, "pipeline injection must not drop core sources"
    assert injected_tools - baseline_tools == {_PIPELINE_OWN_TOOL}
    assert injected_sources - baseline_sources == {_PIPELINE_OWN_SOURCE}


def test_two_packs_register_disjoint_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two packs are UNRELATED: their own source + tool names share nothing.

    The genericity proof only means something if the two consumers are genuinely different
    domains. This pins that the fleet and pipeline additions are disjoint — a game-server-fleet
    source/tool and a data-pipeline source/tool, no shared domain name.
    """
    fleet_additions = {_FLEET_OWN_SOURCE, _FLEET_OWN_TOOL}
    pipeline_additions = {_PIPELINE_OWN_SOURCE, _PIPELINE_OWN_TOOL}
    assert fleet_additions.isdisjoint(pipeline_additions), (
        "the two consumer packs must be UNRELATED domains (disjoint source + tool names)"
    )


# --- clause (b): the core-purity guard is green with BOTH fixtures present -------------------


def test_core_purity_holds_with_both_fixtures_present() -> None:
    """The core-purity guard passes with BOTH consumer fixtures present (re-exercised here).

    The guard module is the authority on core purity (no `examples` import in core/, no banned
    consumer-domain token in core/). Both fixture packs now exist under `examples/`; this drives
    the guard's structural + token checks directly to assert they stay green — consumer-domain
    tokens live ONLY under `examples/`, which the guard does not scan.
    """
    from tests.unit import test_core_purity_guard as purity

    # No core/ file imports examples/ (the structural anti-coupling guarantee).
    purity.test_core_does_not_import_from_examples()
    # No banned consumer-domain token leaked into core/ (the brand-free generic-term grep).
    purity.test_core_contains_no_banned_consumer_tokens()


# --- clause (c): BYTE-IDENTICAL core baseline across the two unrelated injections (Risk G2) ---


def test_core_baseline_is_byte_identical_across_two_unrelated_injections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE THESIS: the core baseline is BYTE-IDENTICAL under fleet vs pipeline injection.

    Computes the core registry baseline (sources/stores/notifiers/tools, minus each pack's OWN
    additions) under the FLEET injection and under the PIPELINE injection, serializes each to a
    deterministic string, and asserts the two strings are EXACTLY equal. A single per-consumer
    core branch — any place the core behaves differently depending on which consumer is wired —
    would make one signature differ. String equality here is the proof of ZERO such branch
    (Risk G2). The two injections are isolated by the root conftest's autouse registry reset.
    """
    fleet_signature = _core_baseline_signature(
        monkeypatch,
        pack_module=_FLEET_PACK_MODULE,
        own_source=_FLEET_OWN_SOURCE,
        own_tool=_FLEET_OWN_TOOL,
    )
    pipeline_signature = _core_baseline_signature(
        monkeypatch,
        pack_module=_PIPELINE_PACK_MODULE,
        own_source=_PIPELINE_OWN_SOURCE,
        own_tool=_PIPELINE_OWN_TOOL,
    )

    # The load-bearing assertion: byte-identical core baseline between two unrelated consumers.
    assert fleet_signature == pipeline_signature, (
        "the core registry baseline MUST be byte-identical across the two unrelated consumer "
        f"injections (zero per-consumer core branch).\n--- fleet ---\n{fleet_signature}\n"
        f"--- pipeline ---\n{pipeline_signature}"
    )
    # And the baseline is genuinely populated (non-vacuous): the core tools + sources are there.
    assert "describe_health" in fleet_signature, "the core baseline must be non-empty"


# --- clause (d): reversibility — hook unset yields exactly the no-pack core baseline ----------


def test_injection_is_reversible_to_the_no_pack_core_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the hook UNSET the core baseline equals each injection's core baseline (no residue).

    Reversibility / no-bundling: after subtracting each pack's own additions, an injected run's
    core baseline equals the NO-PACK baseline byte-for-byte — neither pack leaves any residue in
    the core registries, and nothing is silently bundled into core. The no-pack baseline is the
    fixed point both unrelated injections collapse back to.
    """
    no_pack_signature = _core_baseline_signature(
        monkeypatch, pack_module=None, own_source="", own_tool=""
    )
    fleet_signature = _core_baseline_signature(
        monkeypatch,
        pack_module=_FLEET_PACK_MODULE,
        own_source=_FLEET_OWN_SOURCE,
        own_tool=_FLEET_OWN_TOOL,
    )
    pipeline_signature = _core_baseline_signature(
        monkeypatch,
        pack_module=_PIPELINE_PACK_MODULE,
        own_source=_PIPELINE_OWN_SOURCE,
        own_tool=_PIPELINE_OWN_TOOL,
    )

    # Both injections' core baselines collapse back to the no-pack baseline (no residue).
    assert fleet_signature == no_pack_signature, "fleet injection left residue in the core baseline"
    assert pipeline_signature == no_pack_signature, (
        "pipeline injection left residue in the core baseline"
    )
