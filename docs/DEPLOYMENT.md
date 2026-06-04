# Panoptes — Deployment & Test Playbook

This document is the operator-facing runbook for standing up the **hosted**
Panoptes stack on its own dedicated EKS cluster, verifying every component is
functional, and tearing it down with zero orphans. It is the operational
companion to [`ARCHITECTURE.md`](ARCHITECTURE.md) (the topology + auth contract)
and [`IAM.md`](IAM.md) (the single read-only credential domain).

The stack is **deploy-proven end-to-end**: 5 live apply → verify → destroy cycles
on a real AWS account validated all 8 components functional out-of-box
(VictoriaMetrics on gp3 storage, the collector pipeline, MCP, Grafana with its
datasource, oauth2-proxy, the nginx-ingress ELB, the cert-manager ClusterIssuer,
and IRSA), with zero orphaned resources on teardown.

---

## 1. Prerequisites

Before the first apply, have all of these in hand:

- **AWS credentials to the *hosting* account.** The dedicated VPC + EKS cluster
  live in the same account as the observed infra (failure-domain independence is
  a dedicated VPC + cluster, *not* a separate account — see
  [`ARCHITECTURE.md`](ARCHITECTURE.md) §1). The principal needs EKS, EC2/VPC, IAM,
  and ELB create rights.
- **Terraform `>= 1.3`, Helm `>= 3`, and `kubectl`** on the operator workstation.
  The module wires the `helm` and `kubernetes` providers to `~/.kube/config`, so
  `kubectl` must be able to reach the cluster (§3).
- **A GitHub OAuth app** for *real* (SSO-gated) access. You need its **client id**
  and **client secret**, plus a GitHub **org** (and optionally a **team**) to use
  as the access allowlist. The OAuth app's callback URL must point at the
  deployed hostname's `/oauth2/callback` path. (For verification-only deploys you
  can stand the stack up without it and reach components via `kubectl
  port-forward` — §6 — but the public path is closed until it is wired.)
- **The container image.** The documented path is the **GHCR image** pinned to an
  immutable `v0.2.*` release tag (published by `publish.yml`). For self-service
  cluster *testing* you instead build and push to an **in-account ECR** repo (§4).
- **A DNS hostname for TLS.** cert-manager + Let's Encrypt issues the certificate
  for `var.hostname` (e.g. `panoptes.example.com`); the hostname must resolve to
  the nginx LoadBalancer's address for the ACME HTTP-01 challenge to succeed.

---

## 2. The two-phase Terraform apply

The module installs the controllers via the **`helm`** provider and creates the
**gp3 StorageClass** (plus the gp2 default-class patch) via the **`kubernetes`**
provider. Both providers wire to `~/.kube/config` — which does **not exist yet**
on the first run, because the cluster it points at has not been created.
Therefore the apply is **two-phase**: create the cluster + node group first, wire
kubeconfig, then apply the rest.

The required, no-default input is the EKS API allowlist:

```hcl
# terraform.tfvars
cluster_endpoint_public_access_cidrs = ["203.0.113.4/32"]  # YOUR operator IP/CIDR
hostname                             = "panoptes.example.com"
image_tag                            = "v0.2.0"
github_oauth_client_id               = "Iv1.xxxxxxxxxxxx"
github_org                           = "your-org"
```

> `cluster_endpoint_public_access_cidrs` has **no default** and is **validated** —
> it rejects an empty list *and* an explicit `0.0.0.0/0` / `::/0`. The AWS default
> for the public API endpoint is the wide-open `0.0.0.0/0`; this variable closes
> that exposure and fails **closed** if you forget it.

**Phase 1 — create the cluster + node group, then wire kubeconfig:**

```bash
terraform init

# Target the cluster control plane + the managed node group first. These have no
# dependency on a working kubeconfig, so they apply before the helm/kubernetes
# providers need to reach the cluster.
terraform apply \
  -target=module.panoptes.aws_eks_cluster.panoptes \
  -target=module.panoptes.aws_eks_node_group.panoptes

# Write a kubeconfig context for the new cluster so the helm + kubernetes
# providers (and your kubectl) can reach it.
aws eks update-kubeconfig --name panoptes --region us-east-1
```

**Phase 2 — full apply (controllers, EBS CSI driver, gp3 default SC, ingress):**

