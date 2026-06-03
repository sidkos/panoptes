# Panoptes — Dashboards & the MCP Surface

Grafana and the MCP server are **two faces of one normalized store**. Every
dashboard has a parallel MCP tool by construction: define a signal once in an
adapter, and it shows up on a panel *and* as queryable data for an LLM.

---

## 1. Dashboard catalog — two tiers

Dashboards come in **two tiers**, with two different homes:

- **Tier 1 — standard / core packs** (rows marked *core*). These ship **inside
  Panoptes** under `core/dashboards/` and are **always provisioned**. They apply
  to any AWS/Sentry/K8s consumer (errors, logs, overview now; k8s, compute,
  datastore, networking, cost, slo later).
- **Tier 2 — consumer packs** (rows marked *consumer*). These are **NOT stored in
  Panoptes**. They live in the **consumer's own repo**, version-controlled there
  as code (e.g. `<consumer>/ops/panoptes/dashboards/<name>/dashboard.json`), and
  are **injected into Panoptes at deploy time** via a config pointer (see §4). The
  game-platform-flavored rows below (matchmaking/allocator, fleet, game/business)
  are a *reference consumer's* dashboards — they are consumer-owned, not part of
  Panoptes core, and are not built into this repo.

Each dashboard maps to the source adapter(s) that feed it and the MCP tool that
returns the same data programmatically.

| # | Dashboard | Tier | Source adapter(s) | Key panels | MCP tool |
|---|-----------|------|-------------------|------------|----------|
| 1 | **Errors / Sentry** (`errors-sentry`) | core | sentry | error rate, top issues, tag breakdown, by env | `search_incidents(env, window, tag, level)` |
| 2 | **Logs** | core | cloudwatch (logs) | log stream, error-log rate, full-text search | `search_logs(env, query, window, level)` |
| 3 | **Overview / Single Pane** | core | rollup of all | per-env health traffic-light, top alerts/errors, key SLOs | `describe_health(env)` |
| 4 | **Kubernetes** (v0.2 — SHIPPED) | core | kubernetes | node count, pod restarts/CrashLoops, pending pods | `get_cluster_state(env)` |
| 5 | **Compute** | core | cloudwatch | invocations/errors/throttles/p99 across functions | `query_metric(...)` |
| 6 | **Datastore** | core | cloudwatch | consumed capacity, throttles, latency per table | `query_metric(env, metric, dims)` |
| 7 | **API gateway** | core | cloudwatch | 4xx/5xx, request count, latency | `query_metric(...)` |
| 8 | **Networking / Certs** | core | cloudwatch | LB healthy/unhealthy hosts, cert days-until-expiry | `query_metric(...)` |
| 9 | **Cost** (v0.3) | core | cloudwatch (CE/budgets) | budget burn, per-service spend | `get_cost(env, window)` |
| 10 | **SLO / Golden Signals** (v0.2 — SHIPPED) | core | derived | RED/USE rollup, error budgets | `get_slo(name, env)` |
| C1 | **Matchmaking / Allocator** | consumer | cloudwatch + sentry | allocator errors/throttles, success vs 429/503, time-to-match | `get_allocator_pressure(env, window)` |
| C2 | **Fleet / Agones** | consumer | agones (prometheus) | ready/allocated/reserved, ready-to-serve %, 429 rate | `get_fleet_health(env)` |
| C3 | **Game / Business** | consumer | http-health + sentry | active matches, connections, match duration, registrations | `get_game_metrics(env)` |

*core* packs ship with Panoptes and apply to any AWS/Sentry/K8s consumer.
*consumer* packs (C-prefixed) are a reference game-platform example that **lives in
the consumer's own repo** and is injected at deploy time — they demonstrate the
extension mechanism, not core scope. Consumer-specific MCP tools (e.g.
`get_allocator_pressure`) are registered by the injected consumer pack's optional
`pack.py`, not built into core.

