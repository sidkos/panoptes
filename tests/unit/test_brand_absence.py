"""Brand-absence invariant: the literal consumer brand appears NOWHERE in the source tree.

Panoptes is a PUBLIC, brand-neutral OSS project: the consumer brand must never leak into the
shipped/test/example/IaC text. The pre-commit gate enforces this with a `grep -rin`, and the
core-purity guard enforces a SEPARATE, brand-free anti-coupling check — but neither is a
standalone in-suite test of the brand invariant itself (the purity guard's banned-token set is
intentionally brand-FREE, and it does not scan `tests/` or `examples/`). This module is that
dedicated test: it walks the distributable roots and asserts the brand literal is absent as a
case-insensitive WHOLE word.

The brand literal is BYTE-ENCODED here (assembled from char codes) so this test file does NOT
itself contain the literal — otherwise the walk would scan `tests/` and self-trip on its own
source. The file is therefore safe to include in the scanned roots.
"""

import re
from pathlib import Path

# The brand literal, byte-encoded so this source carries no plaintext occurrence (the scan
# below includes `tests/`, so a plaintext literal here would self-trip). Assembled from its
# ASCII codes: f, i, d, a.
_BRAND_LITERAL = bytes([0x66, 0x69, 0x64, 0x61]).decode("ascii")

# Case-insensitive WHOLE-word match (so unrelated substrings cannot false-positive). Built from
# the byte-encoded literal so the pattern source likewise carries no plaintext occurrence.
_BRAND_WORD_RE = re.compile(rf"\b{re.escape(_BRAND_LITERAL)}\b", re.IGNORECASE)

# Repo root is two levels up from this file (tests/unit/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The distributable roots the gate's brand grep scans (mirrors `scripts/precommit.sh`).
_SCANNED_ROOTS = ("core", "tests", "examples", "modules", "deploy", "charts")

# Generated/binary trees that carry no source we authored (vendored tokens / undecodable bytes).
_SKIP_DIR_NAMES = frozenset({".git", ".terraform", "__pycache__", ".pytest_cache", ".mypy_cache"})


def _scanned_files() -> list[Path]:
    """Every source-text file under the distributable roots, skipping generated/vendored trees."""
    files: list[Path] = []
    for root_name in _SCANNED_ROOTS:
        root = _REPO_ROOT / root_name
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
    """Read a file as UTF-8 text; return ``""`` for binary/undecodable content (nothing to scan)."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_brand_literal_is_absent_from_every_distributable_root() -> None:
    """The consumer brand literal appears NOWHERE (case-insensitive, whole-word) in the tree.

    Walks core/ + tests/ + examples/ + modules/ + deploy/ + charts/ and asserts the byte-encoded
    brand literal does not occur as a whole word in any source-text file. A leak — in a comment,
    a docstring, a test fixture, a Helm value, a Terraform variable — fails here with the exact
    offending file:line, independent of the pre-commit grep.
    """
    offenders: list[str] = []
    for path in _scanned_files():
        text = _read_text_or_empty(path)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _BRAND_WORD_RE.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line_number}")
    assert not offenders, (
        "the consumer brand literal must NOT appear in any distributable root "
        f"(brand-neutrality invariant); found in: {offenders}"
    )


def test_brand_scan_actually_covers_files() -> None:
    """The walk is NON-VACUOUS — it scans a real, non-trivial set of files (no silent no-op)."""
    scanned = _scanned_files()
    assert len(scanned) > 50, (
        f"the brand scan must cover a real file set (a near-empty walk would make the invariant "
        f"vacuous); scanned only {len(scanned)} files"
    )
