# Panoptes hosting module — input variables.
#
# Phase-0 SKELETON: only the load-bearing inputs needed for a valid module are declared
# now. The full variable surface (node sizing, the GitHub OAuth allowlist, the read-role
# ARN list, the alert topic ARN, the Slack webhook, the hostname) is finalized in Phase 6
# alongside the resources that consume them. Defaults keep the dedicated-VPC,
# same-account, failure-domain-independent posture (decision #1) front-and-center.

variable "home_region" {
  description = "AWS region for the dedicated Panoptes hosting account/cluster (SAME account as the observed infra; NEVER an observed cluster's region by accident — failure-domain independence is a deliberate, dedicated VPC + cluster, not co-tenancy)."
  type        = string
  default     = "us-east-1"
}

variable "create_vpc" {
  description = "Provision a DEDICATED VPC for the Panoptes cluster (the default and recommended posture). Set false only to attach to a pre-created, NON-observed VPC in the same account — never an observed workload's VPC (decision #1, failure-domain independence)."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "The immutable GHCR image tag the cluster runs (e.g. a `v0.2.x` release tag). Pinned to an immutable tag, never a moving `:latest` ref, so a republish cannot silently drift the running cluster (Risk K11)."
  type        = string
  default     = "latest"
}
