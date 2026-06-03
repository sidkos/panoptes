# Panoptes hosting module — outputs (the handles the Helm install + operator consume).

output "cluster_name" {
  description = "The dedicated EKS cluster name — passed to `kubectl`/Helm to target the cluster."
  value       = aws_eks_cluster.panoptes.name
}

output "irsa_role_arn" {
  description = "The IRSA role ARN. The Helm chart annotates the collector + MCP service accounts with `eks.amazonaws.com/role-arn = <this>` so their pods assume the SA-scoped role (K9)."
  value       = aws_iam_role.irsa.arn
}

output "ingress_class" {
  description = "The ingress class the Panoptes Ingress uses — `nginx` (decision #3; the GitHub forward-auth annotations are honored by the nginx-ingress controller this module installs)."
  value       = "nginx"
}

output "mcp_url" {
  description = "The public MCP HTTP URL (GitHub-gated at the nginx ingress; the MCP Service itself is ClusterIP-only — decision #5)."
  value       = "https://${var.hostname}/mcp"
}

output "grafana_url" {
  description = "The public Grafana URL (GitHub-gated at the same nginx ingress)."
  value       = "https://${var.hostname}/grafana"
}

# --- Helm-install handoff: the config values the chart (Phase 7) consumes -----------
#
# These pass the module's input config through to the Helm install (the chart's
# values.yaml binds them: the image tag, the GitHub OAuth allowlist for oauth2-proxy, the
# Slack webhook for the notifier). Surfaced as outputs so the deploy wiring reads ONE
# source of truth (the module's resolved inputs), and so the inputs are genuinely consumed.

output "region" {
  description = "The AWS region the cluster runs in (the operator targets it with kubectl/Helm)."
  value       = var.home_region
}

output "image_tag" {
  description = "The immutable GHCR image tag the Helm chart deploys (Risk K11 — pinned, never `:latest`)."
  value       = var.image_tag
}

output "github_oauth_client_id" {
  description = "The GitHub OAuth client id the chart wires into oauth2-proxy (decision #5)."
  value       = var.github_oauth_client_id
}

output "github_oauth_client_secret" {
  description = "The GitHub OAuth client secret the chart wires into oauth2-proxy (decision #5). Sensitive — redacted in output."
  value       = var.github_oauth_client_secret
  sensitive   = true
}

output "github_org" {
  description = "The GitHub org allowlist the chart wires into oauth2-proxy (decision #5)."
  value       = var.github_org
}

output "github_team" {
  description = "The optional GitHub team allowlist the chart wires into oauth2-proxy (decision #5)."
  value       = var.github_team
}

output "slack_webhook_url" {
  description = "The Slack webhook the slack notifier delivers alerts to (a Panoptes-owned sink). Sensitive — redacted in output."
  value       = var.slack_webhook_url
  sensitive   = true
}
