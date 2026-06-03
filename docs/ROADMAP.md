# Panoptes — Roadmap & Integration

Phased plan from a local proof to a hosted, multi-environment single pane. Each
phase is shippable and demonstrates a distinct competency.

---

## v0.1 — core, local (proof of the abstraction)

**Goal:** prove the adapter model and the Grafana↔MCP parity against one live
environment, at zero cloud cost.

- Adapter framework (the four plug-plane `Protocol`s) + a type-safe registry.
- Sources: **cloudwatch**, **sentry**, **http-health** (all have live `dev`
  targets).
- Store: **victoriametrics** (single node).
- Dashboards (Grafana, run locally): the **core packs** **Errors/Sentry**,
  **Logs**, **Overview** (tier 1, shipped in Panoptes), plus a **generic
  brand-neutral demo pack** injected from a mounted dir to prove the
  consumer-pack injection path (tier 2 — see
  [`DASHBOARDS.md`](DASHBOARDS.md) §4). A real consumer keeps its own pack (e.g.
  Matchmaking/Allocator) in its own repo and points the injection config there.
- MCP server (stdio for local dev), **fully read-only**: `describe_health`,
  `search_incidents`, `search_logs`, plus discovery (`describe_signal_catalog`,
  `list_dashboards`, `get_dashboard_data`), and a demo-pack-registered synthetic
  tool (`get_demo_signal`) proving the consumer-tool path.
- Notifier plane proven with a `logging` notifier (writes alerts to stdout/log);
  `sns`/`slack` deferred to v0.2.
- Everything runs via `docker-compose up` pointed at the live `dev` environment.
- Tests: unit coverage on each adapter's normalization logic (mocked upstreams)
  and on the MCP tool aggregation.

**Demonstrates:** plugin architecture, signal normalization, dashboards-as-code,
MCP/AI-ops, typed Python.

---

## v0.2 — hosted, the deployed single pane

**Goal:** stand the stack up in its own always-on home — a **dedicated,
right-sized Panoptes EKS cluster** (never an observed cluster) — and gate access.

- **Terraform module** that provisions a **dedicated VPC + dedicated EKS cluster**
  (control plane + a minimal **managed** node group, spot-eligible) **in the SAME AWS
  account** as the observed infra + **IRSA** for the collector/MCP service accounts
  (the IRSA base identity then assumes the per-env read-roles in-account, via the
  existing AWS-profile / assume-role seam — replacing the old EC2 instance-profile
  path) + an **nginx-ingress + cert-manager** GitHub-gated ingress, and a **Helm
  chart** that runs the stack as Kubernetes workloads (store + Grafana + collector +
  MCP + auth proxy). The **local** path stays `docker-compose up` (v0.1); EKS is the
  **hosted** target — same GHCR image, two deploy shapes.
- MCP over **streamable HTTP**, **GitHub-SSO-gated** (oauth2-proxy's `github` provider
  with an org/team allowlist, fronted by an nginx forward-auth ingress + cert-manager
  TLS) — reachable by any MCP client after the GitHub login. The HTTP face reuses the
  same `build_server` tool table as stdio.
- Add the **kubernetes** source (consumer-agnostic core — it also observes Panoptes'
  OWN cluster); add notifiers (**sns**, **slack**) and declarative alert rules.
- SLO dashboard + `get_slo` / `compare_envs`.
- Wire `stage`/`prod` as `enabled: false` config stubs.

**Demonstrates:** IaC (Terraform EKS + IRSA + ingress), Helm packaging, cross-account
read-only roles via IRSA, a failure-domain-independent hosted service with SSO auth,
GitOps image/build pipeline, alerting.

---

## v0.3 — depth + provable genericity (SHIPPED)

- **prometheus** core source (SHIPPED) — a read-only PromQL scrape of any reachable
  Prometheus. Panoptes does **not** stand up a Prometheus of its own (cost
  discipline); it scrapes a consumer-run one. Because Panoptes now runs on its own
  dedicated EKS cluster (v0.2), the `prometheus` source + the Kubernetes/Karpenter
  core packs can also observe **Panoptes' OWN cluster** (self-monitoring), in
  addition to observed consumer clusters. The **Fleet** dashboard is driven by a
  **consumer-pack source adapter** that BUILDS ON this core `prometheus` source
  (a fleet source composing it) — that adapter is domain-specific and lives under
  `examples/consumer-fleet-pack/` (a brand-neutral fixture), not in core, so it
  stays out of the core catalog and the core-purity guard's banned-term set.
- **loki** core source (SHIPPED) — a read-only Loki `query_range` scrape → `LogSignal`.
  `tempo` is explicitly NOT shipped, so the "no trace source" invariant
  (`{metric, log, incident}`) still holds.
