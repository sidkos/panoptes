"""Unit tests for the v0.2 `git`-injection dashboard path (`core/dashboards/grafana.py`).

The hosted path adds the `git` consumer-pack injection variant (DASHBOARDS §4, IAM §C):
the consumer's dashboards (+ optional `pack.py`) are pulled from a git repo at DEPLOY TIME
(a Terraform null_resource / Helm pre-install job does the read-only `git fetch`), NOT
in the running pod. The Grafana provider's job here is to VALIDATE the ref + consume the
fetched path read-only — the pinned ref is a code-execution trust boundary, so:

- a full 40-hex commit SHA is REQUIRED (control (a) — content-addressed integrity);
- a MUTABLE branch ref (`main`/`HEAD`/a short ref) is REJECTED with a clear error (pulling
  a moving branch would be arbitrary-code-execution-on-deploy);
- the fetched path is consumed READ-ONLY (the provider never writes into it; it globs the
  fetched `dashboards/**/dashboard.json` exactly as the mounted-`path` case does).

These run in the base gate (no network, no real git fetch): the deploy-time fetch is
mocked by pointing the git pack's `json_path` at a `tmp_path` dir the test pre-populates,
simulating "the deploy already fetched the subdir to this path". The provider's ref
VALIDATION (the security control) is what is under test.
"""

import json
from pathlib import Path

import pytest
from core.dashboards.grafana import GrafanaDashboardProvider
from core.errors import CapabilityError
from core.model import DashboardPack

# A full 40-hex commit SHA (the only accepted ref form).
_VALID_SHA = "a" * 40
_ANOTHER_VALID_SHA = "0123456789abcdef0123456789abcdef01234567"

# Mutable / disallowed refs the provider must REJECT. Beyond the obvious mutable branches
# and wrong-length refs, the trailing block pins the adversarial BYPASS CLASSES the
# `\A...\Z`-anchored full-SHA regex already rejects — so a future regex weakening (e.g.
# dropping the anchors, or `re.match` without `\Z`, or adding `re.IGNORECASE`) red-bars here:
#   - uppercase 40-hex: the SHA class is lowercase-only, so `A*40` must fail (no IGNORECASE);
#   - path-traversal: a `../`-prefixed ref must never pass (it is not 40-hex);
#   - newline smuggling: `\A...\Z` (not `^...$`) means a trailing/leading newline + a smuggled
#     `main` cannot satisfy the anchors — `re.match` alone (no `\Z`) would accept `<sha>\nmain`.
_MUTABLE_REFS = (
    "main",
    "HEAD",
    "master",
    "develop",
    "v1.2.3",
    "abc1234",
    "a" * 39,
    "a" * 41,
    # Bypass classes (explicitly pinned — the anchored, lowercase-only regex rejects each):
    "A" * 40,  # uppercase 40-hex — rejected (no IGNORECASE; the class is [0-9a-f])
    "../" + "a" * 37,  # path-traversal ref — not 40-hex, rejected
    "a" * 40 + "\nmain",  # newline-smuggled branch AFTER a valid SHA — `\Z` rejects it
    "main\n" + "a" * 40,  # newline-smuggled branch BEFORE a valid SHA — `\A` rejects it
    "a" * 40 + "\n",  # trailing newline after a valid SHA — `\Z` (not `$`) rejects it
)


def _provider(provisioning_dir: Path) -> GrafanaDashboardProvider:
    return GrafanaDashboardProvider({"provisioning_dir": str(provisioning_dir)})


def _git_pack(json_path: Path, ref: str) -> DashboardPack:
    """A git-selected consumer pack whose `json_path` is the deploy-fetched subdir.

    `json_path` models "the deploy-time fetch already placed the subdir here"; `git_ref`
    is the pinned ref the provider validates (full SHA required, mutable branch rejected).
    """
    return DashboardPack(
        id="consumer", tier="consumer", json_path=json_path, selector="git", git_ref=ref
    )


def _fetched_subdir_with_dashboard(root: Path) -> Path:
    """Pre-populate a `tmp_path` dir as if the deploy-time fetch placed a dashboard there."""
    dashboard = root / "dashboards" / "ops" / "dashboard.json"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text(
        json.dumps(
            {
                "title": "consumer-ops",
                "templating": {"list": [{"name": "env"}]},
                "panels": [{"targets": [{"expr": 'panoptes_health_up{env="$env"}'}]}],
            }
        ),
        encoding="utf-8",
    )
    return root


