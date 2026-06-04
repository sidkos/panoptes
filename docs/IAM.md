# Panoptes — IAM & Access Contract

Panoptes is a **standalone** tool that reaches into other environments. Its
access model must therefore be explicit, least-privilege, and auditable. This
document is the authoritative spec for the credentials Panoptes uses.

Panoptes has **exactly one credential domain at runtime**: read-only access into
the observed environments. There is no write credential of any kind — the MCP
server is fully read-only and there is no dashboard-authoring write path (new
dashboards are authored as code in the consumer's repo and injected at deploy
time; see [`DASHBOARDS.md`](DASHBOARDS.md) §4).

```
┌─────────────────────────────┐
│ READ into observed envs     │
│   (cross-account, RO)       │
│  PanoptesReadRole/<env>     │
│  → CloudWatch, Logs, EKS,   │
│    Cost (v0.2+) —           │
│    read/list/get only       │
└─────────────────────────────┘
```

---

## A. Read into observed environments — `PanoptesReadRole`

One role **per observed environment/account**. Panoptes' home principal assumes
it on a schedule to pull signals. It grants **read-only** access and nothing
else.

### Trust policy (in each observed account)

Trusts only the Panoptes home principal, with an `ExternalId` to prevent the
confused-deputy problem:

```jsonc
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<PANOPTES_HOME_ACCOUNT>:role/PanoptesCollector" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<per-env-external-id>" } }
  }]
}
```

### Permission policy — least privilege, per source capability

Grant only the read actions for the sources actually configured for that env.

| Capability | Actions (read-only) |
|------------|---------------------|
| CloudWatch metrics | `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `cloudwatch:GetMetricStatistics`, `cloudwatch:DescribeAlarms` |
| CloudWatch Logs (logs dashboard) | `logs:DescribeLogGroups`, `logs:FilterLogEvents`, `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery` (see read-path note) |
| EKS control plane (cluster discovery) | `eks:DescribeCluster`, `eks:ListClusters` |
| Cost dashboard (v0.3 — SHIPPED) | `ce:GetCostAndUsage`, `budgets:ViewBudget` (authorizes the `DescribeBudget` API the source calls), `sts:GetCallerIdentity` (resolves the account id `DescribeBudget` requires) |

**Hard rule:** zero `Put*`, `Create*`, `Update*`, `Delete*`, `Write*` on any
observed resource. The policy is read/list/describe/get only. A CI check should
fail the policy if any non-read action appears.

> **Cost-grant note (v0.3) — the CE/budgets grant is CONSUMER-side IaC on the env
> read-role, NOT Panoptes' IRSA, and it is read-only.** When a consumer enables the Cost
> dashboard, the three actions above (`ce:GetCostAndUsage`, `budgets:ViewBudget`,
> `sts:GetCallerIdentity`) are added to **that env's `PanoptesReadRole`** — the same per-env
> read-role pattern as every other source capability — in the **consumer's own IaC**, exactly
> like the EKS-RBAC and CRD examples below. They do NOT belong on Panoptes' home-principal
> IRSA role (§B), which holds only `sts:AssumeRole` for the read-roles + the single
> Panoptes-owned `sns:Publish`. All three are READ actions (Cost Explorer and Budgets expose
> only read APIs here; `GetCallerIdentity` reads the caller's own identity), so the §A
> hard rule (zero `Put*`/`Create*`/`Update*`/`Delete*`/`Write*` on observed resources) holds.
> The source self-limits these calls to **at most once per poll interval** (default hourly —
> CE bills per request), independent of the IAM grant.

> **Read-path note — `logs:StartQuery` / `logs:StopQuery` / `logs:GetQueryResults`
> are read actions despite the `Start`/`Stop` verb shapes.** Logs Insights queries
> are a read mechanism: `StartQuery` initiates a *read* query, `GetQueryResults`
> fetches its results, and `StopQuery` cancels a query the caller itself started —
> none mutates an observed resource (no log group, stream, or event is created or
> altered). They are the standard read path for log-search panels and do **not**
> violate the hard rule, which targets resource *mutation* (`Put*`/`Create*`/
> `Update*`/`Delete*`/`Write*`), not verb spelling. A CI policy check keyed on
> mutation verbs should allowlist these three Logs-Insights actions accordingly
> (the source-side no-write guard already never flags `start_query`/`stop_query`/
> `get_query_results` — spec `## Authorization Rules`).

