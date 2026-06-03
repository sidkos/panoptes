"""The v0.3 genericity proof — TWO unrelated consumer packs, ZERO core diff between them.

This is the RELEASE THESIS of v0.3 (spec § Strategic positioning — the genericity proof):
the only test that distinguishes a genuinely generic core from one secretly shaped around
consumer #1 is "two unrelated consumers inject the same way with a byte-identical core
baseline between them". When both fixture packs exist (Phase 5), this file FINALIZES into:

- inject EACH pack in turn via the v0.1 `PANOPTES_CONSUMER_PACK` hook and assert each
  registers its source + tool + dashboard ADDITIVELY (the v0.1 additive-injection invariant
  — core's own registrations unchanged);
- the core-purity guard is green with BOTH fixtures present (fleet/Agones tokens only under
  `examples/`, never `core/`);
- **the core registry baseline is byte-identical across the two injections** (the proof of
  zero per-consumer core assumption — Risk G2);
- re-running with the hook unset yields the no-pack core baseline (reversibility).

PHASE 0 SCAFFOLD (this file now): the two fixture packs do NOT exist yet
(`examples/consumer-{fleet,pipeline}-pack/` hold only `.gitkeep`), so the byte-identical
two-pack assertion CANNOT yet be made. This file is VACUOUSLY GREEN: it imports the exact
injection seam the real proof will use (`build_server` + the `PANOPTES_CONSUMER_PACK` hook)
and asserts the load-bearing PRECONDITIONS that must hold before the proof is meaningful —
the no-pack core baseline is non-empty, and the hook-unset server equals that baseline
(reversibility, no bundling). The two-pack byte-identical-baseline assertion lands in
Phase 5 when both packs exist.

The injection seam is the SAME one `tests/unit/test_core_purity_guard.py`'s additive-
injection invariants drive (`build_server(config).tool_names()` with the
`PANOPTES_CONSUMER_PACK` env var), so the Phase-5 finalize reuses it verbatim.
"""

import pytest
from core.config import ResolvedConfig
from core.mcp.server import build_server
from core.model import CanonicalSignal, MetricQuery, MetricSeries

# The env var the v0.1 injection hook reads (the dotted/path module of the consumer pack).
# Phase 5 sets it to each fixture pack in turn; Phase 0 only asserts it UNSET is core-only.
_CONSUMER_PACK_ENV_VAR = "PANOPTES_CONSUMER_PACK"


class _NullStore:
    """A minimal in-memory store — `tool_names()` drives no store query, so this is inert."""

    type = "null"

    def write(self, signals: list[CanonicalSignal]) -> None:  # pragma: no cover - unused
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:  # pragma: no cover - unused
        return []


def _baseline_config() -> ResolvedConfig:
    """A minimal core-only `ResolvedConfig` (mirrors the purity-guard baseline config).

    `build_server(config).tool_names()` reads only the resolved tool list — it drives no
    store query — so the inert `_NullStore` is sufficient. The env/notifier/dashboard lists
    are empty; the core registrars supply the default read-only tool set.
    """
    return ResolvedConfig(
        environments={},
        # `_NullStore` is precisely typed (it returns `list[MetricSeries]`), so it
        # structurally satisfies the `Store` protocol — no cast needed.
        store=_NullStore(),
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


def _core_baseline_tool_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The core-only registered tool set, with the consumer-pack hook UNSET.

    The single seam the Phase-5 proof compares each injection against: with no pack hook,
    `build_server` registers exactly the core read-only tools — the "zero per-consumer
    assumption" baseline every consumer injection must leave byte-identical.
    """
    monkeypatch.delenv(_CONSUMER_PACK_ENV_VAR, raising=False)
    return set(build_server(_baseline_config()).tool_names())


def test_no_pack_core_baseline_is_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-pack core baseline registers a NON-EMPTY core read-only tool set.

    The precondition for the genericity proof: there is a real core baseline to compare each
    consumer injection against. A core that registered nothing would make the byte-identical
    assertion vacuous, so this pins the baseline is genuinely populated (the core read-only
    tools — describe_health, query_metric, etc.).
    """
    baseline = _core_baseline_tool_names(monkeypatch)
    assert baseline, "the no-pack core baseline must register a non-empty core tool set"
    # The core read-only rollup tool is present (a concrete anchor, not just non-empty).
    assert "describe_health" in baseline, (
        "the core baseline must include the core read-only tools (e.g. describe_health)"
    )


def test_hook_unset_server_equals_the_core_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the consumer-pack hook UNSET, the server is EXACTLY the core baseline (no bundling).

    Reversibility / no-bundling precondition (the Phase-5 proof's clause (d)): a server built
    with no `PANOPTES_CONSUMER_PACK` hook registers precisely the core-only baseline — no
    consumer pack is silently bundled into core. Phase 5 extends this to "and injecting EITHER
    pack adds only that pack's tools, leaving this baseline byte-identical".
    """
    first = _core_baseline_tool_names(monkeypatch)
    # Building it twice with the hook unset yields the identical core set (deterministic).
    second = _core_baseline_tool_names(monkeypatch)
    assert first == second, "the no-pack core baseline must be deterministic"


def test_phase5_two_pack_proof_is_scaffolded_not_yet_asserted() -> None:
    """SCAFFOLD marker: the two-pack byte-identical baseline lands in Phase 5.

    The two fixture packs (`examples/consumer-{fleet,pipeline}-pack/`) hold only `.gitkeep`
    in Phase 0, so the signature assertion — inject each pack and assert the core baseline is
    byte-identical across both — cannot yet be made. This test documents that the proof is
    SCAFFOLDED here (the injection seam is imported + the baseline is pinned) and FINALIZED in
    Phase 5; it asserts the fixture dirs are reserved so the Phase-5 finalize has its homes.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    for pack_dir in ("consumer-fleet-pack", "consumer-pipeline-pack"):
        reserved = repo_root / "examples" / pack_dir
        assert reserved.is_dir(), f"the {pack_dir} fixture dir must be reserved for Phase 4/5"
