# Panoptes — Architecture

This document is the founding contract for Panoptes. It defines the topology,
the canonical data model, the plug-plane abstractions, the cross-environment
auth model, where the stack lives, and how clients reach it.

---

## 1. Topology — single pane, standalone home

Panoptes runs in **its own always-on home**, independent of any environment it
observes, and reaches into each environment read-only.

```
        ┌──────────────── PANOPTES (its own home, always-on) ──────────────┐
        │   adapters  →  normalized store (VictoriaMetrics)                 │
        │                       ↙                       ↘                   │
        │            Grafana (one pane)          MCP server (SSO-gated HTTP) │
        │                       ▲ read-only pulls, every signal tagged env  │
        └──────┬───────────────┬───────────────────┬──────────────────────┘
               │ env=dev        │ env=stage          │ env=prod
        ┌──────▼──────┐  ┌──────▼──────┐      ┌──────▼──────┐
        │ environment │  │ environment │      │ environment │
        │  CloudWatch │  │  CloudWatch │      │  CloudWatch │
        │  Sentry     │  │  Sentry     │      │  Sentry     │
        │  Kubernetes │  │  Kubernetes │      │  Kubernetes │
        └─────────────┘  └─────────────┘      └─────────────┘
```

### The load-bearing rule: failure-domain independence

A monitoring stack that dies with the thing it monitors is useless exactly when
you need it most. Therefore Panoptes **must not** be deployed inside any
observed cluster. It gets its own home and pulls from each environment from the
outside. This also means it survives environments that are torn down on a
schedule (e.g. cost-saving weekend teardowns).

### `env` is a first-class label

Every signal that enters Panoptes is tagged with the environment it came from
(`env=dev|stage|prod`). This single label drives:

- **Grafana**: one dashboard set with an `env` template variable (switch
  dev→prod, or "All" to compare).
- **MCP**: tools take an `env` argument (`describe_health(env="prod")`) or
  compare across them (`compare_envs(metric=…)`).
- **SLOs / alerts**: defined once, evaluated per environment.

Environments that aren't live yet sit in config as `enabled: false` stubs and
light up with a flag flip when they're stood up — no code change.

---

## 2. Canonical signal model

Adapters normalize everything into four signal kinds — the three OpenTelemetry
signals plus an incident type:

| Signal     | Shape (conceptual)                                              | Typical source |
|------------|----------------------------------------------------------------|----------------|
| `metric`   | name, value, timestamp, labels{env, …}                         | CloudWatch, Prometheus |
| `log`      | timestamp, message, level, labels{env, source, …}              | CloudWatch Logs, Loki |
| `trace`    | trace_id, spans[], duration_ms, labels{env, …}                 | Tempo, Jaeger |
| `incident` | id, title, level, first/last_seen, count, labels{env, phase, …}| Sentry |

Metrics are the spine and live in the time-series store. Logs/traces/incidents
are queried live from their source by default and may be cached/indexed in the
store later. This is the contract that makes Grafana and MCP interchangeable
readers.

---

## 3. The four plug-planes

Everything in Panoptes is a plugin on one of four planes. Each plane is a small
typed `Protocol`; a new tool is one class + a registry entry. Full contracts and
the adapter catalog are in [`ADAPTERS.md`](ADAPTERS.md).

| Plane         | Responsibility                          | Example adapters (build status in [`ADAPTERS.md`](ADAPTERS.md) §2) |
|---------------|-----------------------------------------|----------------|
| **Source**    | read signals *from* a monitoring tool   | cloudwatch, sentry, http-health (v0.1); kubernetes (v0.2) |
| **Store**     | persist + query the canonical model     | victoriametrics (default), passthrough |
| **Notifier**  | deliver alerts                          | logging (v0.1); sns, slack (v0.2) |
| **Dashboard** | provision visualizations as code        | grafana |

