# Panoptes hosting module — ONE small MANAGED node group (decision #2 — NOT Karpenter).
#
# A single small EKS MANAGED node group (decision #2): the stack is a fixed, small,
# always-on set of workloads (store + Grafana + collector + MCP + proxy) — there is nothing
# to autoscale, so a managed node group is the minimal-ops, minimal-cost choice. Karpenter's
# controller + provisioner buys nothing for a static one-node stack and would undercut cost
# discipline — so there is NO Karpenter resource anywhere in this module. SPOT capacity
# (default) + single-AZ are acceptable for a dev/home monitoring stack.

# The node IAM role: the worker identity. It carries the three AWS-managed EKS worker
# policies (node, CNI, ECR read) and NOTHING Panoptes-data-plane — the pods get their read
# scope via IRSA (irsa.tf), never the node role (that would over-grant to any co-scheduled
# pod, the exact anti-pattern IRSA fixes).
resource "aws_iam_role" "node" {
  name = "panoptes-eks-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# The single small managed node group. `capacity_type` defaults to SPOT (cost-disciplined);
# scaling is a FIXED small range (min 1 / max 2) for rolling replacement, NOT an autoscaling
# target — there is no Karpenter, no cluster-autoscaler. One instance type keeps the spot
# pool simple for a one-node stack.
resource "aws_eks_node_group" "panoptes" {
  cluster_name    = aws_eks_cluster.panoptes.name
  node_group_name = "panoptes"
  node_role_arn   = aws_iam_role.node.arn
  # Nodes run in the PRIVATE subnets only — no public IPs, egress via the NAT gateway
  # (vpc.tf). This is the HIGH-severity fix: the kubelet is never internet-routable.
  subnet_ids = local.panoptes_private_subnet_ids

  capacity_type  = var.capacity_type
  instance_types = [var.node_instance_type]
  # The AMI architecture MUST match the instance family (ARM for the t4g/Graviton
  # default). Without an explicit ami_type, EKS picks the x86_64 AL2023 AMI and
  # rejects an ARM instance type at CreateNodegroup — a failure terraform plan
  # cannot surface (only the EKS API validates the pairing).
  ami_type = var.ami_type

  scaling_config {
    desired_size = var.node_min
    min_size     = var.node_min
    max_size     = var.node_max
  }

  # The worker policy attachments must exist before the node group joins the cluster.
  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
    # Private-subnet nodes need NAT egress (image pulls, ECR/STS) to join — the NAT route
    # must exist before the nodes boot, or they never reach Ready.
    aws_route_table_association.private,
  ]

  tags = {
    ManagedBy = "panoptes-terraform"
  }
}
