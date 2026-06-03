# Panoptes — Adapters & Configuration

Adapters are how Panoptes plugs into the monitoring tools you already run. There
are four plug-planes (Source, Store, Notifier, Dashboard). Adding a tool is one
adapter class implementing the plane's `Protocol`, plus a registry entry, plus a
config block.

---

## 1. The plug-plane contracts

Sketches (Python `Protocol`s) — the founding shape, not final signatures. All
types are precise; no untyped escape hatches.

```python
# --- Source: read signals FROM a monitoring tool ---------------------------
class Source(Protocol):
    type: str                                       # config discriminator, e.g. "cloudwatch"
    def capabilities(self) -> set[SignalKind]: ...  # {METRIC, LOG, TRACE, INCIDENT}
    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]: ...
    def health(self) -> SourceHealth: ...           # is the upstream reachable?

# --- Store: persist + query the canonical model ----------------------------
class Store(Protocol):
    type: str
    def write(self, signals: list[CanonicalSignal]) -> None: ...
    def query(self, expr: MetricQuery) -> list[MetricSeries]: ...

# --- Notifier: deliver alerts ----------------------------------------------
class Notifier(Protocol):
    type: str
    def notify(self, alert: Alert) -> None: ...

# --- Dashboard: provision visualizations as code ---------------------------
class DashboardProvider(Protocol):
    type: str
    # DashboardPack carries id, tier, json_path
    # (see specs/v0.1_core_local_proof.md § Query / aggregation types)
    def provision(self, packs: list[DashboardPack]) -> None: ...
```

**Capability negotiation.** Routing logic asks each active Source what it
`provides` before dispatching a query. Asking for a signal kind no configured
source provides returns an explicit "no source for X", never a silent empty
result.

**Read-only Sources.** A Source never writes to the upstream tool. This is what
makes it safe to point Panoptes at production-shaped infrastructure.

---

## 2. The adapter catalog

"Live target" = a reference environment already runs this tool, so the adapter
has something real to develop and test against. Everything is greenfield, so all
adapters are *to build*; the column that matters is build effort and whether a
live target exists.

| Adapter | Plane | Provides | Live target on `dev`? | Build effort |
|---------|-------|----------|-----------------------|--------------|
| **cloudwatch**      | source | metric, log | ✅ | thin (boto3) |
| **sentry**          | source | incident, metric (derived `panoptes_sentry_incident_count` gauge) | ✅ | thin (REST API) |
| **kubernetes**      | source | metric, incident (events → `incident`, resource state → `metric`) | ✅ (EKS) | medium (k8s client) |
| **http-health**     | source | metric (from `/health`) | ✅ | trivial |
| **prometheus**      | source | metric             | ❌ (not deployed) | medium — **v0.3 SHIPPED** |
| **loki**            | source | log                | ❌ | **v0.3 SHIPPED** |
| **tempo / jaeger**  | source | trace              | ❌ (no tracing yet) | later (v0.3 explicitly defers `tempo`) |
| **datadog**         | source | metric, log        | ❌ | optional |
| **victoriametrics** | store  | —                  | ❌ | deploy + adapter (default store) |
| **passthrough**     | store  | —                  | n/a | trivial (no persistence) |
| **logging**         | notifier | —                | n/a | trivial (stdout) |
| **sns**             | notifier | —                | ✅ | thin |
| **slack**           | notifier | —                | ✅ | thin |
| **grafana**         | dashboard | —               | ❌ | deploy + provider wiring |

> A consumer that runs game-server fleet technology (e.g. Agones) plugs in its
> own **consumer-pack source adapter** for it — that adapter is domain-specific
> and lives in the consumer's repo, not in this core catalog (it pairs with the
> consumer-tier Fleet dashboard in [`DASHBOARDS.md`](DASHBOARDS.md) §1). Core
> ships only consumer-agnostic adapters.

**v0.1 build set** (live targets on `dev`): `cloudwatch`, `sentry`,
`http-health` sources; `victoriametrics` store; `grafana` dashboard; the
`logging` notifier (`sns`/`slack` deferred to v0.2). See [`ROADMAP.md`](ROADMAP.md).