**Capability negotiation, not assumption.** Each Source declares which signal
kinds it `provides`. A query for traces when no trace source is configured
returns a clean "no trace source" rather than an empty guess.

### Core vs consumer packs — the plugin boundary

Panoptes is standalone and plugin-shaped in **both** directions: it plugs into
your tools, and you plug it into your repo. The **core** must stay
consumer-agnostic — it knows nothing about any particular product.

- **Core** is where the generic adapters live (cloudwatch, sentry, kubernetes,
  prometheus, … — per the ADAPTERS catalog's build status; not every adapter is
  shipped at every version), alongside the store, the MCP server, and the generic
  *core* dashboard packs. The authoritative core-pack list is the
  [`DASHBOARDS.md`](DASHBOARDS.md) §1 catalog (rows marked *core*: errors-sentry,
  logs, overview, kubernetes, compute, datastore, API gateway, networking/certs,
  cost, slo) — this doc does not re-enumerate it to avoid drift.
- **Consumer packs** carry everything domain-specific: a config file, custom
  dashboard JSON, and (optionally) a **custom Source/Notifier adapter** plus the
  **MCP tools it registers**. They live **in the consumer's own repo** (e.g.
  `<consumer>/ops/panoptes/`), version-controlled there as code, and are
  **injected into Panoptes at deploy time** via a config pointer — a mounted dir
  locally, or a pinned git repo+ref+subdir when hosted (see
  [`DASHBOARDS.md`](DASHBOARDS.md) §4). They are never bundled into Panoptes.

A consumer integrates by pointing Panoptes at its pack — never by editing core.
The consumer can be **anything**: a game platform (with a matchmaking/allocator,
fleet, and business-metrics pack), an e-commerce backend (order-throughput,
payment-error, and inventory packs), or a data pipeline (job-lag, queue-depth,
freshness packs). Each is just an *illustrative example* of a consumer pack that
lives in the consumer's own repo, never part of core scope — core stays
deliberately agnostic to any one domain.

---

## 4. Shared normalized store — the parity guarantee

```
adapters ──▶ normalized store (VictoriaMetrics, Prometheus-compatible)
                 ├──▶ Grafana panels      (human face)
                 └──▶ MCP tools           (LLM face)
```

Grafana and the MCP server are **both thin readers over one store**. Define a
signal once in an adapter and it appears in both surfaces automatically. This is
why "add a monitoring tool" propagates to dashboards *and* MCP for free, and why
the data Claude receives is identical to what a human sees on a panel.

Store choice: **VictoriaMetrics single-node** — Prometheus-compatible (PromQL),
single binary, very low cost. Swappable to `prometheus` or `passthrough` via
config.

---

## 5. Cross-environment read auth

Plugging into an environment means holding a **read-only** credential for it.

| Source     | Per-environment auth                                                       |
|------------|-----------------------------------------------------------------------------|
| CloudWatch | cross-account **assume-role** (`PanoptesReadRole` in each account), or per-region creds in a single account |
| Sentry     | API token; environments distinguished by Sentry's `environment` tag         |
| Kubernetes | read-only ServiceAccount token / kubeconfig context per cluster             |

The consumer's side of the contract is intentionally tiny: **one read-only IAM
role (or equivalent) per environment**. Panoptes assumes that role on a
schedule, pulls, tags `env`, normalizes, and stores. Credentials are supplied by
the deployment environment (env vars / mounted secrets), never hardcoded in
config.

---

## 6. MCP transport & access control

The MCP server is a **hosted, streamable-HTTP** endpoint on the Panoptes home
box, exposing the same data the dashboards show (see
[`DASHBOARDS.md`](DASHBOARDS.md) for the tool surface).

**Access is SSO-gated. There is no anonymous access.** The HTTP endpoint sits
behind an OAuth flow backed by an SSO identity provider, enforced by an
`oauth2-proxy` gate in front of the MCP server (the v0.2 deployment uses GitHub
as the provider, restricted by org/team allowlist — see
[`ROADMAP.md`](ROADMAP.md) §v0.2; other OAuth/OIDC providers, e.g. AWS IAM
Identity Center or an Okta OIDC app, plug in the same way). Any MCP client —
Claude Code, Claude Desktop, Cursor, a custom agent — authenticates through SSO
before it can call a tool.