### Kubernetes is RBAC, not IAM

Reading workload and infrastructure state (pods, events, nodes, services) is
authorized by a **read-only Kubernetes ClusterRole + ServiceAccount** in each
cluster, not by IAM. EKS IAM only covers `DescribeCluster` (to discover the
endpoint); the in-cluster reads use a bound SA token. The canonical core rule
grants read-only access to standard core API resources (`nodes` and `services`
back the Kubernetes dashboard's node-count and pending-pod panels — see
[`DASHBOARDS.md`](DASHBOARDS.md) §1 row 4):

```yaml
# read-only ClusterRole bound to the Panoptes SA in each observed cluster
rules:
  - apiGroups: [""]
    resources: [pods, events, nodes, services]
    verbs: [get, list, watch]
```

**Example — consumer-specific CRDs.** A consumer that runs custom resources (this
is consumer-pack territory, not core least-privilege) would additionally grant
read on those CRDs. For instance, a consumer running Agones would add:

```yaml
  # consumer example only — not part of the core least-privilege template
  - apiGroups: ["agones.dev"]
    resources: [fleets, gameservers]
    verbs: [get, list, watch]
```

### Note: consumer-pack injection is deployment plumbing, not a runtime write

Consumer dashboards (and an optional `pack.py`) are authored as code in the
**consumer's own repo** and injected into Panoptes at deploy time (mounted dir
locally, or a pinned git ref when hosted — see [`DASHBOARDS.md`](DASHBOARDS.md)
§4). The deploy process performs a **read-only `git fetch`** of the consumer pack
at that pinned ref as part of provisioning. This is deployment plumbing — it
reads source the deployer already controls — **not** an MCP or runtime write, and
it needs **no** write credential into any system. There is no
`PanoptesArtifactWriter`, no scoped repo-write PAT, and no save-to-repo path.

---

## B. The Panoptes home principal

**v0.2 (hosted, SHIPPED): the home principal is an IRSA-bound role on the dedicated
EKS cluster**, not an EC2 instance profile. The Terraform module (`modules/stack/irsa.tf`)
provisions an IAM role whose **trust policy** is an OIDC trust to the **cluster's own OIDC
provider** (`aws_iam_openid_connect_provider`), with a `StringEquals` condition pinning the
provider's `:sub` to EXACTLY the collector + MCP Kubernetes service accounts
(`system:serviceaccount:panoptes:panoptes-collector` and `:panoptes-mcp`) and `:aud` to
`sts.amazonaws.com`. So the collector/MCP pods receive the role's credential via their
projected SA token — no node-wide instance profile that any co-scheduled pod would inherit
(least privilege; this is the v0.2 change from the prior instance-profile assume-role path).
The cluster's OIDC provider here is **IRSA mechanics**, NOT the user-auth IdP (that is
GitHub via oauth2-proxy at the ingress — see §C and [`ARCHITECTURE.md`](ARCHITECTURE.md) §6).

