"""Hermetic plan-assertion test for the Panoptes hosting module (the SECURITY core).

Asserts the load-bearing IRSA scoping / resource-scoped publish / no-wildcard-write
invariants against the STATIC rendered config (Risk K3 — never a live `apply`/creds: a
real plan needs AWS access CI must not hold). It parses `modules/stack/*.tf` with
`python-hcl2` into plain dicts and asserts on the rendered HCL structure.

Covers (spec § Authorization Rules / plan Phase 6 red-tests table, Risks K3/K9/K12):
- (a) an `aws_eks_cluster` + a DEDICATED SEPARATE `aws_vpc` (not an observed one);
- (b) the IRSA role TRUST has a `StringEquals` on the cluster-OIDC `:sub` scoped to the
  collector AND MCP `system:serviceaccount:<ns>:<sa>` subjects (+ `:aud`);
- (c) the `sns:Publish` statement's Resource is the single `alert_topic_arn` (NOT `*`);
- (d) NO statement has `Action = "*"` or `Put*/Create*/Delete*` on a non-Panoptes resource;
- (e) `read_role_arns = []` → ZERO assume-role grants (the wiring statically guarantees it:
  the assume-role statement is gated on the list being non-empty AND its `resources` is the
  list itself, so an empty list provably yields no grant);
- (f) the node group is a single small MANAGED SPOT group with NO Karpenter resource.

Binary gating (Risk K2): the whole module is `pytest.mark.terraform` so the base unit run
deselects it; `python-hcl2` is a dev dep (it parses HCL with no terraform binary needed),
but the marker keeps this test alongside the other IaC tests. NEVER `pytest.skip` in a body
— a missing parser while the test is explicitly selected red-bars.
"""

import re
from pathlib import Path
from typing import TypedDict, cast

import hcl2
import pytest

# Gate the whole module on the `terraform` marker (alongside the validate test).
pytestmark = pytest.mark.terraform

# Repo root is two levels up from this file (tests/terraform/<this>.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "modules" / "stack"

# The IRSA trust `:sub` condition references the SA subjects via variable interpolation —
# `system:serviceaccount:${var.namespace}:${var.<sa>}` — so the rendered HCL carries the
# var refs, not the resolved defaults. Assert on the variable-reference form (the actual
# rendered config), and separately assert the variable DEFAULTS pin the expected names.
_COLLECTOR_SA_SUBJECT = "system:serviceaccount:${var.namespace}:${var.collector_service_account}"
_MCP_SA_SUBJECT = "system:serviceaccount:${var.namespace}:${var.mcp_service_account}"
_NAMESPACE_DEFAULT = "panoptes"
_COLLECTOR_SA_DEFAULT = "panoptes-collector"
_MCP_SA_DEFAULT = "panoptes-mcp"


def _content_body(body_dict: dict[str, object]) -> dict[str, object]:
    """Return the `content` body of a `dynamic` block (python-hcl2 renders it as a list)."""
    content = body_dict.get("content")
    if isinstance(content, list) and content:
        return _as_dict(content[0])
    return _as_dict(content)


class _Statement(TypedDict, total=False):
    """A normalized IAM policy-document statement (the subset the assertions read)."""

    sid: str
    effect: str
    actions: list[str]
    resources: list[str]


def _load_tf(filename: str) -> dict[str, object]:
    """Parse one `modules/stack/<filename>` HCL file into a plain dict.

    `python-hcl2` (8.x) wraps block labels + string values in literal quotes and adds
    `__is_block__`/`__comments__` markers; `_unquote`/`_clean` below normalize those away
    at each read site.
    """
    with (_MODULE_DIR / filename).open(encoding="utf-8") as handle:
        parsed = hcl2.load(handle)
    return cast(dict[str, object], parsed)


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _unquote(key: str) -> str:
    """Strip the surrounding literal quotes python-hcl2 8.x wraps block labels in."""
    return key.strip('"')


def _resource_blocks(tf: dict[str, object], resource_type: str) -> list[dict[str, object]]:
    """Return every `resource "<resource_type>" "<name>" { ... }` body in a parsed file.

    Each `resource` entry is `{'"<type>"': {'"<name>"': {<body>}}}`; this collects the bodies
    for the requested (unquoted) type.
    """
    bodies: list[dict[str, object]] = []
    for entry in _as_list(tf.get("resource")):
        for raw_type, by_name in _as_dict(entry).items():
            if _unquote(raw_type) != resource_type:
                continue
            for _name, body in _as_dict(by_name).items():
                bodies.append(_as_dict(body))
    return bodies


def _data_blocks(tf: dict[str, object], data_type: str) -> list[dict[str, object]]:
    """Return every `data "<data_type>" "<name>" { ... }` body in a parsed file."""
    bodies: list[dict[str, object]] = []
    for entry in _as_list(tf.get("data")):
        for raw_type, by_name in _as_dict(entry).items():
            if _unquote(raw_type) != data_type:
                continue
            for _name, body in _as_dict(by_name).items():
                bodies.append(_as_dict(body))
    return bodies