```bash
# Now that ~/.kube/config points at a live, reachable cluster, the full apply
# installs the EBS CSI driver addon, the gp3 default StorageClass (+ the gp2
# default-class patch), and the nginx-ingress + cert-manager Helm releases.
terraform apply
```

After this completes, capture the handoff outputs for the Helm install:

```bash
terraform output irsa_role_arn   # → helm --set irsaRoleArn=...
terraform output cluster_name region
```

---

## 3. The EKS API allowlist + the shifting-IP gotcha

This is the single most common failure mode of a fresh deploy, so it gets its own
section.

The EKS API server endpoint is **hardened**: `endpoint_private_access=true` (the
nodes reach the API over the **private** endpoint) and public access is
**restricted** to the `cluster_endpoint_public_access_cidrs` allowlist — never the
AWS-default `0.0.0.0/0`.

### The load-bearing rule: your operator IP must be in the allowlist

The Terraform `kubernetes`/`helm` providers and your `kubectl` reach the cluster
over the **public** endpoint. If your operator public IP is **not** in
`cluster_endpoint_public_access_cidrs`, those providers and `kubectl` simply time
out — the symptom is `Unable to connect to the server` / "cluster unreachable",
not an auth error. There is no fallback: the endpoint is firewalled to the
allowlist.

### When your IP changes

Office/home/VPN IPs shift. When yours does, the previously-working `kubectl` and
the next `terraform apply` both start timing out. To recover, either:

- update the var with the new CIDR and re-apply:

  ```bash
  # terraform.tfvars → cluster_endpoint_public_access_cidrs = ["NEW.IP.HERE/32"]
  terraform apply -target=module.panoptes.aws_eks_cluster.panoptes
  ```

