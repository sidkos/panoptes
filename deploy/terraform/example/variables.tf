# Root variables for the worked example — forwarded to the module. Defaults keep the root
# config valid+plannable out of the box; the operator overrides via terraform.tfvars.

variable "home_region" {
  description = "AWS region for the dedicated Panoptes cluster (SAME account as the observed infra)."
  type        = string
  default     = "us-east-1"
}

variable "hostname" {
  description = "The public hostname for the Panoptes ingress (TLS via cert-manager + Let's Encrypt)."
  type        = string
  default     = "panoptes.example.com"
}

variable "image_tag" {
  description = "The immutable GHCR image tag to deploy (never `:latest`)."
  type        = string
  default     = "v0.2.0"
}

variable "github_oauth_client_id" {
  description = "GitHub OAuth app client id for oauth2-proxy."
  type        = string
  default     = ""
}

variable "github_oauth_client_secret" {
  description = "GitHub OAuth app client secret for oauth2-proxy (sensitive)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_org" {
  description = "The GitHub org allowlist that gates access."
  type        = string
  default     = ""
}

variable "github_team" {
  description = "Optional GitHub team allowlist (within the org)."
  type        = string
  default     = ""
}

variable "read_role_arns" {
  description = "The per-env in-account PanoptesReadRole ARNs the IRSA role may assume (empty = no grants)."
  type        = list(string)
  default     = []
}

variable "alert_topic_arn" {
  description = "The single Panoptes-owned SNS alert topic ARN (resource-scoped publish)."
  type        = string
  default     = ""
}

variable "slack_webhook_url" {
  description = "The Slack incoming-webhook URL for alert delivery (sensitive)."
  type        = string
  default     = ""
  sensitive   = true
}
