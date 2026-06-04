# `modules/stack` — the Panoptes hosting module (dedicated EKS + IRSA + ingress)

The distributable Terraform module that provisions Panoptes' **dedicated** hosting cluster:
a dedicated VPC, a dedicated EKS control plane + the cluster's OIDC provider (the IRSA
trust anchor), one small **managed** node group, the **IRSA role** (SA-scoped to the
collector + MCP service accounts), and the **nginx-ingress + cert-manager** prerequisites.

Runner-agnostic — it validates and applies under both **Terraform** and **OpenTofu**.

## Import

```hcl
module "panoptes" {
  source = "github.com/sidkos/panoptes//modules/stack"

  home_region     = "us-east-1"
  hostname        = "panoptes.example.com"
  image_tag       = "v0.2.0" # an IMMUTABLE tag, never :latest (Risk K11)

  # GitHub SSO (oauth2-proxy github provider — the access gate at the ingress). The client
  # SECRET is not a module input (it would leak into TF state) — it goes into the chart's
  # out-of-band `panoptes-oauth2-proxy` Kubernetes Secret instead.
  github_oauth_client_id = var.github_oauth_client_id
  github_org             = "your-github-org"

  # The per-env read-roles the IRSA role may assume, IN-ACCOUNT. Empty = no grants
  # (stage/prod stay disabled stubs until a non-empty list is supplied — no code change).
  read_role_arns  = ["arn:aws:iam::111122223333:role/PanoptesReadRole-dev"]

  # The single Panoptes-OWNED SNS alert topic the IRSA role may publish to (resource-scoped).
  alert_topic_arn = "arn:aws:sns:us-east-1:111122223333:panoptes-alerts"
  # (the Slack webhook is not a module input either — it goes into the chart's
  #  out-of-band `panoptes-app-secrets` Kubernetes Secret.)
}
```

A worked root config is in [`deploy/terraform/example`](../../deploy/terraform/example).

## Failure-domain independence — dedicated VPC + dedicated cluster, SAME account

**Panoptes runs in its OWN dedicated VPC and its OWN dedicated EKS cluster — NEVER an
observed cluster or VPC.** This is load-bearing (decision #1, Risk K12): the cluster-level
and network-level failure domains are independent of anything Panoptes monitors, so
Panoptes does not "die with what it monitors". `create_vpc = true` (the default) provisions
the dedicated VPC; set it false ONLY to attach to a pre-created **non-observed** VPC in the
same account.

**The cluster runs in the SAME AWS account as the observed infra** (decision #1) so the
existing AWS-profile / assume-role multi-env mechanism keeps working without cross-account
trust plumbing: the IRSA role gives the pod its base identity, and it then assumes the
per-env `PanoptesReadRole/<env>` roles **within the same account**. The **shared-account
control-plane blast radius is a deliberate, documented trade** — cluster + VPC isolation
satisfies the sharpest edge of the failure-domain rule; full account isolation is a clean
future hardening that would break profile-based access today.

## What the module grants (least privilege)

The **IRSA role** is the home principal (not a node instance profile — so a co-scheduled
pod can never inherit the grant). Its trust policy pins the OIDC `:sub` to EXACTLY the
collector + MCP `system:serviceaccount:<namespace>:<sa>` subjects (`StringEquals`, + the
`:aud` STS audience). It holds:

- `sts:AssumeRole` on each `read_role_arns` entry, in-account (zero grants when the list is
  empty); and
- a **single resource-scoped `sns:Publish`** on `alert_topic_arn` ONLY — the one write
  grant in the whole system, on a Panoptes-owned topic.

There is no `Action = "*"`, no `Put*/Create*/Delete*` on any non-Panoptes resource, and no
observed write anywhere. The `tests/terraform/test_module_plan.py` plan-assertion test
verifies every one of these invariants against the rendered config (no live apply).

## Ingress + TLS + SSO

`nginx-ingress` + `cert-manager` (Let's Encrypt) prerequisites are installed (decision #3 —
NOT ALB/ACM, because GitHub is OAuth2 and does not expose OIDC discovery, so ALB-native
`authenticate-oidc` cannot gate it). The Panoptes Ingress (shipped by `charts/panoptes` in
Phase 7) carries the nginx forward-auth annotations that point at **oauth2-proxy** (the
`github` provider, org/team allowlist). The MCP server holds no IdP secret and validates no
token — the ingress + oauth2-proxy is the trust boundary; the MCP Service is `ClusterIP`-
only (the anonymous-bypass guard).

## Outputs

`cluster_name`, `irsa_role_arn`, `ingress_class`, `mcp_url`, `grafana_url`, plus the
non-secret Helm-handoff config values (`region`, `image_tag`, the GitHub OAuth allowlist)
the chart consumes. The two secrets the chart needs (GitHub OAuth client secret + Slack
webhook) are deliberately NOT outputs — a sensitive output still lands in Terraform state
in plaintext, so the operator pipes them straight into the out-of-band Kubernetes Secrets.
