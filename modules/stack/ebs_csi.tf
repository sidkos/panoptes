# Panoptes hosting module — the EBS CSI driver (persistent storage for the VM StatefulSet).
#
# A live deploy surfaced that the module installed NO storage driver: on k8s 1.30+ the in-tree
# `kubernetes.io/aws-ebs` provisioner (the default gp2 StorageClass) is gone, so the
# VictoriaMetrics StatefulSet's PVC stayed Pending ("0/N nodes available: unbound immediate
# PersistentVolumeClaims"). This installs the EKS-managed aws-ebs-csi-driver addon — with its
# OWN IRSA role (the controller needs EBS CreateVolume/Attach; the node-instance-role-via-IMDS
# path is unreliable behind the default IMDS hop limit, which CrashLooped the controller in
# testing) — and makes a gp3 CSI StorageClass the cluster default (unmarking the now-defunct
# in-tree gp2), so any PVC-using workload provisions storage out of the box.

# --- IRSA role for the EBS CSI controller SA (kube-system:ebs-csi-controller-sa) -----

data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    sid     = "PanoptesEbsCsiOidcTrust"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.cluster.arn]
    }

    # Scoped to EXACTLY the EBS CSI controller SA (a `StringEquals`, no wildcard) — no other pod
    # can assume this role, the same least-privilege OIDC-trust posture as the app IRSA role.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "panoptes-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

# The AWS-managed EBS CSI driver policy (the CreateVolume/Attach/Detach/Delete + snapshot verbs
# the driver needs; AWS scopes them to volumes the driver tags). The module adds nothing broader.
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# --- the EKS-managed addon, bound to the IRSA role above ----------------------------

resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.panoptes.name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi.arn

  # Let the module own the addon's SA annotation (the IRSA role) on create + update.
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # The driver's controller schedules on the node group, so the nodes must exist first.
  depends_on = [aws_eks_node_group.panoptes]

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

# --- a gp3 CSI StorageClass as the cluster default (replacing the defunct in-tree gp2) ----

resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  reclaim_policy         = "Delete"
  allow_volume_expansion = true

  parameters = {
    type = "gp3"
  }

  # The driver must be installed before its StorageClass references its provisioner.
  depends_on = [aws_eks_addon.ebs_csi]
}

# EKS still creates an in-tree gp2 StorageClass marked default, but its provisioner
# (kubernetes.io/aws-ebs) is removed on modern k8s — leaving TWO defaults, one of them broken.
# Patch gp2 to NOT be default so the gp3 CSI class above is the sole, working default.
resource "kubernetes_annotations" "gp2_not_default" {
  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  metadata {
    name = "gp2"
  }
  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "false"
  }
  force = true

  depends_on = [kubernetes_storage_class_v1.gp3]
}
