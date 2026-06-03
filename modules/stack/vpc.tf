# Panoptes hosting module — the DEDICATED VPC (failure-domain independence, K12).
#
# Panoptes runs in its OWN dedicated VPC (decision #1) — NEVER an observed cluster's VPC —
# so the network failure domain is independent of anything Panoptes monitors ("don't die
# with what you monitor"). The VPC is in the SAME AWS account as the observed infra
# (decision #1; the shared-account control-plane blast radius is the documented, accepted
# trade). `create_vpc=true` (the default) provisions it here; set false ONLY to attach to a
# pre-created NON-observed VPC in the same account.
#
# `count = var.create_vpc ? 1 : 0` gates every VPC resource so the false case provisions
# nothing here (the operator wires the pre-created subnets via the data sources a downstream
# revision would add). The plan-assertion test asserts a SEPARATE `aws_vpc` is created in
# the default (create_vpc=true) case — proving Panoptes never co-tenants an observed VPC.

# A small list of AZs to spread the (single, small) subnets across; single-AZ is acceptable
# for a dev/home stack (decision #2), but two subnets in distinct AZs satisfy EKS's
# control-plane subnet requirement without committing to multi-AZ HA workloads.
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

# Two public subnets in distinct AZs (the minimal EKS subnet topology). A single-AZ stack
# is acceptable for the workloads (decision #2); two control-plane subnets keep the EKS
# cluster creatable without a multi-AZ HA commitment.
resource "aws_subnet" "panoptes" {
  count = var.create_vpc ? 2 : 0

  vpc_id                  = aws_vpc.panoptes[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available[0].names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "panoptes-${count.index}"
    # Tag the subnets for EKS so the control plane + nodes discover them.
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_route_table" "panoptes" {
  count = var.create_vpc ? 1 : 0

  vpc_id = aws_vpc.panoptes[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.panoptes[0].id
  }

  tags = {
    Name = "panoptes"
  }
}

resource "aws_route_table_association" "panoptes" {
  count = var.create_vpc ? 2 : 0

  subnet_id      = aws_subnet.panoptes[count.index].id
  route_table_id = aws_route_table.panoptes[0].id
}

# The subnet ids the EKS cluster + node group consume — read from the created subnets here
# (a downstream revision can swap in a data source for the create_vpc=false attach case).
locals {
  panoptes_subnet_ids = var.create_vpc ? aws_subnet.panoptes[*].id : []
}
