"""Boundary guard: ``core/`` must not couple to any consumer.

Three controls. The first two walk the filesystem with ``pathlib`` + ``re`` (NOT by
importing modules — importing would couple the guard to runtime deps); the third is
a runtime invariant exercised against the real Phase-7 demo pack:

1. **Primary — structural import check.** No ``core/**/*.py`` may contain
   ``from examples`` / ``import examples``. This is the real anti-coupling
   guarantee: a consumer pack is injected at runtime, never imported by core.
2. **Defense in depth — generic-term grep.** A small frozenset of *generic*
   consumer-domain terms (``allocator``/``matchmaking``/``agones``) must not
   appear in ``core/``. The guard is **brand-free**: it embeds no consumer brand
   literal. ``demo``/``game`` are intentionally NOT banned — ``demo`` is the
   example pack's own name and ``game`` is a common substring; banning them adds
   no protection beyond the structural check and is false-positive prone.
3. **Additive-injection runtime invariant (Phase 7 — proves injection ≠ bundling).**
   Loading the demo pack via the ``PANOPTES_CONSUMER_PACK`` hook is **purely
   additive and reversible**: a server built WITH the hook adds EXACTLY the demo
   tool(s) on top of the core-only baseline, while a server built WITHOUT the hook
   yields precisely that baseline (the core registrations are unchanged). The pack's
   synthetic adapter likewise appears on the core registry only as an ADDITION to a
   baseline snapshot taken before the pack imports (the core registries are module
   singletons, so additivity is asserted at the snapshot/superset level).

Vacuously green on the Phase 0 skeleton; becomes non-vacuous once the demo pack
lands in Phase 7.
"""

import importlib
import re
import sys
from pathlib import Path

import pytest
from core.config import ResolvedConfig
from core.mcp.server import build_server
from core.registry import STORES

# Repo root is two levels up from this file (tests/unit/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE = _REPO_ROOT / "core"

# v0.2 extends both controls to the new distributable surfaces (Terraform module, the
# worked root example, the Helm chart). Brand-neutrality + the structural no-import-of-
# examples guarantee must hold across IaC + chart text exactly as it does for core/.
# These roots hold non-Python text (.tf/.yaml/.tpl/.md), so the scan walks ALL files
# under them (not just *.py) — vacuously green on the Phase-0 skeleton.
_EXTRA_SCAN_ROOTS = (
    _REPO_ROOT / "modules",
    _REPO_ROOT / "deploy",
    _REPO_ROOT / "charts",
)

# Generic consumer-domain terms — NO brand literal (the guard is brand-free).
_BANNED_TOKENS = frozenset({"allocator", "matchmaking", "agones"})

# `from examples ...` / `import examples ...` at any indentation.
_IMPORT_EXAMPLES = re.compile(r"^\s*(?:from|import)\s+examples\b", re.MULTILINE)

# Suffixes of generated/binary artifacts that may land under the extra scan roots
# (e.g. `.terraform/` provider plugins after a local `terraform init`, or `__pycache__`).
# They are not source text and would either be binary-unreadable or carry vendored
# tokens we do not own — excluded from the text scans below.
_SKIP_DIR_NAMES = frozenset({".terraform", "__pycache__", ".git"})

# The dotted path the injection hook imports (the in-repo demo pack).
_PACK_MODULE = "examples.demo-pack.pack"
# The exact tool the demo pack contributes — the only addition a WITH-hook server makes.
_DEMO_TOOL = "get_demo_signal"
# The exact synthetic adapter the demo pack contributes to the STORES registry.
_DEMO_ADAPTER = "demo-synthetic"


def _core_py_files() -> list[Path]:
    return sorted(_CORE.rglob("*.py"))


def _extra_root_text_files() -> list[Path]:
    """Return every source-text file under the v0.2 distributable roots.

    Walks ``modules/``, ``deploy/``, ``charts/`` (each may not yet exist on a partial
    checkout — tolerated) and returns the regular files, skipping generated/vendored
    trees (`.terraform/`, `__pycache__/`, `.git/`). The structural + brand scans below
    read these as UTF-8 text; truly binary files are skipped at read time.
    """
    files: list[Path] = []
    for root in _EXTRA_SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            files.append(path)
    return files


