# Panoptes hosting module — dedicated EKS cluster (PLACEHOLDER).
#
# Phase-0 SKELETON: the real dedicated EKS control plane + the cluster's OIDC provider
# (the IRSA trust anchor), the managed node group, the SA-scoped IRSA role, and the
# nginx-ingress + cert-manager prerequisites all land in Phase 6 (spec § Rollout Phases
# Phase 6 + § Directory Layout `modules/stack/{eks,nodegroup,irsa,ingress,vpc}.tf`).
#
# For now a single `null_resource` placeholder makes the module a VALID, non-empty
# configuration so the hermetic `terraform init -backend=false` + `validate` + `tflint`
# gate exercises a real (if trivial) resource graph. It provisions nothing on apply.
# `triggers` echoes the load-bearing inputs purely so the placeholder re-plans when they
# change — documenting the Phase-6 dependency wiring without yet creating cloud resources.
resource "null_resource" "placeholder" {
  triggers = {
    home_region = var.home_region
    create_vpc  = tostring(var.create_vpc)
    image_tag   = var.image_tag
  }
}
