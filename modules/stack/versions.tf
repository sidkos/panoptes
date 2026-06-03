# Panoptes hosting module — provider/runtime version constraints.
#
# The provider set the v0.2 dedicated-EKS module needs: `aws` (VPC + EKS + IAM/IRSA + SNS
# scope), `helm` (the nginx-ingress + cert-manager prerequisite releases), and `tls` (the
# cluster OIDC provider thumbprint). Runner-agnostic: these constraints validate under both
# Terraform and OpenTofu.

terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    # Pinned to the 2.x line: the module uses the nested `set { name = ... value = ... }`
    # block form, which the helm provider 3.x replaced with a `set = [{...}]` list — pinning
    # 2.x keeps the ingress.tf release blocks valid.
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    # `tls` backs `data.tls_certificate` for the cluster OIDC provider thumbprint (eks.tf).
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }
}
