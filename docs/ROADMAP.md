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

**Goal:** stand the stack up in its own always-on home and gate access.

- **Terraform module** that provisions the home host + the per-environment
  read-roles, and runs the compose stack (store + Grafana + collector + MCP +
  auth proxy).
- MCP over **streamable HTTP**, **SSO/OIDC-gated** (OAuth 2.1 via an auth
  proxy) — reachable by any MCP client after SSO.
- Add the **kubernetes** source; add notifiers (**sns**, **slack**) and
  declarative alert rules.
- SLO dashboard + `get_slo` / `compare_envs`.
- Wire `stage`/`prod` as `enabled: false` config stubs.

**Demonstrates:** IaC (Terraform module), cross-account read-only roles, hosted
service with SSO auth, GitOps image/build pipeline, alerting.

---

## v0.3 — stretch (depth + provable genericity)

- **prometheus** core source (stand up an in-cluster Prometheus). The
  **Fleet / Game-Business** dashboards are driven by a **consumer-pack source
  adapter** for the consumer's fleet technology (e.g. an Agones source that
  scrapes fleet metrics via that Prometheus) — that adapter is domain-specific and
  lives in the consumer's own repo, not in core, so it stays out of the
  brand-neutral core catalog and the core-purity guard's banned-term set.
- Richer **core** dashboard packs (Kubernetes/Karpenter, Cost, Datastore).
- A **second, unrelated consumer** wired in to prove the core is genuinely
  generic — not just "the reference consumer's monitoring with extra steps."
- Optional: loki/tempo sources for logs/traces parity.

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

- **Terraform module** — `module "panoptes" { source = "github.com/<org>/panoptes//modules/stack" … }` (replace `<org>` with the GitHub org/account the repo is published under). Runner-agnostic (Terraform or OpenTofu).
- **Container image** (GHCR) — the runnable stack (collector + MCP server).
- **Python package** — the adapter framework + MCP server, for local/stdio use.

---

## Non-negotiables (carried from the principles)

- Failure-domain independence: never deploy inside an observed cluster.
- Read-only everywhere: sources and MCP tools never write to observed systems.
- No anonymous MCP access: SSO/OIDC only.
- Cost discipline: single-node store, single small home host, compose over a
  dedicated cluster.
- Two faces, one store: never let Grafana and MCP read different data.