def _all_resource_type_names() -> set[str]:
    """Every resource TYPE declared anywhere in the module (for the no-Karpenter check)."""
    types: set[str] = set()
    for tf_file in sorted(_MODULE_DIR.glob("*.tf")):
        with tf_file.open(encoding="utf-8") as handle:
            tf = cast(dict[str, object], hcl2.load(handle))
        for entry in _as_list(tf.get("resource")):
            for raw_type in _as_dict(entry):
                types.add(_unquote(raw_type))
    return types


# --- (a) dedicated VPC + EKS cluster ----------------------------------------------


def test_provisions_a_dedicated_eks_cluster() -> None:
    """The module provisions an `aws_eks_cluster` (Panoptes' OWN dedicated cluster)."""
    clusters = _resource_blocks(_load_tf("eks.tf"), "aws_eks_cluster")
    assert clusters, "modules/stack must declare an aws_eks_cluster (the dedicated cluster)"


def test_provisions_a_dedicated_separate_vpc_not_an_observed_one() -> None:
    """The module provisions a DEDICATED `aws_vpc` (failure-domain independence, K12).

    A separate VPC resource — gated on `create_vpc` — proves Panoptes runs in its OWN VPC,
    never co-tenanting an observed workload's VPC.
    """
    vpc_tf = _load_tf("vpc.tf")
    vpcs = _resource_blocks(vpc_tf, "aws_vpc")
    assert vpcs, "modules/stack must declare a dedicated aws_vpc (never an observed VPC)"
    # The VPC creation is gated on the dedicated-VPC toggle (count = create_vpc ? 1 : 0),
    # so the dedicated posture is the default and the attach-existing path is explicit.
    vpc = vpcs[0]
    count_expr = str(vpc.get("count", ""))
    assert "create_vpc" in count_expr, (
        "the dedicated VPC must be gated on var.create_vpc so co-tenancy is never the default"
    )


# --- (b) IRSA trust scoped to the cluster-OIDC :sub for the two SAs ----------------


def _irsa_trust_conditions() -> list[dict[str, object]]:
    """The `condition` blocks of the IRSA trust policy document (irsa.tf)."""
    trust_docs = _data_blocks(_load_tf("irsa.tf"), "aws_iam_policy_document")
    # The trust doc is the one whose statement federates the OIDC provider.
    for doc in trust_docs:
        for statement in _as_list(doc.get("statement")):
            stmt = _as_dict(statement)
            actions = _as_list(stmt.get("actions"))
            if any("AssumeRoleWithWebIdentity" in str(action) for action in actions):
                return [_as_dict(cond) for cond in _as_list(stmt.get("condition"))]
    return []


def test_irsa_trust_pins_sub_to_collector_and_mcp_service_accounts() -> None:
    """The IRSA trust `StringEquals` on the cluster-OIDC `:sub` pins to BOTH SAs (K9)."""
    conditions = _irsa_trust_conditions()
    assert conditions, "the IRSA trust policy must carry OIDC :sub/:aud conditions"

    sub_condition = _find_condition(conditions, suffix=":sub")
    assert sub_condition is not None, "the IRSA trust must have a :sub condition"
    # It MUST be a StringEquals (not StringLike — no wildcard widening of the SA scope).
    assert _unquote(str(sub_condition.get("test"))) == "StringEquals", (
        "the :sub condition must be StringEquals (exact match), never StringLike"
    )
    sub_values = " ".join(str(value) for value in _as_list(sub_condition.get("values")))
    # Pinned to EXACTLY the collector AND MCP service-account subjects (the rendered HCL
    # carries the variable-reference form `system:serviceaccount:${var.namespace}:${var.sa}`).
    assert _COLLECTOR_SA_SUBJECT in sub_values, (
        "the IRSA trust :sub must pin the collector service account subject"
    )
    assert _MCP_SA_SUBJECT in sub_values, (
        "the IRSA trust :sub must pin the MCP service account subject"
    )
    # Exactly TWO subjects — no extra SA silently widening the scope.
    assert len(_as_list(sub_condition.get("values"))) == 2, (
        "the IRSA trust :sub must pin EXACTLY the two SAs (collector + MCP), no more"
    )


def test_irsa_subject_variable_defaults_pin_the_expected_service_accounts() -> None:
    """The namespace + SA variable DEFAULTS resolve the trust subjects to the expected names.

    The trust condition references the SAs via variables; this asserts those variables
    default to `panoptes` / `panoptes-collector` / `panoptes-mcp`, so the rendered subjects
    are the SAs the Helm chart (Phase 7) mounts the collector + MCP pods under.
    """
    defaults = _variable_defaults()
    assert defaults.get("namespace") == _NAMESPACE_DEFAULT
    assert defaults.get("collector_service_account") == _COLLECTOR_SA_DEFAULT
    assert defaults.get("mcp_service_account") == _MCP_SA_DEFAULT


