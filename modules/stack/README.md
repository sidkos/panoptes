# `modules/stack` — the Panoptes hosting module (dedicated EKS + IRSA + ingress)

The distributable Terraform module that provisions Panoptes' **dedicated** hosting cluster:
a dedicated VPC (public/private subnet pair, single NAT gateway), a dedicated EKS control
plane + the cluster's OIDC provider (the IRSA trust anchor), one small **managed** node
group in the private subnets, the **IRSA role** (SA-scoped to the collector + MCP service
accounts), the **EBS CSI driver** addon (+ its own IRSA role, gp3 default StorageClass), and
the **nginx-ingress + cert-manager** prerequisites.

Runner-agnostic — it validates and applies under both **Terraform** and **OpenTofu**. The
module is **deploy-proven end-to-end**: 5 live apply → verify → destroy cycles on a real AWS
account validated all 8 components functional out-of-box with zero orphaned resources on
teardown.

## Import

```hcl
module "panoptes" {
  source = "github.com/sidkos/panoptes//modules/stack"

  home_region     = "us-east-1"
  hostname        = "panoptes.example.com"
  image_tag       = "v0.2.0" # an IMMUTABLE tag, never :latest (Risk K11)

  # REQUIRED, no default. The EKS API server's public endpoint is restricted to this
  # allowlist (the private endpoint is always on for the nodes). The validation REJECTS an
  # empty list AND an explicit 0.0.0.0/0 / ::/0 — fail-closed, never the AWS-default
  # wide-open public endpoint.
  cluster_endpoint_public_access_cidrs = ["203.0.113.10/32"]

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

The dedicated VPC is split into a **public subnet pair** (the internet-facing nginx
LoadBalancer + a single cost-disciplined NAT gateway only) and a **private subnet pair**
where the managed node group runs (`map_public_ip_on_launch = false` — nodes are NOT
internet-routable; their egress is via the one NAT gateway, not per-AZ). Both pairs carry
`kubernetes.io/cluster/panoptes = owned`; public subnets are tagged
`kubernetes.io/role/elb` and private `kubernetes.io/role/internal-elb` so the in-tree cloud
provider discovers LoadBalancer subnets (no AWS Load Balancer Controller is installed). The
EKS API server endpoint is **hardened**: the private endpoint is always on (nodes use it),
and public access is restricted to the required `cluster_endpoint_public_access_cidrs`
allowlist (fail-closed validation — see [Inputs](#inputs)) — not the AWS-default
`0.0.0.0/0`. New always-on cost: ~$32/mo for the NAT gateway + 1 EIP.

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

## Inputs

Beyond the GitHub-SSO and IRSA grant inputs shown in [Import](#import):

- **`cluster_endpoint_public_access_cidrs`** — REQUIRED, no default. The CIDR allowlist for
  the EKS API server's public endpoint. The validation rejects an empty list AND an explicit
  `0.0.0.0/0` / `::/0` (fail-closed) — there is no way to silently get the AWS-default
  wide-open endpoint.
- **`node_min` / `node_max`** — default **2** / **3**. The full stack (VM / Grafana /
  collector / MCP / oauth2-proxy + nginx-ingress / cert-manager / ebs-csi controllers +
  kube-system) is ~16 pods, over one `t4g.small`'s ~11-pod ENI cap, so the floor is two
  small spot nodes (~$8/mo) across the two private AZs.
- **`cluster_version`** — default **1.34** (EKS standard support to 2026-12-02; pick a
  STANDARD-support version via `aws eks describe-cluster-versions` — extended support bills
  ~6x; the pinned ingress-nginx 4.11.3 + cert-manager 1.16.1 chart versions cap the safe
  k8s ceiling).
- **`ami_type`** — defaults to `AL2023_ARM_64_STANDARD` to match the ARM `t4g.small` node.

### Provider requirements

In addition to the `helm` provider, the module requires the `hashicorp/kubernetes` provider
(it creates the gp3 default StorageClass and patches the in-tree gp2 class to non-default).
Like `helm`, it wires to `~/.kube/config` — a **two-phase apply** (control plane first, then
the in-cluster resources). The worked root example wires both.

### Storage (EBS CSI driver)

The module installs the EKS-managed `aws-ebs-csi-driver` addon with its **own IRSA role**
(scoped to `kube-system:ebs-csi-controller-sa`) and a **gp3 CSI StorageClass set as the
cluster default** (unmarking the now-defunct in-tree gp2). This is required for PVCs on
k8s 1.30+ — the in-tree `kubernetes.io/aws-ebs` provisioner is gone, so without the addon the
VictoriaMetrics PVC stays `Pending`.

## Outputs

`cluster_name`, `irsa_role_arn`, `ingress_class`, `mcp_url`, `grafana_url`, plus the
non-secret Helm-handoff config values (`region`, `image_tag`, the GitHub OAuth allowlist)
the chart consumes. The two **sensitive** outputs that previously existed (GitHub OAuth
client secret + Slack webhook) were **removed** — a sensitive output still lands in
Terraform state in plaintext and had no consumer (the chart reads those secrets from
out-of-band Kubernetes Secrets). The non-secret handoff outputs stay.