**v0.2 build set (SHIPPED):** the `kubernetes` source (nodes/pods/events →
`panoptes_k8s_*` derived gauges + `Warning`-event `incident`s, `capabilities() ==
{metric, incident}`, the `cluster` label distinguishing observed vs Panoptes' own;
`core/sources/kubernetes.py`), and the `sns` + `slack` notifiers (alert delivery to
Panoptes-owned channels; `core/notifiers/{sns,slack}.py`). The no-write guard's
suppression is **path-scoped** to `core/notifiers/sns.py` — `sns.publish` anywhere else
(esp. `core/sources/`) still red-bars (`tests/unit/test_no_write_actions_guard.py`).

**v0.3 build set (SHIPPED):** the `prometheus` source (read-only PromQL scrape via httpx
`GET /api/v1/query_range` → `MetricSignal` with `env` stamped, `capabilities() == {metric}`;
`core/sources/prometheus.py`) and the `loki` source (read-only `GET
/loki/api/v1/query_range` → `LogSignal`, `capabilities() == {log}`; `core/sources/loki.py`).
Both are read-only (httpx GET — the documented read-only known-miss, same as the v0.1
sentry/http-health sources: the read-only endpoint/token is the authoritative control, not
the boto3-shaped no-write grep). **`tempo` is explicitly NOT shipped in v0.3** — with `loki`
(LOG) added and `tempo` deferred, the union of `capabilities()` across all core sources stays
exactly `{metric, log, incident}` (NO trace), so the v0.1/v0.2 "no trace source" negative
path remains true (`tests/unit/test_source_capabilities.py`). The `cloudwatch` source also
gains a v0.3 opt-in COST read path (CE `GetCostAndUsage` + budgets `DescribeBudget` →
`panoptes_cost_*` gauges, rate-limited to once per poll interval) backing the real `get_cost`
tool — see [`DASHBOARDS.md`](DASHBOARDS.md) §1 row 9 + [`IAM.md`](IAM.md) §A.

> **Consumer-pack source adapters (v0.3 — fixtures under `examples/`, NOT core).** Two
> unrelated consumer fixture packs demonstrate that a consumer extends a core plane without
> touching core: the **fleet** pack (`examples/consumer-fleet-pack/pack.py`) registers a
> `fleet` source that BUILDS ON the core `prometheus` source (it composes `PrometheusSource`
> and relabels the scrape into `panoptes_fleet_*` gauges), and the **pipeline** pack
> (`examples/consumer-pipeline-pack/pack.py`) registers a STANDALONE `pipeline` source over a
> generic pipeline-metrics endpoint. Both register on the core `SOURCES` registry purely
> additively via the pack's `import` (consumer→core only — the structural guard enforces
> `core/`↛`examples/`), and both are read-only. These adapters are consumer-owned and stay
> out of the core catalog above. See [`DASHBOARDS.md`](DASHBOARDS.md) §1 (rows C2/C-pipeline)
> and the genericity proof in `tests/unit/test_genericity_two_consumers.py`.

> **cloudwatch capability set (v0.1):** `cloudwatch` provides `{metric, log}` in
> v0.1. Normalizing CloudWatch alarms into `incident` signals is a **v0.2**
> capability — it is intentionally NOT in the v0.1 capability set so the declared
> `capabilities()` and the config `provides` agree everywhere.

---

## 3. Configuration schema

A single declarative file selects environments, sources, store, notifiers, and
dashboards. This is the entire "plug in a tool" UX.

> **This sketch shows the full (v0.2-superset) schema** — the complete surface a
> hosted deployment can use. It is **not** the v0.1 build set: it shows
> `mcp.transport: http` + `auth: sso` (the v0.2 hosted form; v0.1 uses
> `transport: stdio`), an `mcp.tools` list including the v0.2 tools `get_slo` /
> `compare_envs`, and an `sns` notifier (v0.2). The **v0.1 example config** lives
> in the spec's `## Configuration` (`examples/demo-pack/panoptes.yaml`): it uses
> `transport: stdio`, lists only the v0.1-implemented tools, and uses the
> `logging` notifier (the only v0.1 notifier). Both are correct for their version.

