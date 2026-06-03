"""Hermetic Helm gate: ``charts/panoptes`` lints, templates, and ``kubeconform``-validates.

Phase-0 red-test-first artifact for the in-cluster deploy surface. The pipeline is fully
offline — ``helm lint`` + ``helm template`` (no cluster) piped to ``kubeconform -strict``
(manifest *schema* validation against the bundled K8s API schemas, NO ``kubectl apply
--dry-run=server`` which would need a live cluster + creds — spec § CI/CD, Risk K3).

On the Phase-0 skeleton the chart renders a single trivial ServiceAccount; the rich
workloads (VM StatefulSet, Grafana/collector/MCP/oauth2-proxy Deployments, the
nginx-forward-auth Ingress) and their manifest-shape assertions land in Phase 7. The
render-and-schema-validate gate is wired NOW so those assertions can extend it.

Binary gating (Risk K2): the whole module is marked ``pytest.mark.helm`` so the default
unit run (``-m "not integration and not terraform and not helm"``) deselects it when
``helm``/``kubeconform`` are absent; the CI ``helm`` job installs both and selects
``-m helm``. We NEVER ``pytest.skip`` in a body (a skip would mask a broken chart); the
explicit presence check below converts "binary missing while selected" into a clear
failure instead of a silent skip.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

# Gate the whole module on the `helm` marker so the base unit run deselects it (the
# binaries may be absent locally); the CI `helm` job installs them and selects `-m helm`.
pytestmark = pytest.mark.helm

# Repo root is two levels up from this file (tests/helm/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "charts" / "panoptes"
_CI_VALUES = _CHART_DIR / "ci" / "test-values.yaml"

_LINT_COMMAND = ("helm", "lint", str(_CHART_DIR))
_TEMPLATE_COMMAND = ("helm", "template", str(_CHART_DIR), "-f", str(_CI_VALUES))
# `-strict` rejects unknown/extra fields; `-summary` prints the resource tally. The
# rendered manifests are fed on stdin (the CI job mirrors this exact pipe).
_KUBECONFORM_COMMAND = ("kubeconform", "-strict", "-summary")


def _run(
    command: tuple[str, ...], *, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a helm/kubeconform subcommand, capturing combined output as text.

    Args:
        command: The fully-resolved argv.
        stdin_text: Optional text piped to the process stdin (the rendered manifests
            for ``kubeconform``).

    Returns:
        The completed process; the caller asserts on ``returncode`` and surfaces the
        captured output verbatim on failure.
    """
    # The argv is a module-level literal tuple (no shell, no caller/untrusted input),
    # so there is no injection surface here.
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        input=stdin_text,
        check=False,
    )


def test_helm_and_kubeconform_binaries_are_present_when_selected() -> None:
    """Both ``helm`` and ``kubeconform`` must exist whenever this marked module runs.

    Asserts the toolchain is installed rather than silently skipping. Selecting
    ``-m helm`` without a binary is a configuration error and must red-bar.

    Steps:
        1. Resolve ``helm`` and ``kubeconform`` on ``PATH``.
        2. Assert each is found (the message names the missing tool).
    """
    assert shutil.which("helm") is not None, (
        "the `helm` binary is required for the `-m helm` gate; install via "
        "`azure/setup-helm` in CI or `brew install helm` locally"
    )
    assert shutil.which("kubeconform") is not None, (
        "the `kubeconform` binary is required for the `-m helm` gate; install via the "
        "pinned download in CI or `brew install kubeconform` locally"
    )


def test_chart_lints() -> None:
    """``helm lint charts/panoptes`` passes (exit 0).

    Steps:
        1. Run ``helm lint`` against the chart directory.
        2. Assert exit 0; surface lint output on failure.
    """
    lint_result = _run(_LINT_COMMAND)
    assert lint_result.returncode == 0, (
        f"`helm lint` failed (exit {lint_result.returncode}):\n"
        f"{lint_result.stdout}\n{lint_result.stderr}"
    )


def test_chart_renders_and_passes_kubeconform() -> None:
    """The chart templates offline and the manifests pass ``kubeconform -strict``.

    The hermetic Helm contract (spec § CI/CD, Risk K3): ``helm template`` (no cluster)
    piped to ``kubeconform -strict`` (offline schema validation), never a live
    ``kubectl apply --dry-run=server``.

    Steps:
        1. ``helm template charts/panoptes -f ci/test-values.yaml`` → exit 0; capture
           the rendered manifests.
        2. Pipe the manifests to ``kubeconform -strict -summary`` → exit 0.
    """
    template_result = _run(_TEMPLATE_COMMAND)
    assert template_result.returncode == 0, (
        f"`helm template` failed (exit {template_result.returncode}):\n"
        f"{template_result.stdout}\n{template_result.stderr}"
    )
    rendered_manifests = template_result.stdout
    assert rendered_manifests.strip(), "`helm template` produced no manifests to validate"

    kubeconform_result = _run(_KUBECONFORM_COMMAND, stdin_text=rendered_manifests)
    assert kubeconform_result.returncode == 0, (
        f"`kubeconform -strict` rejected the rendered manifests "
        f"(exit {kubeconform_result.returncode}):\n"
        f"{kubeconform_result.stdout}\n{kubeconform_result.stderr}"
    )
