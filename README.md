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

## Documentation

| Doc | What's in it |
|-----|--------------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Topology, canonical signal model, plug-planes, cross-env auth, deployment home, MCP transport |
| [`docs/ADAPTERS.md`](docs/ADAPTERS.md) | The four plug-plane contracts + the adapter catalog with build status |
| [`docs/DASHBOARDS.md`](docs/DASHBOARDS.md) | Dashboard catalog, the MCP tool surface, and the Grafana↔MCP parity model |
| [`docs/IAM.md`](docs/IAM.md) | The single read-only credential domain — `PanoptesReadRole`, trust/least-privilege policy, K8s RBAC, no write path |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phased plan (v0.1 → v0.3) and how a consumer wires Panoptes in |

## Status

Pre-alpha — founding design phase. The reference consumer is a Kubernetes
(EKS/Agones) game platform with CloudWatch + Sentry already in place; that
platform's `dev` environment is the first proving ground.

## License

[Apache License 2.0](LICENSE).