# --- rejection of mutable / malformed refs ----------------------------------------


@pytest.mark.parametrize("mutable_ref", _MUTABLE_REFS)
def test_git_pack_with_mutable_ref_is_rejected(mutable_ref: str, tmp_path: Path) -> None:
    """A git pack pinned to a mutable/short ref is rejected with a clear error.

    Pulling a moving branch (or a non-full-SHA ref) is arbitrary-code-execution-on-deploy
    (DASHBOARDS §4 control (a)) — the provider must refuse it, naming the SHA-pin requirement.
    """
    fetched = _fetched_subdir_with_dashboard(tmp_path / "fetched")
    pack = _git_pack(fetched, mutable_ref)
    with pytest.raises(CapabilityError) as excinfo:
        _provider(tmp_path / "provisioning").provision([pack])
    message = str(excinfo.value).lower()
    # The error names the immutable-pin requirement so the operator fix is obvious.
    assert "sha" in message or "immutable" in message or "commit" in message


# --- acceptance of a full SHA + read-only consumption ------------------------------


def test_git_pack_with_full_sha_is_accepted_and_provisioned(tmp_path: Path) -> None:
    """A git pack pinned to a full 40-hex SHA is accepted and its dashboards provisioned."""
    fetched = _fetched_subdir_with_dashboard(tmp_path / "fetched")
    pack = _git_pack(fetched, _VALID_SHA)
    provisioning_dir = tmp_path / "provisioning"
    _provider(provisioning_dir).provision([pack])
    # The fetched dashboard was globbed + provisioned (the same path the mounted-dir case uses).
    synced = list(provisioning_dir.rglob("*.json"))
    assert synced, "a full-SHA git pack's dashboards must be provisioned"
    assert any("ops" in path.name or "consumer" in path.name for path in synced)


def test_git_pack_fetch_path_is_consumed_read_only(tmp_path: Path) -> None:
    """The provider consumes the fetched path READ-ONLY — it never writes into it.

    Provisioning copies the fetched JSON into the (separate) provisioning dir; the fetched
    subdir's contents must be byte-identical before and after (no in-place mutation of the
    deploy-fetched tree).
    """
    fetched = _fetched_subdir_with_dashboard(tmp_path / "fetched")
    source_dashboard = fetched / "dashboards" / "ops" / "dashboard.json"
    before = source_dashboard.read_text(encoding="utf-8")
    before_files = sorted(p.relative_to(fetched).as_posix() for p in fetched.rglob("*"))

    _provider(tmp_path / "provisioning").provision([_git_pack(fetched, _VALID_SHA)])

    # The fetched tree is untouched (read-only consumption): same content + same file set.
    assert source_dashboard.read_text(encoding="utf-8") == before
    after_files = sorted(p.relative_to(fetched).as_posix() for p in fetched.rglob("*"))
    assert after_files == before_files, "the provider must not write into the fetched path"


def test_second_valid_sha_also_accepted(tmp_path: Path) -> None:
    """A different full-SHA pin is also accepted (the check is the SHA shape, not a literal)."""
    fetched = _fetched_subdir_with_dashboard(tmp_path / "fetched")
    provisioning_dir = tmp_path / "provisioning"
    _provider(provisioning_dir).provision([_git_pack(fetched, _ANOTHER_VALID_SHA)])
    assert list(provisioning_dir.rglob("*.json")), "a second full-SHA pin must provision too"


# --- the `path` selector stays unchanged ------------------------------------------


def test_path_selector_still_provisions_unchanged(tmp_path: Path) -> None:
    """The v0.1 `path` selector is unchanged — a mounted-dir pack still globs + provisions."""
    fetched = _fetched_subdir_with_dashboard(tmp_path / "mounted")
    path_pack = DashboardPack(id="consumer", tier="consumer", json_path=fetched, selector="path")
    provisioning_dir = tmp_path / "provisioning"
    _provider(provisioning_dir).provision([path_pack])
    assert list(provisioning_dir.rglob("*.json")), "the path selector must still provision"