def _variable_defaults() -> dict[str, object]:
    """Map each variable name to its `default` value (variables.tf)."""
    variables_tf = _load_tf("variables.tf")
    defaults: dict[str, object] = {}
    for entry in _as_list(variables_tf.get("variable")):
        for raw_name, body in _as_dict(entry).items():
            name = _unquote(raw_name)
            raw_default = _as_dict(body).get("default")
            defaults[name] = _unquote(raw_default) if isinstance(raw_default, str) else raw_default
    return defaults


def test_irsa_trust_pins_aud_to_sts() -> None:
    """The IRSA trust also pins the OIDC `:aud` to the STS audience (the standard guard)."""
    conditions = _irsa_trust_conditions()
    aud_condition = _find_condition(conditions, suffix=":aud")
    assert aud_condition is not None, "the IRSA trust must have an :aud condition"
    assert _unquote(str(aud_condition.get("test"))) == "StringEquals"
    aud_values = " ".join(str(value) for value in _as_list(aud_condition.get("values")))
    assert "sts.amazonaws.com" in aud_values, "the :aud must pin sts.amazonaws.com"


def test_irsa_trust_federates_the_cluster_oidc_provider() -> None:
    """The IRSA trust principal is the CLUSTER's OIDC provider (the trust anchor)."""
    trust_docs = _data_blocks(_load_tf("irsa.tf"), "aws_iam_policy_document")
    federated_refs: list[str] = []
    for doc in trust_docs:
        for statement in _as_list(doc.get("statement")):
            stmt = _as_dict(statement)
            for principal in _as_list(stmt.get("principals")):
                principal_dict = _as_dict(principal)
                if _unquote(str(principal_dict.get("type"))) == "Federated":
                    federated_refs.extend(
                        str(identifier)
                        for identifier in _as_list(principal_dict.get("identifiers"))
                    )
    joined = " ".join(federated_refs)
    assert "aws_iam_openid_connect_provider.cluster" in joined, (
        "the IRSA trust must federate the CLUSTER's OIDC provider (the IRSA trust anchor)"
    )


def _find_condition(
    conditions: list[dict[str, object]], *, suffix: str
) -> dict[str, object] | None:
    """The condition whose `variable` ends with `suffix` (e.g. `:sub` / `:aud`)."""
    for condition in conditions:
        variable = _unquote(str(condition.get("variable", "")))
        if variable.endswith(suffix):
            return condition
    return None


# --- IRSA permission statements (publish scope + no wildcard write) ----------------


def _irsa_permission_statements() -> list[_Statement]:
    """The statements of the IRSA PERMISSION policy document (the assume-role + publish)."""
    perm_docs = _data_blocks(_load_tf("irsa.tf"), "aws_iam_policy_document")
    for doc in perm_docs:
        statements: list[_Statement] = []
        is_permission_doc = False
        for raw_statement in _as_list(doc.get("statement")):
            # A `dynamic "statement"` block parses as a `dynamic` entry, not `statement`;
            # both shapes are normalized by `_normalize_statement`.
            normalized = _normalize_statement(_as_dict(raw_statement))
            if normalized is None:
                continue
            statements.append(normalized)
        # The permission doc is the one carrying sns:Publish or sts:AssumeRole (NOT the
        # trust doc, whose action is AssumeRoleWithWebIdentity).
        for stmt in statements:
            actions = " ".join(stmt.get("actions", []))
            if "sns:Publish" in actions or actions == "sts:AssumeRole":
                is_permission_doc = True
        if is_permission_doc:
            return statements
    return _dynamic_permission_statements()


def _dynamic_permission_statements() -> list[_Statement]:
    """Read the IRSA permission statements from their `dynamic "statement"` blocks.

    The assume-role + publish statements are `dynamic "statement"` blocks (gated on the
    inputs), so they live under the document's `dynamic` key, each with a `content` body.
    """
    perm_docs = _data_blocks(_load_tf("irsa.tf"), "aws_iam_policy_document")
    statements: list[_Statement] = []
    for doc in perm_docs:
        for dynamic_entry in _as_list(doc.get("dynamic")):
            for raw_label, body in _as_dict(dynamic_entry).items():
                if _unquote(raw_label) != "statement":
                    continue
                content = _content_body(_as_dict(body))
                normalized = _normalize_statement(content)
                if normalized is not None:
                    statements.append(normalized)
    return statements


