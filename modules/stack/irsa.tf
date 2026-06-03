# Panoptes hosting module — the IRSA role (THE SECURITY CORE).
#
# The home principal is an IRSA role (decision #1, spec § Authorization Rules), NOT a node
# instance profile — so the collector/MCP pods get exactly their grant via the projected SA
# token, with no node-wide profile any co-scheduled pod would inherit (K9 least privilege).
#
# The role has THREE precisely-scoped facets, and NOTHING else:
#
#   1. TRUST  — an OIDC trust to the CLUSTER's OIDC provider (eks.tf), with a `StringEquals`
#      condition pinning the `:sub` to EXACTLY the collector AND MCP
#      `system:serviceaccount:<namespace>:<sa>` subjects (+ `:aud = sts.amazonaws.com`). No
#      other SA — and no other pod — can assume this role (K9).
#   2. ASSUME — `sts:AssumeRole` on EACH `var.read_role_arns` entry (in-account, decision #1).
#      The statement is STRUCTURALLY ABSENT when the list is empty, so `read_role_arns = []`
#      (the stage/prod disabled-stub default, decision #6) provably yields ZERO assume-role
#      grants — no `Resource: "*"`, no statement at all.
#   3. PUBLISH — a SINGLE resource-scoped `sns:Publish` on `var.alert_topic_arn` ONLY (the one
#      write grant in the whole system, on a Panoptes-OWNED topic). Absent when the ARN is "".
#
# There is explicitly NO `Action = "*"`, NO `Put*/Create*/Delete*` on any non-Panoptes
# resource, and NO observed write anywhere. The plan-assertion test verifies every one of
# these invariants against the rendered config (Risk K3/K9 — static assertion, no apply).

# --- (1) TRUST: OIDC trust to the cluster provider, scoped to the two SAs (K9) ------

data "aws_iam_policy_document" "irsa_trust" {
  statement {
    sid     = "PanoptesIrsaOidcTrust"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.cluster.arn]
    }

    # `:sub` pinned to EXACTLY the collector + MCP service accounts (in `var.namespace`) —
    # the credential is scoped to those two pods' SAs and no others (K9). A `StringEquals`
    # (not `StringLike`) so no wildcard widening is possible.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values = [
        "system:serviceaccount:${var.namespace}:${var.collector_service_account}",
        "system:serviceaccount:${var.namespace}:${var.mcp_service_account}",
      ]
    }

    # `:aud` pinned to the STS audience — the standard IRSA audience guard.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "irsa" {
  name               = "panoptes-irsa"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust.json

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

# --- (2)+(3) PERMISSIONS: scoped assume-role + the single resource-scoped publish ---

data "aws_iam_policy_document" "irsa_permissions" {
  # (2) ASSUME — `sts:AssumeRole` on EACH read-role ARN, IN-ACCOUNT (decision #1). The
  # `dynamic` block's `for_each` is `[1]` ONLY when the list is non-empty, so an empty
  # `read_role_arns` produces NO assume-role statement at all (provably zero grants — the
  # stage/prod disabled-stub case, decision #6). `resources = var.read_role_arns` scopes the
  # grant to exactly the supplied ARNs — never `"*"`.
  dynamic "statement" {
    for_each = length(var.read_role_arns) > 0 ? [1] : []
    content {
      sid       = "PanoptesAssumeReadRoles"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = var.read_role_arns
    }
  }

  # (3) PUBLISH — a SINGLE resource-scoped `sns:Publish` on the ONE Panoptes-owned alert
  # topic (the only write grant in the system). `for_each` is `[1]` only when an ARN is
  # configured, so an empty `alert_topic_arn` produces NO publish statement. `resources` is
  # the single topic ARN — NEVER `"*"`.
  dynamic "statement" {
    for_each = var.alert_topic_arn != "" ? [1] : []
    content {
      sid       = "PanoptesAlertPublish"
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [var.alert_topic_arn]
    }
  }
}

# The permission policy is attached only when it has at least one statement (an all-default
# module — empty read roles + empty topic — yields an empty document, which AWS rejects, so
# the policy + attachment are gated on there being something to grant).
locals {
  irsa_has_permissions = length(var.read_role_arns) > 0 || var.alert_topic_arn != ""
}

resource "aws_iam_policy" "irsa" {
  count = local.irsa_has_permissions ? 1 : 0

  name   = "panoptes-irsa"
  policy = data.aws_iam_policy_document.irsa_permissions.json

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

resource "aws_iam_role_policy_attachment" "irsa" {
  count = local.irsa_has_permissions ? 1 : 0

  role       = aws_iam_role.irsa.name
  policy_arn = aws_iam_policy.irsa[0].arn
}
