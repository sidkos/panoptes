# Panoptes

> A standalone, pluggable monitoring stack that gives you **one place to look** across all your environments — and exposes that same view to an LLM over MCP.

Panoptes is **not** another error tracker or a Grafana replacement. It is a thin
**normalizing meta-layer** that plugs into the monitoring tools you already run
(CloudWatch, Sentry, Prometheus, Kubernetes, …), folds their signals into one
canonical model, and serves that model through two faces that never drift apart:

- a **single-pane Grafana** with preconfigured dashboards, and
- a **Model Context Protocol (MCP) server** so Claude — or any MCP client — can
  query the exact same data programmatically.

Because both faces read one shared store, you define each signal **once** (in an
adapter) and get the human dashboard *and* the machine tool for free.

```
        ┌──────────── PANOPTES (its own always-on home) ─────────────┐
        │  adapters → normalized store → Grafana (one pane)          │
        │                              ↘  MCP server (SSO-gated HTTP) │
        │           ▲ read-only pulls, every signal tagged env=…      │
        └───────────┬──────────────┬──────────────┬──────────────────┘
              env=dev          env=stage        env=prod
           (your infra)      (your infra)     (your infra)
```

## Principles

1. **Single pane of glass.** `env` is a first-class label. Look at one
   environment, or compare them side by side.
2. **Don't share a failure domain with what you watch.** Panoptes runs in its
   own home, reachable even when the observed infrastructure is down or torn
   down. Observability must outlive the thing it observes.