def _normalize_statement(raw: dict[str, object]) -> _Statement | None:
    """Normalize a statement (or dynamic-statement content) to the assertion shape.

    `resources` may render as a LIST (`["${var.alert_topic_arn}"]`) or as a STRING
    interpolation of a list var (`"${var.read_role_arns}"`) — both are normalized to a
    list of unquoted action/resource tokens so the assertions read one shape.
    """
    actions = [_unquote(str(action)) for action in _as_list(raw.get("actions"))]
    resources = _string_or_list(raw.get("resources"))
    if not actions and not resources:
        return None
    return _Statement(
        sid=_unquote(str(raw.get("sid", ""))),
        effect=_unquote(str(raw.get("effect", ""))),
        actions=actions,
        resources=resources,
    )


def _string_or_list(value: object) -> list[str]:
    """Coerce a `resources`/`actions` field to a list (handles the string-interpolation form)."""
    if isinstance(value, list):
        return [_unquote(str(item)) for item in value]
    if isinstance(value, str) and value:
        return [_unquote(value)]
    return []


# --- (c) sns:Publish is resource-scoped to the single alert_topic_arn --------------


def test_sns_publish_is_resource_scoped_to_the_single_alert_topic() -> None:
    """The `sns:Publish` statement's Resource is the single `var.alert_topic_arn` (NOT `*`)."""
    statements = _irsa_permission_statements()
    publish = [s for s in statements if "sns:Publish" in s.get("actions", [])]
    assert publish, "the IRSA permission policy must carry a single sns:Publish statement"
    assert len(publish) == 1, "there must be EXACTLY ONE sns:Publish statement"
    resources = publish[0].get("resources", [])
    # The resource is the single alert-topic ARN var — never `*`.
    joined = " ".join(resources)
    assert "var.alert_topic_arn" in joined, (
        "sns:Publish must be scoped to var.alert_topic_arn, the one Panoptes-owned topic"
    )
    assert "*" not in joined, "sns:Publish must NOT be Resource: '*' (resource-scoped only)"


# --- (d) NO wildcard action / no Put*/Create*/Delete* on a non-Panoptes resource ---


def test_no_irsa_statement_has_a_wildcard_action() -> None:
    """No IRSA permission statement grants `Action = "*"` (no blanket write)."""
    for statement in _irsa_permission_statements():
        for action in statement.get("actions", []):
            assert action.strip('"') != "*", (
                f"IRSA statement {statement.get('sid')!r} must not grant Action='*'"
            )


def test_no_irsa_statement_grants_put_create_delete_writes() -> None:
    """No IRSA statement grants a `Put*/Create*/Delete*` write on any resource.

    The IRSA role's only write is the single resource-scoped `sns:Publish`; any
    `Put*/Create*/Delete*` (or `Write*`) action would be an over-grant the least-privilege
    posture forbids. Asserts across EVERY action of EVERY permission statement.
    """
    forbidden = re.compile(r":(Put|Create|Delete|Write|Modify|Update|Terminate)", re.IGNORECASE)
    for statement in _irsa_permission_statements():
        for action in statement.get("actions", []):
            assert not forbidden.search(action), (
                f"IRSA statement {statement.get('sid')!r} grants a forbidden write action: "
                f"{action!r} (only the resource-scoped sns:Publish is allowed)"
            )


# --- (e) read_role_arns = [] → ZERO assume-role grants (statically guaranteed) -----


def test_assume_role_grant_is_gated_on_non_empty_read_role_arns() -> None:
    """The assume-role statement is STRUCTURALLY ABSENT when `read_role_arns` is empty.

    The empty-list → zero-grants property is statically guaranteed by the wiring: the
    assume-role `dynamic "statement"` block's `for_each` is `length(var.read_role_arns) > 0`
    (so no statement renders for an empty list) AND its `resources = var.read_role_arns` (so
    even if it rendered, an empty list could not produce a grant — never a `Resource: "*"`).
    This test asserts BOTH halves of that guarantee against the rendered config.
    """
    irsa_tf = _load_tf("irsa.tf")
    perm_docs = _data_blocks(irsa_tf, "aws_iam_policy_document")
    assume_dynamic_found = False
    for doc in perm_docs:
        for dynamic_entry in _as_list(doc.get("dynamic")):
            for raw_label, body in _as_dict(dynamic_entry).items():
                if _unquote(raw_label) != "statement":
                    continue
                body_dict = _as_dict(body)
                content = _content_body(body_dict)
                actions = [_unquote(str(a)) for a in _as_list(content.get("actions"))]
                if "sts:AssumeRole" not in actions:
                    continue
                assume_dynamic_found = True
                # (i) The block is gated on the list being non-empty — so [] renders nothing.
                for_each = str(body_dict.get("for_each", ""))
                assert "read_role_arns" in for_each and "length" in for_each, (
                    "the assume-role statement must be gated on length(var.read_role_arns) > 0"
                )
                # (ii) Its resources ARE the list — so [] can never produce a grant.
                # `resources = var.read_role_arns` renders as the interpolation string.
                resources = str(content.get("resources", ""))
                assert "read_role_arns" in resources, (
                    "assume-role resources must be var.read_role_arns (empty list -> no grant)"
                )
                assert "*" not in resources, "assume-role must never be Resource: '*'"
    assert assume_dynamic_found, "the IRSA policy must wire the gated assume-role statement"


