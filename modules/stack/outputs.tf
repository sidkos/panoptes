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

# --- Helm-install handoff: the NON-SECRET config values the chart consumes -----------
#
# These surface the module's non-secret inputs (image tag, region, the GitHub OAuth
# allowlist) so the deploy wiring reads one source of truth. The two SECRETS the chart
# needs — the GitHub OAuth client secret and the Slack webhook — are deliberately NOT
# outputs: a sensitive output is still written to the Terraform state file in plaintext
# (it is only redacted in console output), so anyone with state read could recover them.
# The operator pipes those two values straight into the out-of-band Kubernetes Secrets the
# chart references (`panoptes-oauth2-proxy` / `panoptes-app-secrets`), never via TF state.

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

output "github_org" {
  description = "The GitHub org allowlist the chart wires into oauth2-proxy (decision #5)."
  value       = var.github_org
}

output "github_team" {
  description = "The optional GitHub team allowlist the chart wires into oauth2-proxy (decision #5)."
  value       = var.github_team
}