def _read_text_or_empty(path: Path) -> str:
    """Read a file as UTF-8 text; return ``""`` for binary/undecodable content.

    The extra scan roots may contain non-text artifacts; a binary file carries no
    source token we authored, so a decode failure is treated as "nothing to scan".
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _baseline_config() -> ResolvedConfig:
    """A minimal core-only `ResolvedConfig` (no store query is driven by tool_names())."""

    class _NullStore:
        type = "null"

        def write(self, signals: list[object]) -> None:  # pragma: no cover - unused
            return None

        def query(self, query: object) -> list[object]:  # pragma: no cover - unused
            return []

    return ResolvedConfig(
        environments={},
        store=_NullStore(),  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


def test_core_does_not_import_from_examples() -> None:
    # v0.2: the structural no-`examples`-import guarantee extends to the distributable
    # IaC + chart roots. They hold no Python, so the regex matches nothing there —
    # vacuously green on the skeleton, but the scan is wired for future additions.
    scanned = _core_py_files() + _extra_root_text_files()
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in scanned
        if _IMPORT_EXAMPLES.search(_read_text_or_empty(path))
    ]
    assert not offenders, (
        f"core/ + modules/ + deploy/ + charts/ must not import examples/: {offenders}"
    )


def test_core_contains_no_banned_consumer_tokens() -> None:
    # v0.2: the brand-free generic-term grep extends to Terraform + Helm + the worked
    # root example. Brand-neutrality must hold across the distributable surfaces, not
    # only core/ — a leaked `allocator`/`matchmaking`/`agones` token in a chart value
    # or a tfvars comment is just as much a consumer-coupling leak.
    hits: list[str] = []
    for path in _core_py_files() + _extra_root_text_files():
        text = _read_text_or_empty(path).lower()
        for token in _BANNED_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", text):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{token}")
    assert not hits, (
        f"banned consumer-domain token(s) in core/ + modules/ + deploy/ + charts/: {hits}"
    )


def test_no_hook_server_equals_core_only_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the hook UNSET, the server registers exactly the core-only tool set."""
    monkeypatch.delenv("PANOPTES_CONSUMER_PACK", raising=False)
    baseline_tools = set(build_server(_baseline_config()).tool_names())
    assert _DEMO_TOOL not in baseline_tools, (
        "without the injection hook the demo tool must NOT be present (no bundling)"
    )


def test_injection_is_purely_additive_at_the_tool_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WITH-hook server adds EXACTLY the demo tool on top of the no-hook baseline."""
    monkeypatch.delenv("PANOPTES_CONSUMER_PACK", raising=False)
    baseline_tools = set(build_server(_baseline_config()).tool_names())

    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", _PACK_MODULE)
    injected_tools = set(build_server(_baseline_config()).tool_names())

    # Superset: every core tool survives untouched (injection changes nothing of core's).
    assert baseline_tools <= injected_tools, "injection must not remove/alter core tools"
    # Additive by exactly the demo tool — nothing more leaks in, nothing core is lost.
    assert injected_tools - baseline_tools == {_DEMO_TOOL}, (
        "the injected pack must add EXACTLY get_demo_signal (purely additive)"
    )


def test_synthetic_adapter_is_an_addition_to_a_pre_import_baseline() -> None:
    """The pack's synthetic adapter appears on the core registry only as an ADDITION."""
    baseline_adapters = set(STORES.available())
    # Reload if already cached so the `@STORES.register(...)` decorator re-runs (the root
    # conftest rolls back the registration after every test, and Python's import cache
    # would otherwise skip the module body on a second import — F8).
    if _PACK_MODULE in sys.modules:
        importlib.reload(sys.modules[_PACK_MODULE])
    else:
        importlib.import_module(_PACK_MODULE)
    after_adapters = set(STORES.available())

    assert baseline_adapters <= after_adapters, "importing the pack must not drop core adapters"
    # The pack adds its synthetic adapter as a GENUINE addition to the pre-import baseline.
    # The root conftest's per-test registry reset guarantees `demo-synthetic` is NOT
    # pre-registered, so the strict additive form holds regardless of test order (F8 —
    # the prior `... or _DEMO_ADAPTER in baseline_adapters` escape silently weakened to a
    # no-op whenever a sibling test imported the pack first).
    assert _DEMO_ADAPTER in after_adapters - baseline_adapters, (
        "the synthetic adapter must appear ONLY as an addition to the pre-import baseline"
    )