def test_read_role_arns_defaults_to_empty_list() -> None:
    """`read_role_arns` defaults to `[]` — the disabled-stub posture (zero grants by default)."""
    variables_tf = _load_tf("variables.tf")
    for entry in _as_list(variables_tf.get("variable")):
        for raw_name, body in _as_dict(entry).items():
            if _unquote(raw_name) != "read_role_arns":
                continue
            default = _as_dict(body).get("default")
            assert default == [], "read_role_arns must default to [] (zero grants by default)"
            return
    raise AssertionError("variables.tf must declare read_role_arns")


# --- (f) single small MANAGED SPOT node group, NO Karpenter ------------------------


def test_node_group_is_a_single_managed_spot_group() -> None:
    """The module declares exactly ONE managed node group, SPOT-capacity by default."""
    node_groups = _resource_blocks(_load_tf("nodegroup.tf"), "aws_eks_node_group")
    assert len(node_groups) == 1, "there must be EXACTLY ONE managed node group (decision #2)"
    group = node_groups[0]
    capacity_type = str(group.get("capacity_type", ""))
    # The capacity type is var-driven, defaulting to SPOT (asserted on the variable default).
    assert "capacity_type" in capacity_type, "the node group capacity must be var-driven"

    variables_tf = _load_tf("variables.tf")
    for entry in _as_list(variables_tf.get("variable")):
        for raw_name, body in _as_dict(entry).items():
            if _unquote(raw_name) == "capacity_type":
                default = _unquote(str(_as_dict(body).get("default")))
                assert default == "SPOT", "capacity_type must default to SPOT (cost discipline)"


def test_no_karpenter_resource_anywhere_in_the_module() -> None:
    """There is NO Karpenter resource anywhere (decision #2 — managed node group, not Karpenter).

    A managed node group is the minimal-ops choice for a static one-node stack; a Karpenter
    provisioner/nodepool/nodeclass would undercut cost discipline and buys nothing here.
    """
    types = _all_resource_type_names()
    karpenter_types = {t for t in types if "karpenter" in t.lower() or "nodepool" in t.lower()}
    assert not karpenter_types, (
        f"the module must declare NO Karpenter resource (decision #2); found {karpenter_types}"
    )


def test_provisions_the_cluster_oidc_provider() -> None:
    """The module provisions the cluster's OIDC provider (the IRSA trust anchor)."""
    providers = _resource_blocks(_load_tf("eks.tf"), "aws_iam_openid_connect_provider")
    assert providers, "modules/stack must declare the cluster OIDC provider (IRSA trust anchor)"


# --- MODULE-WIDE IAM guard (every .tf, not just irsa.tf — the no-over-grant invariant) -
#
# The IRSA-scoped tests above assert the IRSA role's least privilege; these widen the
# no-over-grant invariant to the WHOLE module: a future .tf adding a blanket policy
# attachment, a wildcard `aws_iam_policy_document` statement, an inline role policy, or an
# IAM user would all be over-grants the static config must never carry. Scanning every file
# (not just irsa.tf) makes the guard robust to a new resource file slipping in unaudited.

# The ONLY policy ARNs any role in the module may carry: the AWS-managed EKS cluster/worker
# policies (control plane + node group bootstrap) — and the in-module `aws_iam_policy.irsa`
# reference (the scoped IRSA permission policy, whose contents the irsa.tf tests gate).
_ALLOWED_MANAGED_POLICY_NAMES = frozenset(
    {
        "AmazonEKSClusterPolicy",
        "AmazonEKSWorkerNodePolicy",
        "AmazonEKS_CNI_Policy",
        "AmazonEC2ContainerRegistryReadOnly",
        # The EBS CSI driver controller's managed policy (ebs_csi.tf) — note the `service-role/`
        # path segment, which the substring check below requires verbatim.
        "service-role/AmazonEBSCSIDriverPolicy",
    }
)
# The forbidden-write action regex (reused module-wide). A wildcard action OR any
# Put*/Create*/Delete*/Write*/Modify*/Update*/Terminate* is an over-grant.
_FORBIDDEN_WRITE_RE = re.compile(
    r":(Put|Create|Delete|Write|Modify|Update|Terminate)", re.IGNORECASE
)


