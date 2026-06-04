# Panoptes hosting module — the DEDICATED VPC (failure-domain independence, K12).
#
# Panoptes runs in its OWN dedicated VPC (decision #1) — NEVER an observed cluster's VPC —
# so the network failure domain is independent of anything Panoptes monitors ("don't die
# with what you monitor"). The VPC is in the SAME AWS account as the observed infra
# (decision #1; the shared-account control-plane blast radius is the documented, accepted
# trade). `create_vpc=true` (the default) provisions it here; set false ONLY to attach to a
# pre-created NON-observed VPC in the same account.
#
# Topology (HARDENED — workloads are NOT internet-exposed): a PUBLIC subnet pair holds only
# the internet-facing nginx LoadBalancer + the NAT gateway; a PRIVATE subnet pair holds the
# managed node group (no public IPs — the kubelet is never internet-routable). Private-subnet
# egress (image pulls, the EKS/ECR/STS endpoints) goes through a SINGLE NAT gateway — one NAT,
# not one-per-AZ, is the cost-disciplined choice for a small dev stack (decision #2). The EKS
# API endpoint is private to the nodes and CIDR-allowlisted (never 0.0.0.0/0) for operator
# kubectl — see eks.tf + var.cluster_endpoint_public_access_cidrs.
#
# `count = var.create_vpc ? 1 : 0` gates every VPC resource so the false case provisions
# nothing here. The plan-assertion test asserts a SEPARATE `aws_vpc` is created in the
# default (create_vpc=true) case — proving Panoptes never co-tenants an observed VPC.

# Two AZs satisfy EKS's control-plane subnet requirement without committing to multi-AZ HA
# workloads (decision #2 — a single small stack).
data "aws_availability_zones" "available" {
  count = var.create_vpc ? 1 : 0
  state = "available"
}

resource "aws_vpc" "panoptes" {
  count = var.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name      = "panoptes"
    ManagedBy = "panoptes-terraform"
    # The dedicated-VPC marker — this VPC is Panoptes' own, never an observed workload's.
    PanoptesDedicated = "true"
  }
}

resource "aws_internet_gateway" "panoptes" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.panoptes[0].id

  tags = {
    Name = "panoptes"
  }
}

# PUBLIC subnets (2, distinct AZs) — hold ONLY the internet-facing nginx LoadBalancer and the
# NAT gateway, never the workload nodes. Tagged `kubernetes.io/role/elb` so the in-tree cloud
# provider places PUBLIC (internet-facing) LoadBalancer Services here.
resource "aws_subnet" "public" {
  count = var.create_vpc ? 2 : 0

  vpc_id                  = aws_vpc.panoptes[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available[0].names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name                     = "panoptes-public-${count.index}"
    "kubernetes.io/role/elb" = "1"
    # The in-tree AWS cloud provider discovers ELB subnets via this per-cluster tag (no AWS
    # Load Balancer Controller is installed); without it the public nginx LB can fail to
    # provision (stuck <pending> EXTERNAL-IP), especially with a mixed public/private cluster
    # subnet set. `owned` = these subnets belong to this dedicated cluster exclusively.
    "kubernetes.io/cluster/panoptes" = "owned"
  }
}

# PRIVATE subnets (2, distinct AZs) — where the managed node group runs. No public IPs: the
# kubelet + workloads are NOT internet-routable (the HIGH-severity fix). Tagged
# `kubernetes.io/role/internal-elb` so any INTERNAL LoadBalancer lands here, never public.
resource "aws_subnet" "private" {
  count = var.create_vpc ? 2 : 0

  vpc_id                  = aws_vpc.panoptes[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone       = data.aws_availability_zones.available[0].names[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name                              = "panoptes-private-${count.index}"
    "kubernetes.io/role/internal-elb" = "1"
    # Per-cluster tag so the in-tree provider recognizes these as cluster subnets too (the
    # role/internal-elb tag keeps INTERNAL LBs here; public LBs go to the elb-tagged pair).
    "kubernetes.io/cluster/panoptes" = "owned"
  }
}

# A SINGLE NAT gateway (cost discipline — one, not one-per-AZ) giving the private subnets
# outbound internet for image pulls + the AWS API endpoints. Lives in public subnet 0.
resource "aws_eip" "nat" {
  count = var.create_vpc ? 1 : 0

  domain = "vpc"

  tags = {
    Name = "panoptes-nat"
  }
}

resource "aws_nat_gateway" "panoptes" {
  count = var.create_vpc ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags = {
    Name = "panoptes"
  }

  # The NAT's own public subnet must already route to the IGW, or the NAT has no egress
  # path — depend on the public route-table association (which transitively needs the IGW).
  depends_on = [aws_route_table_association.public]
}

# PUBLIC route table — 0.0.0.0/0 to the internet gateway; associated with the public subnets.
resource "aws_route_table" "public" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.panoptes[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.panoptes[0].id
  }

  tags = {
    Name = "panoptes-public"
  }
}

resource "aws_route_table_association" "public" {
  count = var.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# PRIVATE route table — 0.0.0.0/0 to the NAT gateway (outbound only, no inbound); associated
# with the private subnets so the nodes reach the internet WITHOUT being reachable from it.
resource "aws_route_table" "private" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.panoptes[0].id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.panoptes[0].id
  }

  tags = {
    Name = "panoptes-private"
  }
}

resource "aws_route_table_association" "private" {
  count = var.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# The subnet ids the EKS cluster + node group consume.
# - The CLUSTER gets BOTH pairs: control-plane ENIs span all subnets, and the elb-tagged
#   public pair is where the internet-facing nginx LoadBalancer lands.
# - The NODE GROUP gets ONLY the private pair (nodegroup.tf) — nodes never get a public IP.
locals {
  panoptes_public_subnet_ids  = var.create_vpc ? aws_subnet.public[*].id : []
  panoptes_private_subnet_ids = var.create_vpc ? aws_subnet.private[*].id : []
  panoptes_cluster_subnet_ids = concat(local.panoptes_public_subnet_ids, local.panoptes_private_subnet_ids)
}