The IRSA role gives the pod its base identity; it then assumes the per-env read-roles
**within the SAME AWS account** (decision #1 — no cross-account trust). It holds:

1. `sts:AssumeRole` for each configured `PanoptesReadRole/<env>` ARN, in-account
   (read-only). The grant is **structurally absent when the role-ARN list is empty** (the
   stage/prod disabled-stub case), so it provably yields ZERO assume-role grants until a
   non-empty list is supplied — never a `Resource: "*"`.
2. **One** resource-scoped `sns:Publish` on the single Panoptes-owned alert topic ARN (the
   only write grant in the whole system, on a Panoptes-owned resource — §A's read-only-
   wrt-observed boundary holds: this writes to Panoptes' own alert channel, not an observed
   system).
3. Nothing else — no `Action: "*"`, no `Put*/Create*/Delete*` on any non-Panoptes resource,
   no observed write. (The `tests/terraform/test_module_plan.py` plan-assertion test pins
   every one of these invariants against the rendered config.)

The v0.1 local path (`docker-compose`) keeps the `AWS_PROFILE` + `PANOPTES_ASSUME_ROLE_ARN`
config seam unchanged — IRSA replaces the home-principal credential SOURCE on EKS, not the
per-env assume-role mechanism.

### Cluster-infrastructure IRSA — the EBS CSI driver

Separate from the Panoptes home principal, the dedicated EKS cluster carries **one
infrastructure IRSA role** for the EKS-managed `aws-ebs-csi-driver` addon
(`modules/stack/ebs_csi.tf`). Its trust policy pins the OIDC provider's `:sub` to EXACTLY
`system:serviceaccount:kube-system:ebs-csi-controller-sa` (and `:aud` to `sts.amazonaws.com`),
and it attaches **only** the AWS-managed `AmazonEBSCSIDriverPolicy`. This role lets the CSI
controller provision/attach EBS volumes for cluster PVCs (the gp3 default StorageClass backing
the VictoriaMetrics PVC) on k8s 1.30+, where the in-tree `kubernetes.io/aws-ebs` provisioner is
gone. It is **not** a Panoptes-runtime credential — it never assumes a read-role and never
touches an observed environment; it is cluster plumbing for the home cluster's own storage,
scoped to its single controller SA. The §A read-only-wrt-observed boundary is unaffected.

MCP **clients** never use these AWS credentials. Clients authenticate to the MCP server at
the **GitHub-gated nginx ingress** (oauth2-proxy `github` provider, org/team allowlist — no
anonymous access; see [`ARCHITECTURE.md`](ARCHITECTURE.md) §6); the server holds the AWS
identity (via IRSA) and validates no client token — the ingress is the boundary.

### EKS API access posture — hardened endpoint

The dedicated cluster's Kubernetes API endpoint is **not** the AWS-default wide-open
`0.0.0.0/0`. Private access is on (nodes in the private subnet pair reach the API server over
the private endpoint), and public access is **restricted to a required, no-default,
validated `cluster_endpoint_public_access_cidrs` allowlist** (`modules/stack/eks.tf`). The
variable validation **fails closed** — it rejects an empty list AND an explicit `0.0.0.0/0` /
`::/0` — so the cluster cannot be applied with an internet-wide management surface. This is
the IAM/network boundary for `kubectl`/control-plane access; it is independent of, and
complementary to, the GitHub-gated ingress that fronts the MCP server above.

---

## C. Auditability

- All `AssumeRole` calls are logged to CloudTrail in the observed account.
- The MCP auth proxy logs every authenticated principal + tool invocation.
- Consumer-pack changes are visible as version-controlled diffs in the consumer's
  own repo (they are code); each deploy pins the ref it injected.
- **Pinned-ref review is a required gate, not just an audit trail.** Because a
  consumer pack's `pack.py` is executed in the Panoptes collector/MCP process,
  the pinned ref is a **code-execution trust boundary**. Bumping a consumer-pack
  pinned ref therefore **REQUIRES a reviewed diff of the fetched subdir before
  the new ref is applied** — the deployer reviews what code will run, not merely
  that a diff exists. See [`DASHBOARDS.md`](DASHBOARDS.md) §4 for the full
  mitigation chain (SHA pin + authenticated transport + optional signature +
  this review gate).

---

## D. Bootstrapping a new environment (consumer checklist)

To plug a new environment into Panoptes:

1. Create `PanoptesReadRole/<env>` from the templates above (Terraform/Pulumi in
   the consumer's own IaC), with the per-env `ExternalId`.
2. Attach the least-privilege read policy for the sources you'll enable.
3. (If using the kubernetes source) apply the read-only ClusterRole + SA and
   hand Panoptes the SA token / kubeconfig context.
4. Add the env block to Panoptes config (see [`ADAPTERS.md`](ADAPTERS.md) §3).

That is the entire access contract a consumer must satisfy.