def _all_policy_attachment_arns() -> list[str]:
    """Every `aws_iam_role_policy_attachment.policy_arn` declared across ALL module .tf."""
    arns: list[str] = []
    for tf_file in sorted(_MODULE_DIR.glob("*.tf")):
        with tf_file.open(encoding="utf-8") as handle:
            tf = cast(dict[str, object], hcl2.load(handle))
        for body in _resource_blocks(tf, "aws_iam_role_policy_attachment"):
            arns.append(str(body.get("policy_arn", "")))
    return arns


def _all_policy_document_statements() -> list[_Statement]:
    """Every `aws_iam_policy_document` statement (incl. dynamic-block content) module-wide.

    Walks every `*.tf`, collects both plain `statement` blocks AND `dynamic "statement"`
    block contents (the assume-role + publish statements render as dynamic blocks), so the
    no-wildcard / no-forbidden-write scan covers EVERY statement the module renders anywhere.
    """
    statements: list[_Statement] = []
    for tf_file in sorted(_MODULE_DIR.glob("*.tf")):
        with tf_file.open(encoding="utf-8") as handle:
            tf = cast(dict[str, object], hcl2.load(handle))
        for doc in _data_blocks(tf, "aws_iam_policy_document"):
            # Plain `statement` blocks.
            for raw_statement in _as_list(doc.get("statement")):
                normalized = _normalize_statement(_as_dict(raw_statement))
                if normalized is not None:
                    statements.append(normalized)
            # `dynamic "statement"` block contents.
            for dynamic_entry in _as_list(doc.get("dynamic")):
                for raw_label, body in _as_dict(dynamic_entry).items():
                    if _unquote(raw_label) != "statement":
                        continue
                    normalized = _normalize_statement(_content_body(_as_dict(body)))
                    if normalized is not None:
                        statements.append(normalized)
    return statements


def test_every_policy_attachment_is_in_the_allowlist() -> None:
    """Every IAM policy attachment in the WHOLE module uses an allowlisted ARN (no over-grant).

    Each `aws_iam_role_policy_attachment.policy_arn` must be one of the four AWS-managed EKS
    policies (the standard control-plane / worker bootstrap) OR the in-module
    `aws_iam_policy.irsa` reference (the scoped IRSA permission policy the irsa.tf tests gate).
    A future blanket attachment (e.g. AdministratorAccess, PowerUserAccess, IAMFullAccess)
    would fail this — the module's roles carry ONLY their minimal bootstrap policies.
    """
    arns = _all_policy_attachment_arns()
    assert arns, "the module must declare IAM policy attachments (cluster + node bootstrap)"
    for arn in arns:
        # The scoped in-module IRSA permission policy is allowed (its contents are gated).
        if "aws_iam_policy.irsa" in arn:
            continue
        # Otherwise the ARN must reference exactly one of the allowlisted AWS-managed policies.
        matched = any(f"policy/{name}" in arn for name in _ALLOWED_MANAGED_POLICY_NAMES)
        assert matched, (
            f"IAM policy attachment uses a non-allowlisted ARN {arn!r}; only the four AWS-"
            f"managed EKS policies + the in-module aws_iam_policy.irsa are permitted"
        )


def test_no_policy_document_statement_anywhere_grants_wildcard_or_write() -> None:
    """No `aws_iam_policy_document` statement in ANY module .tf grants `*` or a write action.

    The module-wide widening of the IRSA no-wildcard / no-Put*/Create*/Delete* invariant:
    EVERY statement of EVERY policy document (across all files, incl. dynamic blocks) must
    grant neither `Action = "*"` nor a forbidden write verb. The trust docs' single action
    (`sts:AssumeRoleWithWebIdentity`) and the permission docs' `sts:AssumeRole` + `sns:Publish`
    all pass; a future blanket/write statement anywhere would red-bar.
    """
    for statement in _all_policy_document_statements():
        for action in statement.get("actions", []):
            assert action.strip('"') != "*", (
                f"policy-document statement {statement.get('sid')!r} grants Action='*'"
            )
            assert not _FORBIDDEN_WRITE_RE.search(action), (
                f"policy-document statement {statement.get('sid')!r} grants a forbidden write "
                f"action: {action!r} (only the resource-scoped sns:Publish is allowed)"
            )


def test_no_inline_role_policy_or_iam_user_anywhere() -> None:
    """The module declares NO inline `aws_iam_role_policy` and NO `aws_iam_user`.

    Inline role policies (`aws_iam_role_policy`) bypass the gated, attached
    `aws_iam_policy.irsa` document — every grant must flow through the audited attached
    policies, not an unscanned inline blob. An `aws_iam_user` (a long-lived static
    credential) has no place in an IRSA-based, SA-token stack. Both being absent keeps the
    module's grant surface fully covered by the statement scan above.
    """
    types = _all_resource_type_names()
    assert "aws_iam_role_policy" not in types, (
        "the module must NOT declare an inline aws_iam_role_policy (grants must flow through "
        "the gated attached aws_iam_policy.irsa, not an unscanned inline blob)"
    )
    assert "aws_iam_user" not in types, (
        "the module must NOT declare an aws_iam_user (no long-lived static credential in an "
        "IRSA-based stack)"
    )


