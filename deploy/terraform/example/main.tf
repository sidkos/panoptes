# Worked root config invoking the Panoptes hosting module.
#
# Copy this dir, fill `terraform.tfvars` (see terraform.tfvars.example), and run
# `terraform init && terraform apply`. The providers are configured HERE (the root's job —
# the module declares only its version constraints, not provider config).
#
# Hermetic note: this root is a WORKED EXAMPLE; CI never `apply`s it (no live creds). The
# module itself is the validated artifact (`tests/terraform/`); this shows the invocation.

terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }
}

provider "aws" {
  region = var.home_region
}

# The helm provider targets the EKS cluster the module creates. After the first apply
# creates the cluster, the operator wires this provider to the cluster's kubeconfig (the
# two-phase apply EKS bootstraps commonly use); the local kubeconfig context is shown here
# as the standard pattern an operator updates via `aws eks update-kubeconfig`.
provider "helm" {
  kubernetes {
    config_path = "~/.kube/config"
  }
}

provider "tls" {}

module "panoptes" {
  source = "../../../modules/stack"

  home_region = var.home_region
  hostname    = var.hostname
  image_tag   = var.image_tag

  github_oauth_client_id = var.github_oauth_client_id
  github_org             = var.github_org
  github_team            = var.github_team

  read_role_arns  = var.read_role_arns
  alert_topic_arn = var.alert_topic_arn
}

output "cluster_name" {
  description = "The dedicated EKS cluster name."
  value       = module.panoptes.cluster_name
}

output "irsa_role_arn" {
  description = "The IRSA role ARN (annotate the collector + MCP service accounts with it)."
  value       = module.panoptes.irsa_role_arn
}

output "mcp_url" {
  description = "The public, GitHub-gated MCP HTTP URL."
  value       = module.panoptes.mcp_url
}

output "grafana_url" {
  description = "The public, GitHub-gated Grafana URL."
  value       = module.panoptes.grafana_url
}
