# Panoptes hosting module — the dedicated EKS control plane + the cluster's OIDC provider.
#
# The DEDICATED EKS cluster (decision #1 — Panoptes' OWN cluster, never an observed one) in
# the dedicated VPC. The cluster's OIDC provider (`aws_iam_openid_connect_provider`) is the
# IRSA TRUST ANCHOR: it lets the IRSA role (irsa.tf) trust the cluster-issued projected SA
# tokens, scoped to the collector + MCP service accounts. NOTE: this OIDC provider is K8s/
# IRSA MECHANICS — it is NOT the user-auth IdP (that is GitHub via oauth2-proxy at the
# ingress, decision #5). The two are unrelated trust systems.

# The control-plane IAM role: assumed by the EKS service to manage the cluster. It carries
# only the AWS-managed EKS cluster policy — no Panoptes data-plane grants.
resource "aws_iam_role" "cluster" {
  name = "panoptes-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "panoptes" {
  name     = "panoptes"
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids = local.panoptes_subnet_ids
  }

  # The control-plane policy attachment must exist before the cluster is created.
  depends_on = [aws_iam_role_policy_attachment.cluster]

  tags = {
    ManagedBy = "panoptes-terraform"
    # The dedicated-cluster marker — Panoptes' own cluster, never an observed one.
    PanoptesDedicated = "true"
  }
}

# The cluster's TLS cert (its OIDC issuer endpoint), used to derive the OIDC provider
# thumbprint. Read from the live cluster issuer — purely cluster mechanics for IRSA.
data "tls_certificate" "cluster_oidc" {
  url = aws_eks_cluster.panoptes.identity[0].oidc[0].issuer
}

# The cluster's OIDC provider — the IRSA TRUST ANCHOR. The IRSA role's trust policy
# (irsa.tf) references THIS provider's ARN + issuer URL, with a `:sub` condition pinning
# to exactly the collector + MCP service accounts. This is what makes the assume-role
# credential SA-scoped (K9) rather than node-wide.
resource "aws_iam_openid_connect_provider" "cluster" {
  url             = aws_eks_cluster.panoptes.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster_oidc.certificates[0].sha1_fingerprint]

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

# The OIDC issuer host (without the `https://` scheme) — the `:sub`/`:aud` condition keys
# in the IRSA trust policy are `<issuer-host>:sub` / `<issuer-host>:aud`.
locals {
  oidc_issuer_url  = aws_eks_cluster.panoptes.identity[0].oidc[0].issuer
  oidc_issuer_host = replace(local.oidc_issuer_url, "https://", "")
}