# --- (g) NETWORK HARDENING — the two HIGH-severity fixes, locked so they can't silently regress -
#
# A prior network-security audit found two HIGH exposures: (1) the EKS API server endpoint was
# the AWS-default public 0.0.0.0/0, and (2) the nodes ran in PUBLIC subnets with public IPs (the
# kubelet internet-routable). Both were fixed (private subnets + NAT egress; a required CIDR
# allowlist for the API endpoint). These assertions PIN the fixes against the rendered config so a
# future edit that re-exposes either path (flip subnet_ids back to public, set map_public_ip=true,
# route private egress at the IGW, or allow a 0.0.0.0/0 endpoint) red-bars here.


def _first(value: object) -> object:
    """The first element of a python-hcl2 block list (single blocks render as one-item lists)."""
    if isinstance(value, list) and value:
        return value[0]
    return value


def _resource_blocks_by_name(
    tf: dict[str, object], resource_type: str
) -> dict[str, dict[str, object]]:
    """Map each resource NAME to its body for `resource "<type>" "<name>"` blocks (unquoted)."""
    by_name_out: dict[str, dict[str, object]] = {}
    for entry in _as_list(tf.get("resource")):
        for raw_type, by_name in _as_dict(entry).items():
            if _unquote(raw_type) != resource_type:
                continue
            for name, body in _as_dict(by_name).items():
                by_name_out[_unquote(name)] = _as_dict(body)
    return by_name_out


def test_node_group_runs_in_the_private_subnets_only() -> None:
    """Node group subnet_ids = the PRIVATE pair — nodes get no public IP (HIGH #2)."""
    node_groups = _resource_blocks(_load_tf("nodegroup.tf"), "aws_eks_node_group")
    assert node_groups, "modules/stack must declare the managed node group"
    subnet_ids = str(node_groups[0].get("subnet_ids", ""))
    assert "panoptes_private_subnet_ids" in subnet_ids, (
        "the node group must run in local.panoptes_private_subnet_ids so the kubelet is never "
        "internet-routable — NEVER local.panoptes_public_subnet_ids / a mixed set"
    )


def test_private_subnets_do_not_auto_assign_public_ips() -> None:
    """The private subnets set `map_public_ip_on_launch = false` (no public IP on the nodes)."""
    private_subnet = _resource_blocks_by_name(_load_tf("vpc.tf"), "aws_subnet").get("private")
    assert private_subnet is not None, "vpc.tf must declare the private subnet pair"
    value = str(private_subnet.get("map_public_ip_on_launch", "")).lower()
    assert "false" in value, (
        "private subnets must set map_public_ip_on_launch = false (no public IP on nodes)"
    )


def test_private_route_table_egress_is_via_nat_not_the_igw() -> None:
    """The private route table's default route targets the NAT gateway, not the IGW (HIGH #2)."""
    private_rt = _resource_blocks_by_name(_load_tf("vpc.tf"), "aws_route_table").get("private")
    assert private_rt is not None, "vpc.tf must declare the private route table"
    route = _as_dict(_first(private_rt.get("route")))
    assert "nat_gateway_id" in route, (
        "the private route table's 0.0.0.0/0 route must target the NAT gateway (egress without "
        "ingress) — routing it at the IGW (gateway_id) would re-expose the private nodes"
    )
    assert "gateway_id" not in route, "the private route must NOT use the IGW (gateway_id)"


def test_eks_public_endpoint_is_cidr_allowlisted_not_wide_open() -> None:
    """The EKS API endpoint is private + CIDR-allowlisted, never the 0.0.0.0/0 default (HIGH #1)."""
    clusters = _resource_blocks(_load_tf("eks.tf"), "aws_eks_cluster")
    assert clusters, "eks.tf must declare the cluster"
    vpc_config = _as_dict(_first(clusters[0].get("vpc_config")))
    assert "true" in str(vpc_config.get("endpoint_private_access", "")).lower(), (
        "endpoint_private_access must be true so nodes reach the API over the PRIVATE endpoint"
    )
    public_cidrs = str(vpc_config.get("public_access_cidrs", ""))
    assert "cluster_endpoint_public_access_cidrs" in public_cidrs, (
        "public_access_cidrs must come from var.cluster_endpoint_public_access_cidrs (the operator "
        "allowlist), never the implicit AWS 0.0.0.0/0 default"
    )