**Fully read-only.** MCP tools are read-only **with respect to observed
systems** — they never write to CloudWatch/Sentry/DynamoDB/etc. — and the server
has **no write path of any kind**: there is no dashboard authoring, no
save-to-repo, no runtime mutation. New dashboards are authored as code in the
consumer's own repo and injected at deploy time (see
[`DASHBOARDS.md`](DASHBOARDS.md) §4), so the runtime never needs a write
credential. See [`IAM.md`](IAM.md) for the single read-only credential domain.

A thin **stdio** wrapper is also provided for local development and for embedding
in a local client's MCP config; it talks to the same server logic without the
hosted HTTP/SSO layer.

---

## 7. Deployment home

The home is a **small, cost-disciplined, always-on EKS cluster, provisioned by
Terraform, running the stack via a Helm chart** (normalized store + Grafana +
collector + MCP server + oauth2-proxy, alongside the nginx-ingress, cert-manager,
and EBS CSI controllers). The cluster lives in its **own dedicated VPC**, never
inside an observed cluster — failure-domain independence is the load-bearing
constraint (see §1), so Panoptes must survive the very environments it watches,
including scheduled teardowns. Rationale:

- Deploying inside an observed cluster violates failure-domain independence.
- A dedicated VPC keeps the blast radius and the networking story self-contained,
  while still demonstrating IaC, networking, cross-account roles, container
  orchestration, and GitOps — without a large bill.

### Network topology — public/private split, one NAT

The dedicated VPC is laid out as a **public/private subnet pair** across two AZs:

- The **public** subnet pair carries only the internet-facing nginx
  `LoadBalancer` (the ingress ELB) and a single, cost-disciplined **NAT
  gateway**. Both subnet pairs carry `kubernetes.io/cluster/panoptes=owned`
  (public also `kubernetes.io/role/elb`, private `kubernetes.io/role/internal-elb`)
  so the in-tree cloud provider can discover LoadBalancer subnets — no AWS Load
  Balancer Controller is installed.
- The **private** subnet pair runs the managed node group with
  `map_public_ip_on_launch=false` — **nodes are not internet-routable**. Their
  egress is via the **single NAT gateway** (one NAT for the whole VPC, not
  per-AZ, to hold the bill down — ~$32/mo for the NAT + EIP).

The **EKS API server endpoint is hardened**: private access is on (nodes reach
the API over the private endpoint), and public access is **restricted to a
required, validated CIDR allowlist** — *not* the AWS-default wide-open
`0.0.0.0/0`. The allowlist variable has no default and its validation fails
closed: an empty list, or an explicit `0.0.0.0/0` / `::/0`, is rejected.

### Persistent storage — EBS CSI driver, gp3 default

Persistent volumes (the VictoriaMetrics PVC) are backed by the EKS-managed
**aws-ebs-csi-driver** addon, with its own **IRSA** role scoped to the
`kube-system:ebs-csi-controller-sa` service account, and a **gp3 CSI
StorageClass set as the cluster default** (the legacy in-tree `gp2` default is
unmarked). This is required on Kubernetes 1.30+, where the in-tree
`kubernetes.io/aws-ebs` provisioner is gone — without the driver the
VictoriaMetrics PVC stays `Pending`.

The **local dev loop** is the same stack run on a laptop via docker-compose,
pointed at a live environment — zero cloud cost. The Terraform layer provisions
the cluster, the VPC, the storage, and the per-environment read-roles.

Distribution: Panoptes ships as a **Terraform module** (consumer imports it) and
a **Python package / container image** (the runnable stack). See
[`ROADMAP.md`](ROADMAP.md).
