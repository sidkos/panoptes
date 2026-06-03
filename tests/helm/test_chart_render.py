"""Hermetic Helm gate: ``charts/panoptes`` lints, templates, kubeconform-validates + the
manifest-shape SECURITY assertions (spec § CI/CD helm job, § MCP HTTP face).

The pipeline is fully offline — ``helm lint`` + ``helm template`` (no cluster) piped to
``kubeconform -strict`` (manifest schema validation, NO ``kubectl apply --dry-run=server``,
Risk K3). Beyond schema validity, this FINALIZED test parses the rendered manifests and
asserts the load-bearing security invariants:

- the MCP Service is ``type: ClusterIP`` (NEVER LoadBalancer/NodePort) — THE anonymous-
  bypass guard: a LB/NodePort MCP Service would be an anonymous bypass of the GitHub gate;
- the collector + MCP ServiceAccounts carry the IRSA ``eks.amazonaws.com/role-arn``
  annotation (their pods assume the SA-scoped role);
- the Ingress carries the nginx forward-auth annotations (``auth-url`` + ``auth-signin`` →
  oauth2-proxy) + a cert-manager TLS annotation, and ``/healthz`` is an UNAUTHENTICATED
  path (exempt from forward-auth);
- the VictoriaMetrics StatefulSet has EXACTLY one replica + a PVC;
- no workload mounts a write-capable observed credential.

Binary gating (Risk K2): the whole module is ``pytest.mark.helm`` so the default unit run
deselects it; the CI ``helm`` job installs helm + kubeconform and selects ``-m helm``. We
NEVER ``pytest.skip`` in a body — a missing binary while selected red-bars.
"""

import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

# Gate the whole module on the `helm` marker so the base unit run deselects it (the
# binaries may be absent locally); the CI `helm` job installs them and selects `-m helm`.
pytestmark = pytest.mark.helm

# Repo root is two levels up from this file (tests/helm/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "charts" / "panoptes"
_CI_VALUES = _CHART_DIR / "ci" / "test-values.yaml"

# `helm lint` is run WITH the CI fixture values: the values.schema.json now REQUIRES a
# non-empty `oauth2Proxy.githubOrg` (the fail-closed GitHub gate — an empty org disables the
# allowlist). The default values.yaml deliberately ships `githubOrg: ""` so a deploy MUST
# supply it; linting the chart against a valid install (the CI fixture) is the right
# hermetic check. (The empty-org-fails case is asserted by a dedicated negative test.)
_LINT_COMMAND = ("helm", "lint", str(_CHART_DIR), "-f", str(_CI_VALUES))
_TEMPLATE_COMMAND = ("helm", "template", str(_CHART_DIR), "-f", str(_CI_VALUES))
# `-strict` rejects unknown/extra fields; `-summary` prints the resource tally. The
# rendered manifests are fed on stdin (the CI job mirrors this exact pipe).
_KUBECONFORM_COMMAND = ("kubeconform", "-strict", "-summary")

# The IRSA SA annotation key the collector + MCP SAs must carry.
_IRSA_ANNOTATION = "eks.amazonaws.com/role-arn"
# The nginx forward-auth + cert-manager ingress annotation keys.
_AUTH_URL_ANNOTATION = "nginx.ingress.kubernetes.io/auth-url"
_AUTH_SIGNIN_ANNOTATION = "nginx.ingress.kubernetes.io/auth-signin"
_CERT_MANAGER_ANNOTATION = "cert-manager.io/cluster-issuer"


