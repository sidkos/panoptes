# Panoptes — CONTEXT (domain glossary)

The shared vocabulary for Panoptes. Architecture-level terms (**module · interface ·
depth · seam · adapter · leverage · locality**) come from the
`improve-codebase-architecture` skill's LANGUAGE.md; this file names the **domain**.
Founding contracts live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); decisions that
should not be re-litigated live in [`docs/adr/`](docs/adr/). New names introduced by a
deepening are recorded here as the decision crystallizes.

## The stack

- **single pane** — one Grafana + one MCP server reading one normalized store across all
  environments. The pane is the product, not another error tracker.
- **failure-domain independence** — Panoptes runs in its own always-on home, never inside
  an observed cluster/VPC, so it survives the thing it monitors.
- **env (first-class label)** — every signal is tagged `env=dev|stage|prod`; the one label
  that drives Grafana's template var, the MCP tools' `env` argument, and per-env alerts/SLOs.
- **two faces, one store** — Grafana (the human face) and the MCP server (the LLM face) are
  both thin readers over the same store, built by the same `build_server` tool table, so
  they cannot diverge.

## The four plug-planes

Each plane is a small `@runtime_checkable Protocol`; a new tool is one class + a registry
entry. Adapters are concrete classes (no base classes — see ADR 0001).

- **Source** — reads signals *from* a monitoring tool (cloudwatch, sentry, http-health,
  kubernetes, prometheus, loki) and normalizes them to the canonical model.
- **Store** — persists + queries the canonical model (victoriametrics default, passthrough).
- **Notifier** — delivers an `Alert` to a Panoptes-owned channel (logging, sns, slack).
- **Dashboard** — provisions visualizations as code (grafana).
- **capability negotiation** — each Source declares `capabilities()` (its signal kinds); a
  query for a kind no source provides returns a clean "not available", never an empty guess.
  The union across core sources is exactly `{METRIC, LOG, INCIDENT}` (no TRACE — tempo deferred).
- **core vs consumer pack** — *core* is consumer-agnostic (generic adapters + dashboards).
  A *consumer pack* (a config + dashboard JSON + optional Source/Notifier + the MCP tools it
  registers) lives in the consumer's repo and is **injected** at deploy time via the
  `PANOPTES_CONSUMER_PACK` hook — `core/` never imports it. Proven by the genericity test:
  the core registry baseline is byte-identical across two unrelated consumer injections.

## The canonical signal model

- **MetricSignal / LogSignal / IncidentSignal / TraceSignal** — the four signal kinds
  (`core/model.py`); metrics are the spine in the store, the rest queried live by default.
- **SourceHealth** — a `(reachable, detail, checked_at)` reachability result a Source
  returns from `health()`.

## Deepening seams (added 2026-06-04)

- **health probe** — a read-only, **no-raise** call a Source makes to its upstream to
  determine reachability; any exception becomes `SourceHealth(reachable=False, …)`. Owned by
  the `probe_health` seam (`core/sources/probe.py`).
- **no-`str(exc)`-leak invariant** — `SourceHealth.detail` must never contain verbatim
  `str(exc)`: a boto3 `ClientError` / httpx error can carry a role ARN, bearer token, or
  endpoint that reaches the MCP-visible `describe_health` rollup. The detail carries only a
  label + `type(exc.__cause__ or exc).__name__`. Concentrated in `probe_health` (ADR 0001).
- **gauge read / series read** — the two store-read contracts behind `QueryContext`
  (`read_gauge` → latest scalar `float | None`, swallows `CapabilityError`; `read_series` →
  `list[MetricSeries]`, propagates `CapabilityError` so a fan-out can mark an env down). Both
  own the PromQL-injection escape — one home for that security invariant (ADR 0004).
- **invoker derivation** — the transport-free, uniform-shape `_ToolCallable` test seam
  produced automatically from a tool function's `inspect.signature` (`_make_invoker`), so
  tests invoke tools synchronously without FastMCP's async transport.
- **tool-param rename** — a deliberate mismatch between a tool fn's MCP-facing parameter
  name and the core fn's (e.g. tool `metric` → core `name`); handled inside the tool fn.
- **PollGate** — a value type that gates a **sub-fetch** to at most once per interval via an
  injectable clock, advancing only on explicit `mark_done()` so a failed read retries next
  cycle. Names the **cost cadence** (Risk G3): Cost Explorer bills per request, so the
  CE/budgets sub-fetch polls hourly, not every cycle.
