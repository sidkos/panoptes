# Demo consumer pack (brand-neutral injection fixture)

This directory is a **generic, brand-neutral example** of a Panoptes *consumer pack*.
It exists to prove — end to end — that the consumer-pack **injection** path works:
a pack registers an adapter, an MCP tool, and a dashboard, all from outside `core/`,
**without `core/` ever importing it**.

> **This is a fixture, not a feature.** A real consumer does **not** edit or ship this
> directory. It keeps an equivalent pack in **its own repo** and points Panoptes'
> injection config at it. Everything here is synthetic and meant to be replaced.

## What's in the pack

| File | Role |
|------|------|
| [`pack.py`](pack.py) | The injection entry point. On import it registers a synthetic `Store` (`demo-synthetic`) on the core registry; its `register_tools(mcp_server)` hook adds the read-only `get_demo_signal(env, window) -> DemoSignal` tool. |
| [`dashboards/demo/dashboard.json`](dashboards/demo/dashboard.json) | A generic Grafana dashboard over `panoptes_*` metrics, with the `env` template variable. The provider globs `dashboards/**/dashboard.json` and provisions it alongside the core packs. |
| [`panoptes.yaml`](panoptes.yaml) | The v0.1 reference config: `dev` live (cloudwatch + sentry + http-health → victoriametrics), `stage`/`prod` inert, two-tier dashboards, stdio MCP. |
| [`.env.example`](.env.example) | The `${VAR}` set the config interpolates (read-only credentials + the injection pointers). Copy to `.env`; never commit a populated `.env`. |

## The injection contract (how a real consumer wires its own pack)

Injection is driven by two pointers — neither bundles the pack into Panoptes:

1. **Dashboards** are resolved from `dashboards.consumer_pack.path` (this dir, mounted
   read-only at `/packs/consumer` in compose). The Grafana provider globs
   `dashboards/**/dashboard.json` across the core dir **and** this injected dir.
2. **Code** (the optional custom adapters + MCP tools in `pack.py`) is loaded by the
   `PANOPTES_CONSUMER_PACK` env var naming the pack module. On import the pack
   registers its adapters via the core registries and registers its MCP tools via the
   `register_tools(mcp_server)` hook. `core/` never imports the pack statically.

A real consumer therefore:

```yaml
# the consumer's own panoptes.yaml
dashboards:
  consumer_pack:
    path: ${CONSUMER_PACK_DIR}      # -> /path/to/<consumer-repo>/ops/panoptes
```

```dotenv
# the consumer's own .env
CONSUMER_PACK_DIR=/path/to/<consumer-repo>/ops/panoptes
PANOPTES_CONSUMER_PACK=/packs/consumer/pack.py
```

and writes its own `pack.py` against the same `register_tools(mcp_server)` contract —
registering, say, a domain tool that returns its own typed signal — **in its own
repo**. The v0.2 hosted path swaps the mounted `path:` for a pinned
`git: { repo, ref, subdir }` injection; the loading mechanism is otherwise identical.

## The boundary this proves

The repo's CI guards assert the boundary is real, not aspirational:

- **Structural purity** — no `core/**/*.py` imports from `examples/`.
- **Additive injection** — loading this pack via the hook adds *exactly* the demo
  tool/adapter on top of the core-only baseline; building the server with the hook
  unset yields precisely the core-only set. Injection is purely additive and
  reversible — it is **not** bundling.