3. **Everything is a plugin.** Sources, stores, notifiers, and dashboards are
   all config-selected adapters. Adding a monitoring tool = a few lines of YAML
   (and one small adapter class if it doesn't exist yet).
4. **Read-only by contract.** Panoptes reads, normalizes, stores, and notifies.
   It never writes back to the systems it observes.
5. **Two faces, one store.** Grafana and the MCP server are thin readers over
   the same normalized data — guaranteed parity between what a human sees and
   what an LLM gets.

## Quickstart (v0.1)

v0.1 is the **local proof**: the whole stack runs on a laptop via `docker compose`
at zero cloud cost. The MCP transport in v0.1 is **stdio** (the SSO-gated HTTP face
in the diagram above is v0.2).

```bash
# 1. Point Panoptes at your consumer pack (defaults to the in-repo brand-neutral demo)
cp .env.example .env          # then fill in the read-only AWS/Sentry creds + DEV_HEALTH_URL

# 2. Bring up VictoriaMetrics + Grafana + the collector + the MCP server
docker compose up --build     # Grafana on :3000, VictoriaMetrics on :8428
# (--build on first run / after a core/ edit; the image bakes deps, so restarts
#  are fast and work offline — no per-start pip install)

# 3. Open the single pane
open http://localhost:3000    # the 3 core dashboards + your injected consumer pack, env-templated
```

The **collector** pulls read-only from each configured source (CloudWatch, Sentry,
an HTTP `/health`) into the shared store; **Grafana** and the **MCP server** are two
thin readers over that one store, so a human and an LLM see the same data.

### Local stack

`scripts/stack.sh` is a thin, idempotent convenience wrapper over `docker compose`
for the local proof — it builds the image, polls readiness, and gives one-shot
status / logs / smoke / query subcommands:

```bash
bash scripts/stack.sh up        # build + start, seed .env on first run, wait until ready
bash scripts/stack.sh status    # ps + health probes + last collector logs (exit 0 iff all up)
bash scripts/stack.sh smoke     # run a single collector cycle (--once)
bash scripts/stack.sh down      # stop (add -v / --volumes to also drop the vm-data volume)
```

Run `bash scripts/stack.sh --help` for the full subcommand list.

Register the MCP server with any MCP client (e.g. Claude) by running
`python -m core.mcp.server` over stdio with `PANOPTES_CONFIG` + (optionally)
`PANOPTES_CONSUMER_PACK` set — it exposes read-only `describe_signal_catalog`,
`list_dashboards`, `get_dashboard_data`, `query_metric`, `search_incidents`,
`search_logs`, and `describe_health`.

**Wiring your own consumer pack:** keep a pack dir in *your* repo (a `pack.py`, a
`panoptes.yaml`, and `dashboards/<name>/dashboard.json`), point `CONSUMER_PACK_DIR`
at it, and Panoptes injects it at deploy time — it is never bundled into core. See
[`examples/demo-pack/`](examples/demo-pack/) for the minimal brand-neutral template and its
[README](examples/demo-pack/README.md), and the two richer v0.3 proof fixtures —
[`examples/consumer-fleet-pack/`](examples/consumer-fleet-pack/) (a source that builds on the
core `prometheus` source) and [`examples/consumer-pipeline-pack/`](examples/consumer-pipeline-pack/)
(a standalone source, an unrelated domain) — which together form the "provable genericity"
proof described under [Status](#status).

## Quickstart (v0.2 — hosted on EKS)

**Local stays `docker compose` (unchanged); hosted is a dedicated EKS cluster.** v0.2 adds
a Terraform module + a Helm chart + a GHCR image so the same stack runs in-cluster behind a
GitHub-gated nginx ingress. The MCP face becomes streamable-**HTTP** (the v0.1 stdio face
still works); the GitHub auth gate is the ingress, not the server.

**1. Provision the dedicated EKS cluster + IRSA + ingress prereqs (Terraform).** The module
creates Panoptes' OWN dedicated VPC + dedicated EKS cluster (SAME AWS account, never an
observed cluster/VPC — failure-domain independence), a small managed spot node group, the
IRSA role (SA-scoped to the collector + MCP service accounts), and the nginx-ingress +
cert-manager prerequisites:

```hcl
module "panoptes" {
  source = "github.com/sidkos/panoptes//modules/stack"

  home_region = "us-east-1"
  hostname    = "panoptes.example.com"
  image_tag   = "v0.2.0"                              # an IMMUTABLE tag, never :latest

  github_oauth_client_id = var.github_oauth_client_id
  github_org             = "your-github-org"          # the access allowlist (in-account)

  read_role_arns  = ["arn:aws:iam::1234:role/PanoptesReadRole-dev"]  # empty = no grants
  alert_topic_arn = "arn:aws:sns:us-east-1:1234:panoptes-alerts"     # the ONE write grant
}
```

The module's `irsa_role_arn` output is the value the chart annotates the SAs with. A worked
root config is in [`deploy/terraform/example`](deploy/terraform/example); the module is
runner-agnostic (Terraform **or** OpenTofu).

**2. Set up the GitHub OAuth app (the access gate).** Create a GitHub OAuth app, set its
callback to `https://panoptes.<domain>/oauth2/callback`, and supply the client id/secret +
the **org (and/or team) allowlist** to the chart. `oauth2Proxy.githubOrg` is **REQUIRED and
FAIL-CLOSED**: an empty org would disable the GitHub allowlist (admitting any GitHub user),
so the chart's `values.schema.json` + a template `required` guard ABORT the render if it is
empty. The client secret comes from a referenced Kubernetes Secret — never inlined.

**3. Install the chart.** It renders the single-node VictoriaMetrics store + the
Grafana/collector/MCP/oauth2-proxy workloads, ClusterIP-only Services (the MCP Service is
**ClusterIP** — the anonymous-bypass guard; the nginx ingress is the sole public path), and
the GitHub forward-auth Ingress (oauth2-proxy + cert-manager TLS, with `/healthz` exempt):

```bash
helm install panoptes ./charts/panoptes \
  --set image.tag=v0.2.0 \
  --set hostname=panoptes.example.com \
  --set irsaRoleArn="$(terraform output -raw irsa_role_arn)" \
  --set oauth2Proxy.githubOrg=your-github-org \
  --set oauth2Proxy.githubClientId=Iv1.xxxxxxxx
```

`stage`/`prod` ship as `enabled:false` config stubs that light up with a flag flip + a
non-empty read-role ARN (no code change). See
[`examples/demo-pack/panoptes.hosted.yaml`](examples/demo-pack/panoptes.hosted.yaml).

**4. The GHCR image** (`ghcr.io/sidkos/panoptes:<tag>`) is built + pushed by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) on a `v0.2.*` tag — the
single CI write permission, scoped to the publish job. The Helm `image.tag` + the Terraform
`image_tag` pin the same immutable tag (never `:latest`).

> **local == compose, hosted == EKS.** The `docker compose` path above is unchanged — it is
> the local/dev proof at zero cloud cost (stdio MCP, no proxy). The Terraform module + Helm
> chart are the HOSTED analogue; the **same GHCR image** runs in both.

### Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -m "not integration"   # fast unit suite (mocked upstreams)
pytest -m integration         # the compose/testcontainers suite (needs Docker)
```

Run `./scripts/precommit.sh` before pushing — it mirrors the full CI gate locally.

The build is specified end-to-end in
[`docs/specs/v0.1_core_local_proof.md`](docs/specs/v0.1_core_local_proof.md) (the
authoritative spec) and
[`docs/specs/v0.1_implementation_plan.md`](docs/specs/v0.1_implementation_plan.md)
(the phased build playbook).

## Documentation

| Doc | What's in it |
|-----|--------------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Topology, canonical signal model, plug-planes, cross-env auth, deployment home, MCP transport |
| [`docs/ADAPTERS.md`](docs/ADAPTERS.md) | The four plug-plane contracts + the adapter catalog with build status |
| [`docs/DASHBOARDS.md`](docs/DASHBOARDS.md) | Dashboard catalog, the MCP tool surface, and the Grafana↔MCP parity model |
| [`docs/IAM.md`](docs/IAM.md) | The single read-only credential domain — `PanoptesReadRole`, trust/least-privilege policy, K8s RBAC, no write path |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phased plan (v0.1 → v0.3) and how a consumer wires Panoptes in |

## Status

**v0.1 (local proof) — built.** The canonical model, the four typed plug-planes +
registry, the config loader, the `cloudwatch`/`sentry`/`http-health` sources, the
`victoriametrics`/`passthrough` stores, the collector loop + `logging` notifier, the
`grafana` provider with three core dashboard packs + consumer-pack injection, the
read-only MCP stdio server, the brand-neutral demo pack, and the docker-compose
stack are all implemented and tested (unit + integration, strict CI).

**v0.2 (hosted on EKS) — built.** The streamable-**HTTP** MCP face (reusing the same
`build_server` as stdio — two faces, one store), the `kubernetes` source +
`get_cluster_state` + the Kubernetes dashboard, the `sns`/`slack` notifiers (with the
path-scoped no-write guard), declarative alert rules evaluated by the collector,
`get_slo`/`compare_envs` promoted to real tools (`get_cost` stays a v0.3 stub), the SLO
dashboard, the Terraform **dedicated-EKS module** (`modules/stack` — dedicated VPC +
cluster, IRSA SA-scoped trust, resource-scoped publish, managed-not-Karpenter node group),
the **Helm chart** (ClusterIP-only MCP, IRSA-annotated SAs, GitHub forward-auth nginx
ingress + cert-manager TLS, unauthenticated `/healthz`, `stage`/`prod` stubs), the
deploy-time `git` dashboard injection (full-SHA pin, mutable-ref rejected), and the
GHCR publish workflow are all implemented and tested (unit + integration + the hermetic
`terraform validate/tflint` + `helm lint/template/kubeconform` gates).

**v0.3 (depth & provable genericity) — built.** The `prometheus` source (read-only PromQL
scrape → `MetricSignal`) and the `loki` source (read-only → `LogSignal`; `tempo` explicitly
deferred, so the "no trace source" invariant `{metric, log, incident}` still holds), three
new core dashboard packs (**Cost**, **Datastore**, **Karpenter**), the now-real `get_cost`
tool (the LAST `_V0_2_STUB_TOOLS` entry removed — the stub set is empty) reading
`panoptes_cost_*` gauges from the `cloudwatch` source's opt-in once-per-interval CE/budgets
read path, and — the release thesis — **two unrelated consumer packs proving provable
genericity** (see below) are all implemented and tested (unit + integration, the gate stays
green). `docker compose` stays the local/dev path; EKS is the hosted target. The full v0.3
spec is [`docs/specs/v0.3_depth_genericity.md`](docs/specs/v0.3_depth_genericity.md) (+ its
plan); the roadmap is summarized in [`docs/ROADMAP.md`](docs/ROADMAP.md).

### Provable genericity — two unrelated consumers, zero core diff

The v0.3 release thesis is the one test that distinguishes a *genuinely* generic core from
one secretly shaped around its first consumer: **two UNRELATED consumer packs inject the same
way with a byte-identical core baseline between them.** Both ship as brand-neutral fixtures
under [`examples/`](examples/), each injected via the v0.1 `PANOPTES_CONSUMER_PACK` hook,
each registering its own source + MCP tool + dashboard via `register_tools` — **never
touching `core/`**:

- **Consumer #1 — a game-server fleet** ([`examples/consumer-fleet-pack/`](examples/consumer-fleet-pack/)):
  a `fleet` source that **BUILDS ON the core `prometheus` source** (it composes
  `PrometheusSource` and relabels the scrape into `panoptes_fleet_*` gauges) + a
  `get_fleet_health(env)` tool + a Fleet dashboard.
- **Consumer #2 — a data pipeline** ([`examples/consumer-pipeline-pack/`](examples/consumer-pipeline-pack/)):
  a deliberately UNRELATED domain — a STANDALONE `pipeline` source (job lag / queue depth /
  data freshness, not built on prometheus) + a `get_pipeline_lag(env)` tool + a Pipeline
  dashboard.

The proof (`tests/unit/test_genericity_two_consumers.py`) asserts each pack injects
**additively** (the core's own registrations are unchanged), the core-purity guard is green
with **both** fixtures present, and — the load-bearing assertion — the **core registry
baseline is BYTE-IDENTICAL** across the two injections: with each pack's own additions
subtracted, the serialized core sources/stores/notifiers/tools are string-equal. A single
per-consumer core branch would break it. `tests/integration/test_two_consumer_injection.py`
then proves both packs' tools answer over the real MCP transport. The dependency arrow points
ONE way — consumer→core — enforced structurally by the `core/`↛`examples/` import guard.

## License

[Apache License 2.0](LICENSE).
