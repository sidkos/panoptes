# Panoptes hosting module — provider/runtime version constraints.
#
# Phase-0 SKELETON: a minimal, valid Terraform module so the hermetic
# `init -backend=false` + `validate` + `tflint` gate passes. The real dedicated-EKS
# resources (VPC, EKS control plane + cluster OIDC provider, managed node group, the
# SA-scoped IRSA role, nginx-ingress + cert-manager prerequisites, resource-scoped
# sns:Publish) land in Phase 6. Runner-agnostic: the constraints below validate under
# both Terraform and OpenTofu.

terraform {
  required_version = ">= 1.3"

  # Only the providers the Phase-6 resources actually need are declared here. The
  # `null` provider backs the Phase-0 placeholder; `aws`/`helm` are pinned now so the
  # version contract is stable before the real resources arrive.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.13"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2"
    }
  }
}