```yaml
panoptes:
  # --- environments: env is a first-class dimension ----------------------
  environments:
    - name: dev
      enabled: true
      sources:
        - { type: cloudwatch, provides: [metric, log],
            config: { assume_role_arn: "${DEV_PANOPTES_READ_ROLE}",
                      external_id: "${PANOPTES_EXTERNAL_ID}",   # SHOULD be set when assume_role_arn is — matches the IAM.md trust-policy condition
                      region: "${AWS_REGION}",
                      namespaces: [AWS/Lambda, AWS/NetworkELB] } }
        - { type: sentry, provides: [incident, metric],   # incident native + derived panoptes_sentry_incident_count gauge
            config: { org: "${SENTRY_ORG}", project: "${SENTRY_PROJECT}", environment: dev } }
        - { type: http-health, provides: [metric],
            config: { url: "${DEV_HEALTH_URL}" } }
    - name: stage
      enabled: false                # wired but inert until the env exists
    - name: prod
      enabled: false

  # --- store: Grafana and MCP both read this -----------------------------
  store:
    type: victoriametrics
    config: { retention: 15d }

  # --- alerting ----------------------------------------------------------
  notifiers:
    # - { type: logging }                          # v0.1 default (stdout/log); the only v0.1 notifier
    - { type: sns, config: { topic_arn: "${PAGER_TOPIC_ARN}" } }   # sns is a v0.2 notifier

  # --- dashboards as code (two tiers) ------------------------------------
  dashboards:
    provider: grafana
    env_variable: true             # template variable to switch/compare envs
    core_packs: [errors-sentry, logs, overview]   # tier 1: ship inside Panoptes
    consumer_pack:                                 # tier 2: external, injected — one of:
      path: /packs/consumer                        # local/compose: mounted dir
      # git: { repo: "...", ref: "<full-commit-sha>", subdir: "ops/panoptes" }  # hosted/Terraform — immutable pin only

  # --- SLOs --------------------------------------------------------------
  slos:
    - name: health-up
      query: 'avg(panoptes_health_up)'
      target: 0.99

  # --- MCP: same data, machine-readable ----------------------------------
  # Only core/discovery tools are declared here. Consumer-specific tools
  # (e.g. get_allocator_pressure) are registered by the injected pack's
  # pack.py — not enumerated in core config.
  mcp:
    transport: http               # v0.2 hosted form; SSO/OIDC enforced at the proxy. v0.1 uses transport: stdio.
    auth: sso                     # no anonymous access (v0.2 hosted)
    # FULL catalog incl. v0.2 tools. The v0.1 example config (spec ## Configuration)
    # lists only v0.1-implemented tools (discovery + describe_health/search_*/query_metric)
    # and defers get_slo/compare_envs to v0.2 (see ROADMAP).
    tools: [describe_health, search_incidents, search_logs,
            query_metric, get_slo, compare_envs]
```

**Conventions**

- Secrets and per-environment endpoints come from the deployment environment
  (`${VAR}` interpolation), never inlined.
- Disabling a tool = remove or `enabled: false` its block.
- Requesting an unknown `type` fails fast with a clear "no adapter for type X",
  telling you exactly what to build.
- **`provides:` is advisory documentation, not the authority.** The adapter's
  runtime `capabilities()` is the single source of truth for capability
  negotiation. Config resolution **cross-checks** the declared `provides:` list
  against the adapter's `capabilities()` and **fails fast** if they disagree
  (e.g. a `sentry` source configured `provides: [incident]` while its
  `capabilities()` returns `{incident, metric}`), so a stale `provides:` can
  never silently mis-route a query into a "dashboard-empty" result. Omitting
  `provides:` is allowed — `capabilities()` is then used directly.
- **`external_id`** is optional but **SHOULD** accompany `assume_role_arn`: it is
  the value the cross-account trust policy requires (IAM.md §A confused-deputy
  guard). When `assume_role_arn` is set without `external_id`, the assume-role
  call will fail against a trust policy that pins `sts:ExternalId`.
- **Core packs ship in Panoptes; consumer packs are external.** Tier-1
  `core_packs` are provisioned from `core/dashboards/`. The tier-2
  `consumer_pack` is **not** stored in Panoptes — it lives in the consumer's own
  repo and is injected at deploy time (a mounted dir locally, or a pinned git
  repo+ref+subdir when hosted). The injected dir also carries the consumer's
  optional custom adapters + MCP tools (`pack.py`). See
  [`DASHBOARDS.md`](DASHBOARDS.md) §4.
