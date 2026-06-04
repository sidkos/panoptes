# Panoptes hosting module — input variables.
#
# The full v0.2 input surface (spec § Locked decisions + § Directory Layout). Defaults keep
# the dedicated-VPC, same-account, failure-domain-independent posture (decision #1) and the
# single small MANAGED spot node group (decision #2). Secrets are marked `sensitive = true`
# so Terraform redacts them in plan/apply output.

variable "home_region" {
  description = "AWS region for the dedicated Panoptes hosting cluster (SAME account as the observed infra — decision #1; never an observed cluster's region by accident, failure-domain independence is a deliberate dedicated VPC + cluster, not co-tenancy)."
  type        = string
  default     = "us-east-1"
}

# --- VPC (dedicated, same account — decision #1, failure-domain independence) ------

variable "create_vpc" {
  description = "Provision a DEDICATED VPC for the Panoptes cluster (the default + recommended posture). Set false only to attach to a pre-created, NON-observed VPC in the same account — NEVER an observed workload's VPC (decision #1, K12 failure-domain independence)."
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated Panoptes VPC. A private /16 distinct from any observed VPC's range so the network failure domain is independent."
  type        = string
  default     = "10.180.0.0/16"
}

# --- EKS control plane + node group (single small MANAGED spot group — decision #2) -

variable "cluster_version" {
  description = "The EKS control-plane Kubernetes version. Pin a version in EKS STANDARD support — EXTENDED support bills ~6x the standard rate (~$0.60 vs ~$0.10/cluster-hr). Per `aws eks describe-cluster-versions` (checked 2026-06): standard support ends 2026-07-29 (1.33), 2026-12-02 (1.34), 2027-03-27 (1.35, the EKS default), 2027-08-02 (1.36). Pinned to 1.34 — AWS's recommended upgrade and ONE minor above the deploy-validated 1.33, so the pinned ingress-nginx (4.11.3) + cert-manager (1.16.1) charts in ingress.tf stay compatible. Going to 1.35/1.36 for longer runway ALSO needs those chart versions bumped (their tested k8s range is the real ceiling) + a re-validation deploy."
  type        = string
  default     = "1.34"
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "REQUIRED allowlist of CIDRs that may reach the EKS PUBLIC API server endpoint. There is deliberately NO default: the AWS default is 0.0.0.0/0 (the whole internet), so an implicit value is the HIGH-severity exposure this variable closes. Set it to your operator IP/CIDR (e.g. [\"203.0.113.4/32\"]). Nodes always reach the API over the PRIVATE endpoint (endpoint_private_access=true in eks.tf), so this gates only human/CI kubectl access."
  type        = list(string)

  validation {
    condition = length(var.cluster_endpoint_public_access_cidrs) > 0 && alltrue([
      for cidr in var.cluster_endpoint_public_access_cidrs : cidr != "0.0.0.0/0" && cidr != "::/0"
    ])
    error_message = "cluster_endpoint_public_access_cidrs must be a NON-EMPTY allowlist that does NOT contain 0.0.0.0/0 or ::/0 — an empty list falls back to, and a wildcard explicitly re-creates, the wide-open public API endpoint this guards. Use a specific operator IP/CIDR (e.g. [\"203.0.113.4/32\"])."
  }
}

variable "node_instance_type" {
  description = "Instance type for the single small managed node group (decision #2: a fixed, small, always-on stack — store + Grafana + collector + MCP + proxy — nothing to autoscale). A small default keeps cost discipline."
  type        = string
  default     = "t4g.small"
}

variable "ami_type" {
  description = "EKS managed-node-group AMI type. MUST match node_instance_type's CPU architecture: the default node_instance_type (t4g.small) is ARM/Graviton, so this defaults to the ARM AL2023 AMI. Override to AL2023_x86_64_STANDARD if node_instance_type is an x86 family (e.g. t3.small) — a mismatch makes EKS reject CreateNodegroup with InvalidParameterException at apply time (terraform validate/plan cannot catch it)."
  type        = string
  default     = "AL2023_ARM_64_STANDARD"
}

variable "node_min" {
  description = "Minimum node count for the managed node group. Defaults to 2: a live deploy showed the FULL stack (VictoriaMetrics + Grafana + collector + MCP + oauth2-proxy + the nginx-ingress/cert-manager/ebs-csi controllers + kube-system) is ~16 pods, which exceeds a single t4g.small's ~11-pod ENI cap. Two small spot nodes (still cost-disciplined, ~$8/mo) hold the stack with headroom across the two private AZs; raising node_instance_type or enabling VPC-CNI prefix delegation is the alternative if a single node is required."
  type        = number
  default     = 2
}

