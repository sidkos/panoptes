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
* **Path-agnostic suppression allowlist = file I/O only.** ``write_text`` /
  ``write_bytes`` genuinely contain the matched ``write_`` verb and would match; the
  allowlist suppresses them EVERYWHERE (the Phase-5 Grafana provider writes JSON to
  local disk — R6). They are the only path-agnostic allowlist members.
* **Path-SCOPED suppression (v0.2) = ``sns.publish`` in ``core/notifiers/sns.py`` ONLY.**
  ``publish`` is in the mutation-verb set, but the ``sns`` notifier publishes to a
  Panoptes-OWNED alert topic — its own channel, NOT an observed system (spec § New
  notifier adapters, the CRITICAL no-write-guard note). The suppression is therefore
  PATH-KEYED to ``core/notifiers/sns.py``, so a ``publish`` anywhere ELSE (esp. any
  ``core/sources/`` file) STILL red-bars. The guard's intent is "no writes to OBSERVED
  systems"; the alert channel is exempt, by exact file path, with a comment.

Known-miss shapes (explicit blind spots, not silent): raw httpx POST/PUT bodies
(the sentry / http-health / slack-notifier adapters use httpx, not boto3 — the slack
webhook is an alert sink, not an observed write); paginator-wrapped mutations; dynamic
``getattr(client, verb)(...)`` dispatch; the low-level ``client.api_call(...)`` escape
hatch; and any new AWS verb absent from the set.
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

# Path-AGNOSTIC suppression-allowlist members: file-I/O verbs containing a matched
# `write_` verb (the Grafana provider writes JSON to disk — Risk R6). Suppressed in EVERY
# scanned file.
_ALLOWLISTED_METHODS = frozenset({"write_text", "write_bytes"})

# Path-SCOPED suppression (v0.2): a `(repo-relative posix path, method)` pair is suppressed
# ONLY in that exact file. `sns.publish` is a legitimate write to Panoptes' OWN alert topic
# (spec § New notifier adapters — the CRITICAL note), so it is exempt ONLY in
# `core/notifiers/sns.py`; a `publish(` anywhere else (esp. `core/sources/`) still red-bars.
# This is intentionally NOT a blanket `publish` allowlist — the file path is the scope.
_PATH_SCOPED_ALLOW: frozenset[tuple[str, str]] = frozenset({("core/notifiers/sns.py", "publish")})


def _find_mutation_calls(text: str, rel_path: str = "") -> list[str]:
    """Return matched mutation method names not covered by either allowlist.

    Args:
        text: The file/snippet text to scan for the call-shape-anchored mutation verbs.
        rel_path: The file's repo-relative POSIX path (e.g. ``core/notifiers/sns.py``),
            used to apply the PATH-SCOPED suppression. Defaults to ``""`` so the
            snippet-only self-tests (which pass no path) get NO path-scoped suppression —
            a `publish(` snippet with no attributed path is still flagged, which is exactly
            what the negative self-test asserts.

    A matched method is suppressed when EITHER (a) it is a path-agnostic file-I/O verb
    (`write_text`/`write_bytes`), OR (b) the `(rel_path, method)` pair is in the
    path-scoped allowlist. Everything else is returned as an offender.
    """
    offenders: list[str] = []
    for match in _MUTATION_RE.finditer(text):
        method = str(match.group(1))
        if method in _ALLOWLISTED_METHODS:
            continue  # path-agnostic file-I/O suppression (every file).
        if (rel_path, method) in _PATH_SCOPED_ALLOW:
            continue  # path-scoped suppression (this exact file only).
        offenders.append(method)
    return offenders


def _scanned_py_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_no_mutating_call_sites_in_core_or_examples() -> None:
    offenders: list[str] = []
    for path in _scanned_py_files():
        # The repo-relative POSIX path keys the path-scoped allowlist (sns.publish is
        # exempt ONLY in core/notifiers/sns.py); `as_posix()` keeps the separator stable
        # across platforms so the allowlist literal matches on every OS.
        rel_path = path.relative_to(_REPO_ROOT).as_posix()
        for method in _find_mutation_calls(path.read_text(encoding="utf-8"), rel_path):
            offenders.append(f"{rel_path}:{method}")
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


# --- v0.2: path-scoped sns.publish suppression (load-bearing) --------------------


def test_path_scoped_publish_in_sources_is_still_flagged() -> None:
    """(a) `sns.publish(` attributed to a `core/sources/…` path is STILL flagged.

    The negative case is the whole point of making the suppression path-keyed: a
    `publish` to an OBSERVED system (anywhere outside `core/notifiers/sns.py`) must
    red-bar. A `core/sources/` path gets no path-scoped suppression.
    """
    offenders = _find_mutation_calls("sns.publish(", "core/sources/evil.py")
    assert "publish" in offenders, "publish in core/sources/ must NOT be suppressed"


def test_path_scoped_publish_in_notifiers_sns_is_suppressed() -> None:
    """(b) The real `core/notifiers/sns.py` `publish` IS suppressed by the path scope."""
    offenders = _find_mutation_calls("self._sns().publish(", "core/notifiers/sns.py")
    assert offenders == [], "sns.publish in core/notifiers/sns.py must be suppressed"


def test_path_scoped_publish_with_no_path_is_still_flagged() -> None:
    """A `publish(` snippet with NO attributed path (the default) is still flagged.

    The snippet-only self-tests pass no path, so an empty `rel_path` must never grant
    the path-scoped suppression — only the exact `core/notifiers/sns.py` path does.
    """
    assert _find_mutation_calls("sns.publish(") == ["publish"]


def test_path_scoped_publish_in_other_notifier_file_is_still_flagged() -> None:
    """`publish` in a DIFFERENT notifier file (not sns.py) still red-bars.

    The scope is the exact file `core/notifiers/sns.py` — not the whole `core/notifiers/`
    directory — so a stray `publish` in `core/notifiers/slack.py` (or any other) is
    flagged, proving the allowlist is file-path-keyed, not directory-keyed.
    """
    offenders = _find_mutation_calls("client.publish(", "core/notifiers/slack.py")
    assert "publish" in offenders, "publish outside sns.py must NOT be suppressed"


def test_real_sns_notifier_file_is_clean_under_the_path_scope() -> None:
    """The real shipped `core/notifiers/sns.py` passes the guard via the path scope.

    Reads the ACTUAL file (not a synthetic snippet) and asserts the only mutation match
    it contains — `publish` — is suppressed because its path is the allowlisted one. If a
    future edit adds a NON-publish mutation verb to that file, this red-bars (the scope is
    `publish` only, not a blanket file exemption).
    """
    sns_path = _REPO_ROOT / "core" / "notifiers" / "sns.py"
    rel_path = sns_path.relative_to(_REPO_ROOT).as_posix()
    offenders = _find_mutation_calls(sns_path.read_text(encoding="utf-8"), rel_path)
    assert offenders == [], f"core/notifiers/sns.py must be guard-clean; got {offenders}"
