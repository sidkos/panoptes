# Panoptes hosting module — nginx-ingress + cert-manager prerequisites (decision #3).
#
# The auth + TLS prerequisites for the hostname (decision #3 — nginx-ingress + cert-manager
# + Let's Encrypt, NOT ALB/ACM): GitHub is OAuth2 and does NOT expose OIDC discovery, so
# ALB-native authenticate-oidc cannot gate it; the nginx external-auth (forward-auth) →
# oauth2-proxy(github) chain is the canonical GitHub-org gate (the chart wires the
# forward-auth annotations + oauth2-proxy in Phase 7). cert-manager + Let's Encrypt issues
# the TLS cert for `var.hostname`.
#
# These are Helm releases of the upstream controllers (the prerequisites the chart's Ingress
# depends on). They validate offline (the helm provider resolves nothing at validate time).

# The nginx-ingress CONTROLLER — the cluster's ingress data plane. The Panoptes Ingress
# (Phase 7) carries the nginx forward-auth annotations that point at oauth2-proxy; this
# release installs the controller that honors them.
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true

  # Pin the chart version for reproducible installs (an immutable-pin discipline mirroring
  # the image-tag pin, Risk K11).
  version = "4.11.3"

  # The controller's Service is a LoadBalancer (the ONLY public path); the MCP Service stays
  # ClusterIP (decision #5 — the anonymous-bypass guard, asserted in the Phase-7 Helm test).
  set {
    name  = "controller.service.type"
    value = "LoadBalancer"
  }

  depends_on = [aws_eks_node_group.panoptes]
}

# cert-manager — issues + renews the Let's Encrypt TLS cert for `var.hostname` (decision #3,
# NOT ACM). The ClusterIssuer (a cert-manager CRD) is created post-install by the chart;
# this release installs the cert-manager controller + CRDs it depends on.
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true

  version = "v1.16.1"

  # Install the CRDs so the ClusterIssuer (Let's Encrypt) the chart adds resolves.
  set {
    name  = "installCRDs"
    value = "true"
  }

  depends_on = [aws_eks_node_group.panoptes]
}