**v0.1 scope (dev only):** the three core packs — **Errors**, **Logs**,
**Overview** — ship in Panoptes and are always provisioned. The consumer tier is
exercised by injecting a **generic, brand-neutral demo pack** (a `demo` dashboard
+ a synthetic tool) from a mounted directory, proving the injection mechanism
end to end without baking any domain content into Panoptes. A real consumer (the
reference game platform) would instead point the injection config at its own repo
dir carrying the Matchmaking/Allocator pack. All core packs are backed by live
CloudWatch + Sentry data.

---

## 2. The MCP tool surface

Three categories, so that any client (Claude or otherwise) can both **explore**
and **query** without hardcoded prompts.

### Discovery / parity
- `describe_signal_catalog()` → what environments, sources, metrics, and
  dashboards exist right now.
- `list_dashboards()` → the dashboard catalog above.
- `get_dashboard_data(id, env)` → the *underlying data* of a named Grafana
  dashboard as JSON, so an LLM reads exactly what a human sees on the panel.

### Query (the table's right column)
Generic where it makes sense. **Convention: every tool takes `env` as its first
argument** (all tools accept and respect an `env` argument), for a predictable
LLM-facing surface:
- `query_metric(env, name, window, filters)` — PromQL-backed metric query.
- `search_logs(env, query, window, level)`.
- `search_incidents(env, window, tag, level)`.

Synthesized where one question deserves one call.
- **Core (v0.1):** `describe_health(env)` — the "one thing to look at" rollup.
- **Core (v0.2 — SHIPPED):** `get_slo(name, env)` — the SLO rollup (objective vs.
  actual + error-budget remaining) that ships with the SLO dashboard (catalog row 10).
  Promoted out of `_V0_2_STUB_TOOLS` to a real registrar (`core/mcp/tools_query.py`).
- **Core (deferred):** `get_cost(env, window)` (v0.3) — the only remaining call-time stub;
  it ships with the Cost dashboard (catalog row 9) + the CE/budgets read grant in v0.3.
- **Core (v0.2 — SHIPPED, ships with the `kubernetes` source):** `get_cluster_state(env)`
  — node count, pod restarts/CrashLoops, pending pods (the parallel tool for the
  Kubernetes dashboard, catalog row 4), rendered from the stored `panoptes_k8s_*` gauges
  (two-faces-one-store parity). Listed here so the "every dashboard has a parallel MCP
  tool" invariant resolves; introduced alongside its source in v0.2.
- **Consumer-pack-registered examples** (registered by an injected consumer pack,
  not core): `get_fleet_health(env)`, `get_allocator_pressure(env, window)`,
  `get_game_metrics(env)`.

### Cross-environment
- `compare_envs(metric, window)` (v0.2 — SHIPPED) — same signal across dev/stage/prod.
  Reuses the v0.1 `env="all"` fan-out + per-env error markers; promoted out of
  `_V0_2_STUB_TOOLS` to a real registrar (`core/mcp/tools_query.py`).

### Contract — fully read-only
- The MCP server is **fully read-only**. Every tool is read-only with respect to
  observed systems, and there is **no write path of any kind** — no dashboard
  authoring, no save-to-repo, no runtime mutation. New dashboards are authored as
  code in a repo and injected at deploy time (§4), never created through MCP.
- All tools accept and respect an `env` argument (or `"all"`).
- Tools fail explicitly when a required source/capability isn't configured —
  never a silent empty result.

---

## 3. Access control

The MCP server is hosted over streamable HTTP and is **SSO-gated — no anonymous
access** (OAuth 2.1 / OIDC against an SSO IdP, enforced by an auth proxy in
front of the server). A local **stdio** wrapper exists for development. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) §6.

---

## 4. Dashboard packs as code — two tiers, deploy-time injection

Every dashboard is versioned JSON using the `env` template variable so one
dashboard serves all environments. There are two tiers with two homes, and the
Grafana provider provisions **both** by globbing
`dashboards/**/dashboard.json` across the core dir and the injected consumer dir
— no click-ops, no runtime mutation.

**Tier 1 — core packs.** Ship inside Panoptes under `core/dashboards/<pack>/` and
are always provisioned.