- Richer **core** dashboard packs (SHIPPED): **Cost** (+ the now-real `get_cost`), 
  **Datastore**, and **Karpenter**.
- A **second, unrelated consumer** (SHIPPED) — `examples/consumer-pipeline-pack/` (a
  data-pipeline domain: job lag / queue depth / freshness) — wired in alongside the
  fleet consumer to prove the core is genuinely generic. The **genericity proof**
  (`tests/unit/test_genericity_two_consumers.py`) asserts the core registry baseline is
  byte-identical across the two unrelated injections — not just "the reference consumer's
  monitoring with extra steps."

---

## How a consumer wires Panoptes in

Panoptes is standalone; a consumer's repo carries almost nothing:

1. **One read-only role per environment** (Terraform/Pulumi in the consumer's
   own IaC) that `PanoptesReadRole` can assume — CloudWatch + (optionally) EKS
   read.
2. **A config file** selecting that environment's sources (see
   [`ADAPTERS.md`](ADAPTERS.md) §3).
3. **Optional MCP client registration** — point a local client at the hosted
   SSO-gated endpoint, or use the stdio wrapper for local dev.

That's the whole contract. The stack, the core adapters, the core dashboard
packs, and the MCP server all live in this repo and deploy from here;
consumer-specific dashboards and any custom adapters/tools stay in the consumer's
own repo and are injected at deploy time.

---

## Distribution

- **Terraform module** — `module "panoptes" { source = "github.com/<org>/panoptes//modules/stack" … }` (replace `<org>` with the GitHub org/account the repo is published under). Runner-agnostic (Terraform or OpenTofu). As of v0.2 the hosted target is a **dedicated, right-sized EKS cluster**, so the module provisions the EKS control plane + a minimal managed node group + IRSA + the SSO ingress (see v0.2 spec).
- **Helm chart** (v0.2) — the in-cluster deploy artifact for the stack (store + Grafana + collector + MCP + auth proxy) as Kubernetes workloads. (Helm is the confirmed packaging path — operator decision 2026-06-03; no Kustomize alternative.)
- **Container image** (GHCR) — the runnable stack image (collector + MCP server); pulled by the in-cluster workloads (hosted) and by docker-compose (local).
- **Python package** — the adapter framework + MCP server, for local/stdio use.

> **Local vs hosted (the deploy split — load-bearing).** The **local** dev/proof loop
> is `docker-compose up` on a laptop (v0.1, unchanged — zero cloud cost, stdio MCP). The
> **hosted** target (v0.2) is a dedicated Panoptes **EKS** cluster running the same
> stack as Kubernetes workloads (Helm). Compose is NOT deleted — it remains the local
> experience; EKS is the hosted experience. The same GHCR image runs in both.

---

## Non-negotiables (carried from the principles)

- **Failure-domain independence — a SEPARATE, DEDICATED Panoptes cluster + VPC (be
  precise about what IS and ISN'T independent).** Panoptes must never share a *cluster*
  or *network* failure domain with what it observes. The hosted home (v0.2) is its **own
  dedicated EKS cluster in its own dedicated VPC, and NEVER an observed cluster/VPC** — so
  a cluster crash, node-group failure, or VPC-network fault in an observed environment
  cannot take Panoptes down with it. **What is NOT independent:** the v0.2 home runs in
  the **SAME AWS account** as the observed infra (a deliberate trade — it lets the
  existing AWS-profile / in-account assume-role multi-env access mechanism keep working),
  so the **account control-plane** blast radius is shared (a root-credential compromise,
  account-wide quota exhaustion, or account suspension is a shared event). Cluster + VPC
  isolation satisfies the rule's sharpest edge ("don't die with the thing you monitor");
  full account isolation is the stronger form and a clean future hardening (own account +
  cross-account assume-role) if the shared-account risk ever outweighs the access
  convenience. "Don't share a failure domain with what you watch" means *a separate,
  dedicated Panoptes cluster + VPC*, not *no cluster at all* and not necessarily *a
  separate account*.
- Read-only everywhere: sources and MCP tools never write to observed systems.
- No anonymous MCP access: SSO only (v0.2: a **GitHub**-gated nginx ingress via
  oauth2-proxy's `github` provider — an org/team allowlist).
- **Cost discipline — a minimal, right-sized dedicated cluster.** A single small
  **managed** node group (spot-eligible; single-AZ acceptable for a dev/home stack), a
  single-node store, and no Agones / game-server / Karpenter / multi-node machinery. The
  dedicated Panoptes EKS cluster is a deliberate, honest **cost step-up** over the prior
  single-EC2 + compose idea — accepted because it is the cluster/VPC-failure-domain-
  independent, IRSA-native, GitHub-gated home the hosted single pane needs; it is kept
  minimal to hold the line on cost.
- Two faces, one store: never let Grafana and MCP read different data.