def test_endpoint_cidr_var_is_required_and_rejects_wildcards() -> None:
    """The endpoint allowlist var has no default and rejects 0.0.0.0/0 (fail-closed)."""
    variables_tf = _load_tf("variables.tf")
    found = False
    for entry in _as_list(variables_tf.get("variable")):
        for raw_name, body in _as_dict(entry).items():
            if _unquote(raw_name) != "cluster_endpoint_public_access_cidrs":
                continue
            found = True
            body_dict = _as_dict(body)
            # No default → omitting the value hard-errors at plan, never a silent 0.0.0.0/0.
            assert "default" not in body_dict, (
                "cluster_endpoint_public_access_cidrs must have NO default (fail-closed)"
            )
            # The validation must explicitly reject the wildcard CIDR (not only the empty list).
            validation = _as_dict(_first(body_dict.get("validation")))
            condition = str(validation.get("condition", ""))
            assert "0.0.0.0/0" in condition, (
                "the validation must explicitly reject 0.0.0.0/0 (an explicit wildcard, not only "
                "an empty list, would otherwise re-create the wide-open endpoint)"
            )
    assert found, "variables.tf must declare cluster_endpoint_public_access_cidrs"


def test_subnets_carry_the_per_cluster_discovery_tag() -> None:
    """Both subnet pairs carry `kubernetes.io/cluster/<name>` for in-tree LB subnet discovery.

    No AWS Load Balancer Controller is installed, so the EKS-default in-tree cloud provider
    discovers ELB subnets via this per-cluster tag; without it the public nginx LoadBalancer can
    fail to provision (stuck `<pending>` EXTERNAL-IP) against the mixed public/private subnet set.
    """
    subnets = _resource_blocks(_load_tf("vpc.tf"), "aws_subnet")
    assert len(subnets) >= 2, "vpc.tf must declare the public + private subnet pairs"
    for subnet in subnets:
        tags = _as_dict(subnet.get("tags"))
        cluster_tag = [key for key in tags if "kubernetes.io/cluster/" in _unquote(str(key))]
        assert cluster_tag, (
            "every subnet must carry a kubernetes.io/cluster/<name> tag so the in-tree provider "
            "discovers it for LoadBalancer placement"
        )


# --- (h) EBS CSI driver — persistent storage works out of the box (a live-deploy fix) ---------
#
# A live deploy showed the module shipped NO storage driver, so the VictoriaMetrics PVC stayed
# Pending (the in-tree gp2 provisioner is gone on k8s 1.30+). These pin the fix: the managed
# aws-ebs-csi-driver addon (with its own IRSA role, not the node instance role) + a gp3 CSI
# StorageClass as the cluster default.


def test_provisions_the_ebs_csi_driver_with_a_gp3_storageclass() -> None:
    """The module installs the EBS CSI addon (own IRSA role) + a gp3 CSI StorageClass."""
    ebs_tf = _load_tf("ebs_csi.tf")
    addons = _resource_blocks(ebs_tf, "aws_eks_addon")
    assert any("aws-ebs-csi-driver" in str(addon.get("addon_name", "")) for addon in addons), (
        "the module must install the aws-ebs-csi-driver EKS addon (PVCs need a CSI driver on "
        "k8s 1.30+ — the in-tree gp2 provisioner is gone)"
    )
    # The addon must use its DEDICATED IRSA role (not the node instance role — the IMDS-via-node
    # path CrashLooped the controller behind the default IMDS hop limit).
    sa_role = str(addons[0].get("service_account_role_arn", ""))
    assert "aws_iam_role.ebs_csi" in sa_role, (
        "the EBS CSI addon must use its dedicated IRSA role via service_account_role_arn"
    )
    storage_classes = _resource_blocks(ebs_tf, "kubernetes_storage_class_v1")
    assert storage_classes, "the module must create a gp3 CSI StorageClass"
    assert "ebs.csi.aws.com" in str(storage_classes[0].get("storage_provisioner", "")), (
        "the gp3 StorageClass must use the ebs.csi.aws.com provisioner"
    )


def test_ebs_csi_irsa_trust_is_scoped_to_the_controller_sa() -> None:
    """The EBS CSI IRSA trust pins :sub to kube-system:ebs-csi-controller-sa (least privilege)."""
    docs = _data_blocks(_load_tf("ebs_csi.tf"), "aws_iam_policy_document")
    subjects: list[str] = []
    for doc in docs:
        for statement in _as_list(doc.get("statement")):
            for condition in _as_list(_as_dict(statement).get("condition")):
                condition_dict = _as_dict(condition)
                if _unquote(str(condition_dict.get("variable", ""))).endswith(":sub"):
                    subjects.extend(
                        _unquote(str(value)) for value in _as_list(condition_dict.get("values"))
                    )
    assert "system:serviceaccount:kube-system:ebs-csi-controller-sa" in " ".join(subjects), (
        "the EBS CSI IRSA trust must pin :sub to the ebs-csi-controller-sa (no broader scope)"
    )