variable "node_max" {
  description = "Maximum node count for the managed node group (headroom of 1 over node_min for a rolling node replacement; NOT an autoscaling target — decision #2 is no Karpenter)."
  type        = number
  default     = 3
}

variable "capacity_type" {
  description = "Capacity type for the managed node group: SPOT (default, cost-disciplined) or ON_DEMAND. Single-AZ spot is acceptable for a dev/home monitoring stack (decision #2)."
  type        = string
  default     = "SPOT"
}

# --- Ingress + TLS + GitHub SSO (nginx + cert-manager + oauth2-proxy — decision #3/5) -

variable "hostname" {
  description = "The public hostname for the Panoptes ingress (e.g. panoptes.example.com). TLS is issued by cert-manager + Let's Encrypt (decision #3 — NOT ACM); the nginx ingress + oauth2-proxy GitHub-gate it (decision #5)."
  type        = string
  default     = "panoptes.example.com"
}

variable "github_oauth_client_id" {
  description = "GitHub OAuth app client id for oauth2-proxy's `github` provider (decision #5). The MCP server holds no IdP secret — oauth2-proxy is the auth boundary."
  type        = string
  default     = ""
}

variable "github_org" {
  description = "The GitHub ORG allowlist that gates access via oauth2-proxy (decision #5 — the org/team allowlist is the access boundary)."
  type        = string
  default     = ""
}

variable "github_team" {
  description = "Optional GitHub TEAM allowlist (within `github_org`) for a finer access gate. Empty = org-wide access."
  type        = string
  default     = ""
}

# --- Image pin (immutable tag — Risk K11) ------------------------------------------

variable "image_tag" {
  description = "The immutable GHCR image tag the cluster runs (e.g. a `v0.2.x` release tag). Pinned to an immutable tag, never a moving `:latest` ref, so a republish cannot silently drift the running cluster (Risk K11). REQUIRED (no default) — a no-default var forces the operator to pin an immutable tag rather than fall back to a moving ref."
  type        = string
}

# --- IRSA read scope + the single write grant (the security core — decisions #1/#6) -

variable "read_role_arns" {
  description = "The per-env `PanoptesReadRole/<env>` ARNs the IRSA role may `sts:AssumeRole`, IN-ACCOUNT (decision #1 — no cross-account trust; decision #6 — Panoptes does NOT create these, the observed-side IaC owns them). Default `[]` so stage/prod stay disabled stubs that produce ZERO assume-role grants until a non-empty list is supplied (no code change)."
  type        = list(string)
  default     = []
}

variable "alert_topic_arn" {
  description = "The single Panoptes-OWNED SNS alert topic ARN. The IRSA role grants `sns:Publish` on THIS ARN ONLY (resource-scoped — the one write grant in the system, on a Panoptes-owned resource). Empty = no publish grant."
  type        = string
  default     = ""
}

# --- Kubernetes namespace + service-account names (the IRSA trust-scope subjects) ---
#
# These pin the EXACT `system:serviceaccount:<namespace>:<sa>` subjects the IRSA trust
# policy's `StringEquals` condition binds to (irsa.tf). Defaulted so the module is
# self-contained; the Helm chart (Phase 7) mounts the collector + MCP pods under exactly
# these SA names in this namespace, so the IRSA credential is scoped to those two SAs only.

variable "namespace" {
  description = "The Kubernetes namespace the Panoptes collector + MCP service accounts live in. The IRSA trust policy binds to `system:serviceaccount:<namespace>:<sa>` for exactly the collector + MCP SAs."
  type        = string
  default     = "panoptes"
}

variable "collector_service_account" {
  description = "The collector pod's Kubernetes ServiceAccount name. The IRSA trust policy's `:sub` StringEquals pins to this SA (in `namespace`) — scoping the assume-role credential to the collector pod only (K9)."
  type        = string
  default     = "panoptes-collector"
}

variable "mcp_service_account" {
  description = "The MCP pod's Kubernetes ServiceAccount name. The IRSA trust policy's `:sub` StringEquals pins to this SA (in `namespace`) — scoping the assume-role credential to the MCP pod only (K9)."
  type        = string
  default     = "panoptes-mcp"
}
