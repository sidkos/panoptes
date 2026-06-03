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
[`examples/demo-pack/`](examples/demo-pack/) for the brand-neutral template and its
[README](examples/demo-pack/README.md).

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

**v0.2 + v0.3 — fully specified, implementation pending.** The hosted deployment
(a **dedicated EKS cluster** in its own VPC, Helm-packaged, GitHub-SSO-gated via
oauth2-proxy), the streamable-HTTP MCP face, the `kubernetes`/`prometheus` sources,
the `sns`/`slack` notifiers, and the second-consumer genericity proof are written
up in [`docs/specs/`](docs/specs/) (`v0.2_hosted_single_pane.md` + its plan,
`v0.3_depth_genericity.md` + its plan) and summarized in
[`docs/ROADMAP.md`](docs/ROADMAP.md). `docker compose` stays the local/dev path;
EKS is the hosted target.

## License

[Apache License 2.0](LICENSE).
