"""Boundary guard: ``core/`` must not couple to any consumer.

Two controls, walking the filesystem with ``pathlib`` + ``re`` (NOT by importing
modules — importing would couple the guard to runtime deps):

1. **Primary — structural import check.** No ``core/**/*.py`` may contain
   ``from examples`` / ``import examples``. This is the real anti-coupling
   guarantee: a consumer pack is injected at runtime, never imported by core.
2. **Defense in depth — generic-term grep.** A small frozenset of *generic*
   consumer-domain terms (``allocator``/``matchmaking``/``agones``) must not
   appear in ``core/``. The guard is **brand-free**: it embeds no consumer brand
   literal. ``demo``/``game`` are intentionally NOT banned — ``demo`` is the
   example pack's own name and ``game`` is a common substring; banning them adds
   no protection beyond the structural check and is false-positive prone.

Vacuously green on the Phase 0 skeleton; becomes non-vacuous once the demo pack
lands in Phase 7.
"""

import re
from pathlib import Path

# Repo root is two levels up from this file (tests/unit/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE = _REPO_ROOT / "core"

# Generic consumer-domain terms — NO brand literal (the guard is brand-free).
_BANNED_TOKENS = frozenset({"allocator", "matchmaking", "agones"})

# `from examples ...` / `import examples ...` at any indentation.
_IMPORT_EXAMPLES = re.compile(r"^\s*(?:from|import)\s+examples\b", re.MULTILINE)


def _core_py_files() -> list[Path]:
    return sorted(_CORE.rglob("*.py"))


def test_core_does_not_import_from_examples() -> None:
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in _core_py_files()
        if _IMPORT_EXAMPLES.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"core/ must not import from examples/: {offenders}"


def test_core_contains_no_banned_consumer_tokens() -> None:
    hits: list[str] = []
    for path in _core_py_files():
        text = path.read_text(encoding="utf-8").lower()
        for token in _BANNED_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", text):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{token}")
    assert not hits, f"banned consumer-domain token(s) in core/: {hits}"