- or patch the allowlist directly (out-of-band, faster):

  ```bash
  aws eks update-cluster-config --name panoptes \
    --resources-vpc-config publicAccessCidrs="NEW.IP.HERE/32",endpointPublicAccess=true
  ```

  (Reconcile the var afterward so Terraform doesn't revert it on the next apply.)

You **cannot** sidestep this by widening to `0.0.0.0/0` — the variable's
validation rejects the wildcard. That is intentional: the whole point of the
allowlist is that the public API endpoint is never open to the internet.

---

## 4. Building + pushing the image

There are two paths, for two different purposes.

**Documented (release) path — GHCR.** The published image is
`ghcr.io/sidkos/panoptes` on an immutable `v0.2.*` tag, produced by `publish.yml`
on a release. This is what `image_tag` / the chart's `image.tag` pin in normal
operation — never a moving `:latest` ref.

**Self-service cluster testing — in-account ECR.** For a throwaway test cluster
you usually don't have (or want to mint) a GHCR pull token or cut a release tag.
Build for the node architecture and push to an ECR repo in the same account — the
node IAM role already carries `AmazonEC2ContainerRegistryReadOnly`, so **no
pull-secret, no GHCR token, and no release tag are needed**:

```bash
# The default node group is ARM/Graviton (t4g.small + AL2023_ARM_64_STANDARD),
# so the image MUST be built for arm64.
aws ecr create-repository --repository-name panoptes --region us-east-1
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com

docker buildx build --platform linux/arm64 \
  -t <acct>.dkr.ecr.us-east-1.amazonaws.com/panoptes:test .
docker push <acct>.dkr.ecr.us-east-1.amazonaws.com/panoptes:test
```

Then point the Helm install at it (`--set image.repository=<acct>.dkr.ecr.us-east-1.amazonaws.com/panoptes --set image.tag=test`).

> Architecture must match: the default node group is **arm64**. A linux/amd64
> image will `CrashLoopBackOff` with an `exec format error` that no `helm
> template` could surface.

---

## 5. Helm install

With the cluster up and the image reachable, install the chart. The values fall
into three buckets: image override, the Terraform handoff, and the **two
out-of-band Kubernetes Secrets** (the chart references them by name; it never
inlines secret values).

**First, create the two Secrets** (out-of-band, never in Terraform state):

```bash
kubectl create namespace panoptes

# oauth2-proxy needs the GitHub OAuth client secret + a cookie secret (32 random
# bytes, base64). The chart references this by name (oauth2Proxy.existingSecretName).
kubectl -n panoptes create secret generic panoptes-oauth2-proxy \
  --from-literal=client-secret="$GITHUB_OAUTH_CLIENT_SECRET" \
  --from-literal=cookie-secret="$(openssl rand -base64 32)"

# The collector + MCP read app secrets (Slack webhook, Sentry token, assume-role
# ARN, …) from this Secret via envFrom. Referenced by name (config.secretRefName).
kubectl -n panoptes create secret generic panoptes-app-secrets \
  --from-literal=SLACK_WEBHOOK_URL="$SLACK_WEBHOOK_URL"
```

**Then install:**

```bash
helm install panoptes ./charts/panoptes -n panoptes \
  --set image.repository=<acct>.dkr.ecr.us-east-1.amazonaws.com/panoptes \
  --set image.tag=test \
  --set irsaRoleArn="$(terraform output -raw irsa_role_arn)" \
  --set hostname=panoptes.example.com \
  --set oauth2Proxy.githubOrg=your-org \
  --set oauth2Proxy.githubClientId=Iv1.xxxxxxxxxxxx \
  --set ingress.createClusterIssuer=true \
  --set ingress.acmeEmail=ops@example.com
```

Key value notes:

- **`irsaRoleArn`** is the Terraform `irsa_role_arn` output — it annotates the
  collector + MCP ServiceAccounts (`eks.amazonaws.com/role-arn`) so their pods
  assume the SA-scoped role. This is the *only* credential path; there are no
  mounted static keys.
- **`oauth2Proxy.githubOrg` / `githubClientId`** are the non-secret half of the
  GitHub gate; the client secret + cookie secret live in the
  `panoptes-oauth2-proxy` Secret above.
- **`ingress.createClusterIssuer`** is **opt-in (default `false`)** and creates
  the cert-manager Let's Encrypt ClusterIssuer the Ingress references. It is off
  by default because it needs the cert-manager CRDs (installed by the Terraform
  module) **and** a real ACME contact email — **`ingress.acmeEmail` is required**
  when you enable it (an empty email fails the render, and Let's Encrypt rejects
  an empty contact). An operator who manages their own issuer leaves this false
  and references it by name. While testing, set
  `ingress.acmeServer=https://acme-staging-v02.api.letsencrypt.org/directory` to
  avoid prod rate limits.

---

## 6. Verifying each component is functional

A green `helm install` only means the manifests applied. Verify each of the 8
components actually works — via `kubectl port-forward` from the operator box, or
an in-cluster `curl` from a throwaway pod. Do **not** trust "Running" alone.

```bash
# Pods + PVC bound (the gp3 default StorageClass must have provisioned the VM PVC):
kubectl -n panoptes get pods
kubectl -n panoptes get pvc        # VM PVC must be Bound, NOT Pending
kubectl get storageclass           # gp3 is (default); gp2 is NOT default
```

| # | Component | Check |
|---|-----------|-------|
| 1 | **VictoriaMetrics** | `kubectl -n panoptes port-forward svc/panoptes-victoriametrics 8428` → `curl localhost:8428/health` returns `OK`. PVC is **Bound** on gp3 (proves the EBS CSI driver works). |
| 2 | **MCP server** | `kubectl -n panoptes port-forward svc/panoptes-mcp 8080` → `curl localhost:8080/healthz` returns healthy. |
| 3 | **Collector pipeline** | Query VM for the collector's own heartbeat: `curl 'localhost:8428/api/v1/query?query=panoptes_health_up'` returns a non-empty result (the pipeline is scraping → normalizing → writing). |
| 4 | **Grafana + datasource** | `kubectl -n panoptes port-forward svc/panoptes-grafana 3000` → `curl localhost:3000/api/health` returns `ok`; the **VictoriaMetrics datasource** is auto-provisioned (the ConfigMap mounted at `/etc/grafana/provisioning/datasources`) — confirm in the Grafana datasources list. |
| 5 | **oauth2-proxy** | `kubectl -n panoptes port-forward deploy/panoptes-oauth2-proxy 4180` → `curl localhost:4180/ping` returns `OK`. |
| 6 | **nginx-ingress ELB** | `kubectl -n ingress-nginx get svc` shows an `EXTERNAL-IP` (the LoadBalancer provisioned — *not* stuck `<pending>`); then hit the public path: `curl -k https://panoptes.example.com/healthz`. |
| 7 | **cert-manager ClusterIssuer** | `kubectl get clusterissuer` shows the issuer with `Ready=True`; `kubectl -n panoptes get certificate` shows the cert **Ready** (not stuck Pending with no issuer). |
| 8 | **IRSA** | `kubectl -n panoptes get sa panoptes-collector panoptes-mcp -o yaml` shows the `eks.amazonaws.com/role-arn` annotation; `kubectl -n panoptes exec <collector-pod> -- env \| grep AWS_WEB_IDENTITY_TOKEN_FILE` shows the **injected web-identity token** path (the SA-scoped credential is mounted). |

If any check fails, the cause is almost always a default-config defect — a PVC
stuck Pending (EBS CSI / gp3), a Certificate with no issuer
(`createClusterIssuer` left false), or a ServiceAccount missing the IRSA
annotation (`irsaRoleArn` not set).

---

## 7. Teardown (zero orphans)

Order matters. **Helm-uninstall and delete the namespace BEFORE `terraform
destroy`** so the EBS CSI driver reaps the gp3-backed volume *while the cluster
and its driver still exist*. If you destroy the cluster first, the PV's backing
EBS volume is orphaned (no controller left to delete it).

```bash
# 1. Uninstall the chart, then delete the namespace. Deleting the namespace
#    deletes the PVC, which the EBS CSI driver (still running) observes and reaps
#    the gp3 volume for (reclaimPolicy: Delete).
helm uninstall panoptes -n panoptes
kubectl delete namespace panoptes

#    Confirm the PV/EBS volume is gone before proceeding:
kubectl get pv
aws ec2 describe-volumes --filters Name=tag:kubernetes.io/created-for/pvc/name,Values='*' \
  --query 'Volumes[].VolumeId'

# 2. Destroy the infrastructure (cluster, node group, EBS CSI addon, ingress +
#    cert-manager releases, VPC, NAT, EIP, IAM roles, the gp3 StorageClass).
terraform destroy

# 3. Confirm ZERO orphans — none of these should reference panoptes:
aws eks list-clusters
aws ec2 describe-vpcs       --filters Name=tag:PanoptesDedicated,Values=true --query 'Vpcs[].VpcId'
aws ec2 describe-nat-gateways --filter Name=tag:Name,Values=panoptes --query 'NatGateways[?State!=`deleted`].NatGatewayId'
aws ec2 describe-addresses  --filters Name=tag:Name,Values=panoptes-nat --query 'Addresses[].AllocationId'
aws iam list-roles          --query 'Roles[?starts_with(RoleName,`panoptes-`)].RoleName'
aws ecr describe-repositories --query 'repositories[?repositoryName==`panoptes`].repositoryName'
aws ec2 describe-volumes    --filters Name=status,Values=available --query 'Volumes[].VolumeId'
```

The NAT gateway + its EIP are the **always-on cost** (~$32/mo) — confirming they
are deleted is the most cost-relevant check. Two small spot nodes add ~$8/mo
while the stack is up.

---

## 8. Deploy-validation discipline

The load-bearing rule of this runbook:

> **A real apply → install → verify finds default-config defects that `terraform
> validate`, `helm template`, and `kubeconform` structurally *cannot*.**

Static checks validate *shape*: that HCL parses, that a chart renders, that the
rendered YAML matches the Kubernetes schema. They cannot tell you that:

- the in-tree gp2 provisioner is gone on k8s 1.30+, so the VM PVC stays
  **Pending** until an **EBS CSI driver + gp3 default StorageClass** is installed;
- the upstream VictoriaMetrics image runs as **root**, which the shared
  `runAsNonRoot: true` rejects at the kubelet — needing the VM-specific pod
  `securityContext`;
- the chart **referenced** a ClusterIssuer it never **created**, leaving the
  Certificate Pending — fixed by the opt-in `createClusterIssuer` template;
- a single `t4g.small`'s ~11-pod ENI cap can't hold the ~16-pod full stack,
  needing `node_min = 2`;
- an arm64/amd64 image mismatch `CrashLoopBackOff`s with `exec format error`.

Every one of these was found by a **live** deploy, not by a static gate. So the
standing rule for any change to the hosting module or the Helm chart is:
**deploy → verify → destroy against a throwaway cluster before trusting it.** The
module + chart in this repo earned their "deploy-proven" status exactly this way
— 5 full apply → verify → destroy cycles on a real account, all 8 components
functional out-of-box, zero orphans on teardown.
