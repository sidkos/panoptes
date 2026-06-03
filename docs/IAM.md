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
| Cost dashboard (v0.2+, when the Cost dashboard ships) | `ce:GetCostAndUsage`, `budgets:ViewBudget` |

**Hard rule:** zero `Put*`, `Create*`, `Update*`, `Delete*`, `Write*` on any
observed resource. The policy is read/list/describe/get only. A CI check should
fail the policy if any non-read action appears.

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

The collector/MCP process runs under `PanoptesCollector` in the Panoptes home
account. It holds:

1. `sts:AssumeRole` for each `PanoptesReadRole/<env>` (read-only).
2. Nothing else — no write credential of any kind.

MCP **clients** never use these AWS credentials. Clients authenticate to the MCP
server over **SSO/OIDC** (no anonymous access — see
[`ARCHITECTURE.md`](ARCHITECTURE.md) §6); the server, not the client, holds the
AWS identity.

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
