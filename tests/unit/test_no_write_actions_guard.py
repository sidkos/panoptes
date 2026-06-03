"""Boundary guard: no mutating boto3 call sites in ``core/`` or ``examples/``.

Heuristic defense-in-depth (the authoritative no-write control is the read-only
IAM role + read-scoped upstream tokens). Walks the filesystem with ``pathlib`` +
``re`` and matches a **call-shape-anchored snake_case boto3 mutation verb set**.

Design choices (documented per playbook §3.3, Risk R6):

* **snake_case shape only.** boto3 Python call sites are snake_case; the optional
  PascalCase shape is dropped to avoid false positives on read-response field
  names. The shape is ``.<prefix><verb><suffix>(`` with the trailing ``(``
  REQUIRED, so plain identifiers / attribute names / prose never match. The
  ``[a-z_]*`` framing catches mid-name forms (``batch_write_item``,
  ``transact_write_items``).
* **The verb set deliberately OMITS the bare ``register_``/``add_``/``set_``/
  ``run_``/``stop_``/``get_``/``start_``/``tag_``/``remove_`` families** so they
  never collide with (a) the read-only Logs-Insights actions ``start_query`` /
  ``stop_query`` / ``get_query_results`` and (b) Panoptes' own registration
  helpers ``register_tools`` / ``register`` / ``add_tool`` / ``add_source``.
  Those are *never matched in the first place* — they need no suppression.
* **Suppression allowlist = file I/O only.** ``write_text`` / ``write_bytes``
  genuinely contain the matched ``write_`` verb and would match; the allowlist
  suppresses them (the Phase-5 Grafana provider writes JSON to local disk — R6).
  They are the ONLY allowlist members.

Known-miss shapes (explicit blind spots, not silent): raw httpx POST/PUT bodies
(the sentry / http-health sources use httpx, not boto3); paginator-wrapped
mutations; dynamic ``getattr(client, verb)(...)`` dispatch; the low-level
``client.api_call(...)`` escape hatch; and any new AWS verb absent from the set.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = (_REPO_ROOT / "core", _REPO_ROOT / "examples")

# Genuine AWS-mutation verb fragments. Bare register_/add_/set_/run_/stop_/get_/
# start_/tag_/remove_ families are OMITTED on purpose (see module docstring).
_MUTATION_VERBS = (
    "put_",
    "create_",
    "update_",
    "delete_",
    "write_",
    "upload_",
    "copy_",
    "publish",
    "send_message",
    "send_raw_email",
    "invoke",
    "terminate_",
    "reboot_",
    "attach_",
    "detach_",
    "enable_",
    "disable_",
    "untag_",
    "associate_",
    "disassociate_",
    "deregister_",
    "batch_write",
    "transact_write",
    "modify_",
    "set_alarm",
    "start_instances",
    "stop_instances",
    "stop_db_instance",
    "put_object",
    "put_item",
)

# Method-call shape: `.<prefix><verb><suffix>(`. Trailing `(` is required.
_MUTATION_RE = re.compile(r"\.([a-z_]*(?:" + "|".join(_MUTATION_VERBS) + r")[a-z_]*)\(")

# The ONLY suppression-allowlist members: file-I/O verbs containing a matched
# `write_` verb (the Grafana provider writes JSON to disk — Risk R6).
_ALLOWLISTED_METHODS = frozenset({"write_text", "write_bytes"})


def _find_mutation_calls(text: str) -> list[str]:
    """Return matched mutation method names not covered by the allowlist."""
    methods = [str(match.group(1)) for match in _MUTATION_RE.finditer(text)]
    return [method for method in methods if method not in _ALLOWLISTED_METHODS]


def _scanned_py_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_no_mutating_call_sites_in_core_or_examples() -> None:
    offenders: list[str] = []
    for path in _scanned_py_files():
        for method in _find_mutation_calls(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{method}")
    assert not offenders, f"mutating boto3 call site(s) found: {offenders}"


def test_self_test_flags_known_bad_mutations() -> None:
    known_bad = [
        "ddb.put_item(",
        "s3.upload_file(",
        "sns.publish(",
        "dynamodb.batch_write_item(",
        "ec2.terminate_instances(",
        "client.create_table(",
    ]
    for snippet in known_bad:
        assert _find_mutation_calls(snippet), f"matcher should flag: {snippet}"


def test_self_test_file_io_is_matched_then_suppressed() -> None:
    # These contain the matched `write_` verb: the raw pattern matches, but the
    # allowlist suppresses them. They are the only true allowlist members.
    for snippet in ("path.write_text(", "path.write_bytes("):
        assert _MUTATION_RE.search(snippet), f"raw pattern should match: {snippet}"
        assert not _find_mutation_calls(snippet), f"should be suppressed: {snippet}"


def test_self_test_never_matched_by_construction() -> None:
    # The verb set omits the bare start_/stop_/get_/register_/add_ families, so
    # these read-only / registration / file-io forms produce NO match at all
    # (not a vacuous allowlist hit).
    never_matched = [
        "client.stop_query(",
        "client.start_query(",
        "client.get_query_results(",
        "mcp.register_tools(",
        "pack.add_tool(",
        "provider.add_source(",
        "f.writelines(",
        "csv.writer(",
    ]
    for snippet in never_matched:
        assert _MUTATION_RE.search(snippet) is None, f"should NOT match: {snippet}"