**Tier 2 — consumer packs (injected, not bundled).** The consumer keeps its
dashboards in **its own repo** (e.g. `<consumer>/ops/panoptes/dashboards/<name>/dashboard.json`).
Panoptes is handed a **pointer** to that pack at deploy time via config:

```yaml
dashboards:
  provider: grafana
  core_packs: [errors-sentry, logs, overview]   # tier 1, from Panoptes
  consumer_pack:                                  # tier 2, external — one of:
    path: /packs/consumer                         # local/compose: mounted dir
    # git: { repo: "...", ref: "<full-commit-sha>", subdir: "ops/panoptes" }  # hosted/Terraform — immutable pin only
```

- **Local / compose:** the consumer's pack dir is bind-mounted read-only into the
  stack — `${CONSUMER_PACK_DIR:?}:/packs/consumer:ro` in `docker-compose.yml` —
  and `consumer_pack.path` points at the mount.
- **Hosted / Terraform (v0.2):** `consumer_pack.git` names a repo + ref + subdir;
  the deploy pulls it (a read-only `git fetch` at the pinned ref) at
  `terraform apply`. The consumer's dashboards stay versioned in the consumer
  repo; each deploy pins a ref. **The `ref` MUST be an immutable pin — a full
  commit SHA or a verified tag, never a mutable branch name like `main`.**
- The same injected dir also carries the consumer's **optional `pack.py`** (custom
  Source/Notifier adapters + the MCP tools it registers), loaded the same way.
  Because `pack.py` is **executed in the Panoptes collector/MCP process**, the
  pinned ref is a **trust boundary**: pulling a moving branch would be
  arbitrary-code-execution-on-deploy. Mitigations, in order of strength:
  - **(a) A full commit SHA pin is REQUIRED** (never a mutable branch like
    `main`). Git's content-addressing makes the fetched tree integrity-checkable
    against the SHA: a compromised host cannot serve a different tree under the
    same SHA without git's own object verification rejecting it. **Caveat — the
    integrity guarantee is only as strong as the hash.** Git's default object hash
    is **SHA-1**, which is collision-vulnerable (SHAttered, 2017); a sufficiently
    resourced attacker could in principle craft a colliding tree. Two hardenings
    apply: (i) modern git enables **`sha1dc`** (hardened SHA-1 with collision
    detection) by default, which rejects known collision-attack patterns; and (ii)
    repositories using git's **SHA-256 object format** raise the bar substantially.
    Treat the SHA pin as a strong-but-not-absolute integrity control, and pair it
    with mitigations (b)-(d) below — do not rely on the SHA alone for packs pulled
    from a host outside the deployer's control.
  - **(b) Fetch over an authenticated transport** — HTTPS with TLS verification
    or SSH with a pinned host key — so the SHA is resolved from a host the
    deployer trusts, not a man-in-the-middle.
  - **(c) Verifying a detached signature (signed tag / `cosign`) of the fetched
    subdir is RECOMMENDED** for packs pulled from a host outside the deployer's
    control, before `pack.py` is loaded.
  - **(d) The deployer MUST review the diff of the fetched subdir before bumping
    the pin** — this is a required gate, specified as a control in
    [`IAM.md`](IAM.md) §C (the pin is a code-execution trust boundary).

  The `git` injection variant is **parsed-but-deferred to v0.2** (v0.1 uses only
  the mounted-dir injection), so v0.1 is unaffected — but the contract above must
  hold before v0.2 ships the git path.

**Authoring a new dashboard (normal git, no PR into Panoptes).** Add the JSON to
the consumer repo's pack dir → commit it there → deploy. The new dashboard is
injected alongside the standard packs and provisioned automatically. There is
**no PR into Panoptes, no MCP write, and no runtime mutation** of a running
Grafana — authoring is just the consumer's ordinary git-as-code workflow, and the
deploy-time `git fetch` of the pinned ref is read-only deployment plumbing (see
[`IAM.md`](IAM.md)).
