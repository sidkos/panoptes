"""Hermetic Terraform gate: ``modules/stack`` must ``init -backend=false`` + ``validate``.

This is the Phase-0 red-test-first artifact for the IaC surface. It runs the static,
offline Terraform pipeline — NO live AWS creds, NO backend, NO ``plan`` — and asserts a
clean exit. The real EKS / IRSA / ingress resources land in Phase 6; on the Phase-0
skeleton the assertions are vacuously satisfied by a minimal valid module (a couple of
``variable`` blocks + a ``null_resource`` placeholder), but the gate is wired NOW so the
rest of v0.2 can extend it.

Binary gating (Risk K2): the whole module is marked ``pytest.mark.terraform`` so the
default unit run (``-m "not integration and not terraform and not helm"``) deselects it
when the ``terraform`` binary is absent, while the CI ``terraform`` job installs the binary
and runs ``-m terraform``. Per the spec we NEVER ``pytest.skip`` inside a test body — a
skip would mask a genuinely broken module behind a green-looking run. The single binary
presence check below converts "terraform missing while explicitly selected" into a clear
failure instead of a silent skip, so selecting ``-m terraform`` without the tool red-bars.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

# Gate the whole module on the `terraform` marker so the base unit run deselects it
# (the binary may be absent locally); the CI `terraform` job installs the binary and
# selects `-m terraform`.
pytestmark = pytest.mark.terraform

# Repo root is two levels up from this file (tests/terraform/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "modules" / "stack"

# A no-backend init+validate stays fully offline: no state, no provider auth, no plan.
_INIT_COMMAND = ("terraform", f"-chdir={_MODULE_DIR}", "init", "-backend=false", "-input=false")
_VALIDATE_COMMAND = ("terraform", f"-chdir={_MODULE_DIR}", "validate")
_FMT_CHECK_COMMAND = ("terraform", f"-chdir={_MODULE_DIR}", "fmt", "-check", "-recursive")


def _run(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run a terraform subcommand, capturing combined stdout/stderr as text.

    Args:
        command: The fully-resolved argv (terraform + chdir + subcommand + flags).

    Returns:
        The completed process; the caller asserts on ``returncode`` and surfaces
        ``stdout``/``stderr`` verbatim on failure so a broken module is diagnosable
        from the test output alone.
    """
    # The argv is a module-level literal tuple (no shell, no caller/untrusted input),
    # so there is no injection surface here.
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


def test_terraform_binary_is_present_when_selected() -> None:
    """The ``terraform`` binary must exist whenever this marked module is selected.

    Asserts the IaC toolchain is installed rather than silently skipping. Selecting
    ``-m terraform`` without the binary is an operator/CI configuration error and must
    red-bar, not produce a false green.

    Steps:
        1. Resolve ``terraform`` on ``PATH``.
        2. Assert it is found (a clear failure message names the missing tool).
    """
    assert shutil.which("terraform") is not None, (
        "the `terraform` binary is required to run the `-m terraform` gate; install it "
        "via `hashicorp/setup-terraform` in CI or `brew install terraform` locally"
    )


def test_module_validates_offline() -> None:
    """``modules/stack`` initialises (no backend) and validates with exit 0.

    The hermetic IaC contract (spec § CI/CD, Risk K3): assert on the static rendered
    config, never a live ``plan`` (which would need creds CI must not hold).

    Steps:
        1. ``terraform -chdir=modules/stack init -backend=false -input=false`` → exit 0.
        2. ``terraform -chdir=modules/stack validate`` → exit 0.
    """
    init_result = _run(_INIT_COMMAND)
    assert init_result.returncode == 0, (
        f"`terraform init -backend=false` failed (exit {init_result.returncode}):\n"
        f"{init_result.stdout}\n{init_result.stderr}"
    )

    validate_result = _run(_VALIDATE_COMMAND)
    assert validate_result.returncode == 0, (
        f"`terraform validate` failed (exit {validate_result.returncode}):\n"
        f"{validate_result.stdout}\n{validate_result.stderr}"
    )


def test_module_is_fmt_clean() -> None:
    """``modules/stack`` is ``terraform fmt``-clean (no whitespace/style drift).

    Mirrors the CI ``terraform`` job's ``fmt -check -recursive`` step so a local run
    catches formatting drift before the push.

    Steps:
        1. ``terraform -chdir=modules/stack fmt -check -recursive`` → exit 0.
    """
    fmt_result = _run(_FMT_CHECK_COMMAND)
    assert fmt_result.returncode == 0, (
        f"`terraform fmt -check` found unformatted files (exit {fmt_result.returncode}):\n"
        f"{fmt_result.stdout}\n{fmt_result.stderr}\n"
        "run `terraform -chdir=modules/stack fmt -recursive` to fix"
    )