def _run(
    command: tuple[str, ...], *, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a helm/kubeconform subcommand, capturing combined output as text."""
    # The argv is a module-level literal tuple (no shell, no caller/untrusted input),
    # so there is no injection surface here.
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        input=stdin_text,
        check=False,
    )


def _rendered_manifests() -> str:
    """`helm template -f ci/test-values.yaml` → the rendered manifest YAML (asserts exit 0)."""
    result = _run(_TEMPLATE_COMMAND)
    assert result.returncode == 0, (
        f"`helm template` failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    assert result.stdout.strip(), "`helm template` produced no manifests"
    return result.stdout


def _parse_documents(rendered: str) -> list[dict[str, object]]:
    """Parse the multi-doc rendered manifest YAML into a list of resource dicts."""
    documents: list[dict[str, object]] = []
    for document in yaml.safe_load_all(rendered):
        if isinstance(document, dict):
            documents.append(cast(dict[str, object], document))
    return documents


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _by_kind(documents: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [doc for doc in documents if doc.get("kind") == kind]


def _name_of(document: dict[str, object]) -> str:
    return str(_as_dict(document.get("metadata")).get("name", ""))


def _annotations_of(document: dict[str, object]) -> dict[str, object]:
    return _as_dict(_as_dict(document.get("metadata")).get("annotations"))


# --- toolchain + the offline render/schema gate ----------------------------------


def test_helm_and_kubeconform_binaries_are_present_when_selected() -> None:
    """Both ``helm`` and ``kubeconform`` must exist whenever this marked module runs."""
    assert shutil.which("helm") is not None, (
        "the `helm` binary is required for the `-m helm` gate; install via "
        "`azure/setup-helm` in CI or `brew install helm` locally"
    )
    assert shutil.which("kubeconform") is not None, (
        "the `kubeconform` binary is required for the `-m helm` gate; install via the "
        "pinned download in CI or `brew install kubeconform` locally"
    )


def test_chart_lints() -> None:
    """``helm lint charts/panoptes`` passes (exit 0)."""
    lint_result = _run(_LINT_COMMAND)
    assert lint_result.returncode == 0, (
        f"`helm lint` failed (exit {lint_result.returncode}):\n"
        f"{lint_result.stdout}\n{lint_result.stderr}"
    )


def test_chart_renders_and_passes_kubeconform() -> None:
    """The chart templates offline and the manifests pass ``kubeconform -strict``."""
    rendered = _rendered_manifests()
    kubeconform_result = _run(_KUBECONFORM_COMMAND, stdin_text=rendered)
    assert kubeconform_result.returncode == 0, (
        f"`kubeconform -strict` rejected the rendered manifests "
        f"(exit {kubeconform_result.returncode}):\n"
        f"{kubeconform_result.stdout}\n{kubeconform_result.stderr}"
    )


# --- the SECURITY manifest-shape assertions --------------------------------------


def test_mcp_service_is_clusterip_never_loadbalancer_or_nodeport() -> None:
    """THE anonymous-bypass guard: the MCP Service is `ClusterIP`, never LB/NodePort.

    A LoadBalancer/NodePort MCP Service would expose the MCP HTTP face publicly, bypassing
    the GitHub auth gate at the nginx ingress (decision #5). ClusterIP forces every external
    request through the authenticated ingress. Asserts the MCP Service specifically, and that
    NO Service anywhere in the chart is a LoadBalancer/NodePort.
    """
    services = _by_kind(_parse_documents(_rendered_manifests()), "Service")
    mcp_services = [svc for svc in services if "mcp" in _name_of(svc).lower()]
    assert mcp_services, "the chart must render an MCP Service"
    for svc in mcp_services:
        service_type = _as_dict(svc.get("spec")).get("type")
        assert service_type == "ClusterIP", (
            f"the MCP Service {_name_of(svc)!r} must be ClusterIP (the anonymous-bypass "
            f"guard), got {service_type!r}"
        )
    # Defense in depth: NO Service in the chart may be LoadBalancer/NodePort.
    for svc in services:
        service_type = _as_dict(svc.get("spec")).get("type")
        assert service_type not in ("LoadBalancer", "NodePort"), (
            f"Service {_name_of(svc)!r} is {service_type!r}; the chart must never expose a "
            f"LoadBalancer/NodePort (everything is reached via the gated nginx ingress)"
        )


def test_collector_and_mcp_service_accounts_carry_the_irsa_annotation() -> None:
    """The collector + MCP ServiceAccounts carry the `eks.amazonaws.com/role-arn` IRSA tag."""
    service_accounts = _by_kind(_parse_documents(_rendered_manifests()), "ServiceAccount")
    by_name = {_name_of(sa): sa for sa in service_accounts}
    # The SA names must match the IRSA trust subjects from Phase 6.
    for sa_name in ("panoptes-collector", "panoptes-mcp"):
        assert sa_name in by_name, f"the chart must render the {sa_name!r} ServiceAccount"
        annotations = _annotations_of(by_name[sa_name])
        assert _IRSA_ANNOTATION in annotations, (
            f"the {sa_name!r} ServiceAccount must carry the IRSA {_IRSA_ANNOTATION} annotation"
        )
        assert str(annotations[_IRSA_ANNOTATION]), (
            f"the {sa_name!r} IRSA annotation must carry a non-empty role ARN"
        )


def test_ingress_carries_nginx_forward_auth_and_cert_manager_annotations() -> None:
    """The Ingress carries the nginx forward-auth (→ oauth2-proxy) + cert-manager TLS tags."""
    ingresses = _by_kind(_parse_documents(_rendered_manifests()), "Ingress")
    assert ingresses, "the chart must render a nginx forward-auth Ingress"
    ingress = ingresses[0]
    annotations = _annotations_of(ingress)
    # The nginx external-auth (forward-auth) annotations → oauth2-proxy (the GitHub gate).
    assert _AUTH_URL_ANNOTATION in annotations, (
        "the Ingress must carry the nginx auth-url annotation"
    )
    assert _AUTH_SIGNIN_ANNOTATION in annotations, (
        "the Ingress must carry the nginx auth-signin annotation"
    )
    # Both forward-auth annotations point at the oauth2-proxy endpoints (`/oauth2/auth` is
    # oauth2-proxy's auth-check endpoint; `/oauth2/start` its sign-in redirect).
    assert "/oauth2/" in str(annotations[_AUTH_URL_ANNOTATION]), (
        "the auth-url must point at the oauth2-proxy /oauth2/auth endpoint (the GitHub gate)"
    )
    assert "/oauth2/" in str(annotations[_AUTH_SIGNIN_ANNOTATION]), (
        "the auth-signin must point at the oauth2-proxy /oauth2/start endpoint"
    )
    # cert-manager issues the TLS cert (decision #3 — Let's Encrypt, not ACM).
    assert _CERT_MANAGER_ANNOTATION in annotations, (
        "the Ingress must carry the cert-manager cluster-issuer annotation"
    )
    # The Ingress declares TLS for the hostname.
    spec = _as_dict(ingress.get("spec"))
    assert _as_list(spec.get("tls")), "the Ingress must declare a TLS block (cert-manager)"


def test_ingress_exempts_healthz_from_forward_auth() -> None:
    """`/healthz` is an UNAUTHENTICATED Ingress path (exempt from the forward-auth gate).

    nginx forward-auth is INGRESS-SCOPED (the auth-url/auth-signin annotations gate EVERY
    path on the Ingress they are set on), so `/healthz` must NOT live on a gated Ingress.
    The assertion is UNCONDITIONAL: for EVERY Ingress whose path-set CONTAINS `/healthz`,
    the forward-auth annotations must be ABSENT — so folding `/healthz` onto the gated
    Ingress (which would gate it) red-bars here, never silently passes. A separate check
    confirms a dedicated unauthenticated `/healthz` Ingress actually exists.
    """
    ingresses = _by_kind(_parse_documents(_rendered_manifests()), "Ingress")
    healthz_ingresses = [ing for ing in ingresses if _routes_healthz(ing)]
    assert healthz_ingresses, "the chart must route /healthz on an Ingress"
    for ingress in healthz_ingresses:
        annotations = _annotations_of(ingress)
        # UNCONDITIONAL: any Ingress routing /healthz must NOT carry the forward-auth
        # annotations (forward-auth is Ingress-scoped — a /healthz on a gated Ingress would
        # be gated, breaking the unauthenticated-liveness contract).
        assert _AUTH_URL_ANNOTATION not in annotations, (
            f"Ingress {_name_of(ingress)!r} routes /healthz but carries the forward-auth "
            f"annotation — /healthz must be on an UNAUTHENTICATED Ingress (forward-auth is "
            f"Ingress-scoped, so this would gate the liveness path)"
        )
        assert _AUTH_SIGNIN_ANNOTATION not in annotations, (
            f"Ingress {_name_of(ingress)!r} routes /healthz but carries the auth-signin "
            f"annotation — the liveness path must be unauthenticated"
        )


def test_a_dedicated_healthz_ingress_exists() -> None:
    """A dedicated Ingress routes ONLY `/healthz` (the single unauthenticated liveness path).

    Paired with the unconditional exemption check above: this confirms the unauthenticated
    `/healthz` route actually exists (it is not merely absent), so liveness probing has a
    real un-gated path.
    """
    ingresses = _by_kind(_parse_documents(_rendered_manifests()), "Ingress")
    dedicated = [ing for ing in ingresses if _only_routes_healthz(ing)]
    assert dedicated, "the chart must render a dedicated /healthz-only (unauthenticated) Ingress"


def test_victoriametrics_statefulset_has_one_replica_and_a_pvc() -> None:
    """The VictoriaMetrics store is a single-node StatefulSet: EXACTLY one replica + a PVC."""
    statefulsets = _by_kind(_parse_documents(_rendered_manifests()), "StatefulSet")
    vm_sets = [sts for sts in statefulsets if "victoria" in _name_of(sts).lower()]
    assert len(vm_sets) == 1, "the chart must render exactly one VictoriaMetrics StatefulSet"
    spec = _as_dict(vm_sets[0].get("spec"))
    assert spec.get("replicas") == 1, (
        "the VM StatefulSet must have EXACTLY one replica (single-node)"
    )
    # The PVC is declared via volumeClaimTemplates (the StatefulSet's persistent storage).
    pvc_templates = _as_list(spec.get("volumeClaimTemplates"))
    assert pvc_templates, "the VM StatefulSet must declare a volumeClaimTemplate (its PVC)"


def test_no_workload_mounts_a_write_capable_observed_credential() -> None:
    """No workload mounts a write-capable observed credential (read-only-wrt-observed).

    Panoptes is read-only w.r.t. observed systems: a pod must never mount an AWS access-key
    Secret or a write-scoped credential as a volume. The read scope comes from the IRSA SA
    token (projected by EKS), never a mounted static credential. Scans every Deployment +
    StatefulSet pod spec's volumes for a Secret volume whose name hints at a write credential.
    """
    documents = _parse_documents(_rendered_manifests())
    workloads = _by_kind(documents, "Deployment") + _by_kind(documents, "StatefulSet")
    # Names that would indicate a mounted static write-capable credential.
    forbidden_volume_hints = ("aws-access-key", "aws-secret-key", "write-credential", "admin-key")
    for workload in workloads:
        pod_spec = _pod_spec(workload)
        for volume in _as_list(pod_spec.get("volumes")):
            volume_dict = _as_dict(volume)
            volume_name = str(volume_dict.get("name", "")).lower()
            # A Secret-backed volume named like a write credential is forbidden.
            if "secret" in volume_dict:
                for hint in forbidden_volume_hints:
                    assert hint not in volume_name, (
                        f"workload {_name_of(workload)!r} mounts a write-capable credential "
                        f"volume {volume_name!r}; observed access is read-only via IRSA only"
                    )


# --- the GitHub-gate ENFORCEMENT assertions (Fix 2 + Fix 7) -----------------------


def test_oauth2_proxy_enforces_a_non_empty_github_org() -> None:
    """The rendered oauth2-proxy args carry a NON-EMPTY `--github-org=<value>`.

    A bare `--github-org=` would DISABLE the GitHub allowlist (oauth2-proxy v7.7.1 admits any
    user when Org==""), so the gate would be wide open. The CI fixture supplies a real org; a
    rendered empty-value org-arg must be REJECTED — this pins ENFORCEMENT, not just that the
    arg is wired.
    """
    args = _oauth2_proxy_args()
    org_args = [arg for arg in args if arg.startswith("--github-org=")]
    assert org_args, "the oauth2-proxy args must include --github-org"
    for org_arg in org_args:
        value = org_arg.split("=", 1)[1]
        assert value, (
            f"oauth2-proxy renders a BARE {org_arg!r} (empty org disables the GitHub "
            f"allowlist and admits any user); the org allowlist must be non-empty"
        )


def test_helm_template_with_empty_github_org_fails_closed() -> None:
    """`helm template --set oauth2Proxy.githubOrg=""` FAILS (the fail-closed guard fires).

    The NEGATIVE half of Fix 2: an empty org must ABORT the render (non-zero exit), not
    produce a chart with an open gate. Proven by the values.schema.json minLength + the
    template `required` guard — either firing is the fail-closed behavior under test.
    """
    fail_closed = _run(
        (
            "helm",
            "template",
            str(_CHART_DIR),
            "-f",
            str(_CI_VALUES),
            "--set",
            "oauth2Proxy.githubOrg=",
        )
    )
    assert fail_closed.returncode != 0, (
        "`helm template` with an EMPTY oauth2Proxy.githubOrg must FAIL (fail-closed); it "
        f"exited 0 instead:\n{fail_closed.stdout}\n{fail_closed.stderr}"
    )
    # The failure names the org requirement (the schema minLength or the required-message).
    combined = (fail_closed.stdout + fail_closed.stderr).lower()
    assert "githuborg" in combined or "github-org" in combined or "github" in combined, (
        f"the fail-closed error should name the org requirement; got:\n"
        f"{fail_closed.stdout}\n{fail_closed.stderr}"
    )


def test_oauth2_proxy_sets_xauthrequest_so_the_identity_header_is_injected() -> None:
    """The oauth2-proxy args include `--set-xauthrequest=true` (the identity-header switch).

    `--set-xauthrequest=true` is what makes oauth2-proxy inject the `X-Auth-Request-User`
    header — the exact header the integration forward-auth-gate simulation keys on. Pinning
    it here ties the test simulation to the deployed gate's ground truth (so they cannot
    silently drift; if the deployed flag is dropped, the integration sim would be testing a
    header the gate never injects).
    """
    args = _oauth2_proxy_args()
    assert "--set-xauthrequest=true" in args, (
        "oauth2-proxy must set --set-xauthrequest=true so X-Auth-Request-User is injected "
        "(the header the integration gate simulation keys on)"
    )


# --- workload hardening assertions (Fix 3) ---------------------------------------


def test_every_workload_runs_non_root_and_drops_all_capabilities() -> None:
    """Every workload's pod runs non-root + RuntimeDefault seccomp; every container drops ALL.

    Defense-in-depth hardening on all five workloads (collector/mcp/grafana/oauth2-proxy
    Deployments + the VM StatefulSet): the pod spec sets runAsNonRoot + a RuntimeDefault
    seccomp profile, and EVERY container drops ALL Linux capabilities + forbids privilege
    escalation.
    """
    documents = _parse_documents(_rendered_manifests())
    workloads = _by_kind(documents, "Deployment") + _by_kind(documents, "StatefulSet")
    assert workloads, "the chart must render workloads to harden"
    for workload in workloads:
        pod_spec = _pod_spec(workload)
        pod_sc = _as_dict(pod_spec.get("securityContext"))
        # Pod-level: runAsNonRoot + RuntimeDefault seccomp.
        assert pod_sc.get("runAsNonRoot") is True, (
            f"workload {_name_of(workload)!r} pod must set runAsNonRoot: true"
        )
        seccomp = _as_dict(pod_sc.get("seccompProfile"))
        assert seccomp.get("type") == "RuntimeDefault", (
            f"workload {_name_of(workload)!r} pod must set seccompProfile RuntimeDefault"
        )
        # Container-level: every container drops ALL caps + forbids privilege escalation.
        containers = _as_list(pod_spec.get("containers"))
        assert containers, f"workload {_name_of(workload)!r} must declare containers"
        for container in containers:
            container_sc = _as_dict(_as_dict(container).get("securityContext"))
            assert container_sc.get("allowPrivilegeEscalation") is False, (
                f"container in {_name_of(workload)!r} must set allowPrivilegeEscalation: false"
            )
            dropped = _as_list(_as_dict(container_sc.get("capabilities")).get("drop"))
            assert "ALL" in [str(cap) for cap in dropped], (
                f"container in {_name_of(workload)!r} must drop ALL capabilities"
            )


def _oauth2_proxy_args() -> list[str]:
    """The rendered oauth2-proxy container args (the GitHub gate's flags)."""
    documents = _parse_documents(_rendered_manifests())
    for deployment in _by_kind(documents, "Deployment"):
        if "oauth2-proxy" not in _name_of(deployment).lower():
            continue
        for container in _as_list(_pod_spec(deployment).get("containers")):
            if str(_as_dict(container).get("name")) == "oauth2-proxy":
                return [str(arg) for arg in _as_list(_as_dict(container).get("args"))]
    raise AssertionError("the chart must render the oauth2-proxy container with args")


def _routes_healthz(ingress: dict[str, object]) -> bool:
    """Whether an Ingress has a rule routing the `/healthz` path."""
    return "/healthz" in _ingress_paths(ingress)


def _only_routes_healthz(ingress: dict[str, object]) -> bool:
    """Whether an Ingress routes ONLY `/healthz` (the dedicated unauthenticated path)."""
    paths = _ingress_paths(ingress)
    return paths == {"/healthz"}


def _ingress_paths(ingress: dict[str, object]) -> set[str]:
    """The set of HTTP paths declared across an Ingress's rules."""
    paths: set[str] = set()
    spec = _as_dict(ingress.get("spec"))
    for rule in _as_list(spec.get("rules")):
        http = _as_dict(_as_dict(rule).get("http"))
        for path_entry in _as_list(http.get("paths")):
            path_value = _as_dict(path_entry).get("path")
            if isinstance(path_value, str):
                paths.add(path_value)
    return paths


def _pod_spec(workload: dict[str, object]) -> dict[str, object]:
    """The pod `spec` of a Deployment/StatefulSet (`spec.template.spec`)."""
    template = _as_dict(_as_dict(workload.get("spec")).get("template"))
    return _as_dict(template.get("spec"))
