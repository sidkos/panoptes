# Next-Session Resume Prompt — Start Testing Panoptes (real-environment + MCP + automation)

> Handoff written 2026-06-04 at the end of the "make Panoptes deployable" arc. The hosting
> module + Helm chart are now **deploy-proven** (5 live apply→verify→destroy cycles on the FIDA
> AWS account; all 8 components functional out-of-box; zero orphans). The next phase is
> **TESTING** — moving from "manually deployed once and eyeballed" to repeatable, real-data tests.

## Where we are (read these first)

- `docs/DEPLOYMENT.md` — the operational deploy + test playbook (two-phase apply, the EKS API
  CIDR-allowlist + shifting-IP gotcha, the build-arm64→in-account-ECR image path, helm install
  values + the two out-of-band Secrets, per-component verification, teardown).
- Module: `modules/stack/` — dedicated VPC (public LB/NAT subnets + private node subnets),
  hardened EKS API endpoint (private + `cluster_endpoint_public_access_cidrs` allowlist, no
  default, rejects 0.0.0.0/0), `ebs_csi.tf` (EBS CSI addon + IRSA + gp3 default SC), `node_min=2`,
  `cluster_version=1.34`.
- Chart: `charts/panoptes/` — non-root images, VM `runAsUser`/`fsGroup`, Grafana datasource
  provisioning, opt-in ClusterIssuer.
- The deploy used a **self-contained demo config** (the collector http-health-probes the MCP's
  own `/healthz`; dummy OAuth secrets; placeholder hostname). That proved the PLUMBING — it did
  NOT yet prove Panoptes observing a **real** environment, which is the whole value prop.

## The testing goal — prove Panoptes observes a REAL environment, end-to-end

**Priority A — Real-environment observation (the core proof).** Point a deployed Panoptes at the
actual FIDA `dev` environment and verify real signals flow source → store → Grafana + MCP:

1. Configure REAL sources in the consumer pack / `panoptes.yaml` (not the demo `http-health`):
   `cloudwatch` (metrics + the cost path), `sentry` (incidents), the `kubernetes` source
   (the FIDA EKS cluster state), `prometheus`/`loki` if reachable. Wire the read access via the
   IRSA role's `read_role_arns` (an in-account or cross-account `PanoptesReadRole/dev`) — see
   `docs/IAM.md`.
2. Verify the collector actually ingests real data: real `cloudwatch` series land in
   VictoriaMetrics; `search_incidents` returns real Sentry issues; `get_cluster_state` reflects
   the real cluster. Confirm Grafana panels render the real series via the provisioned datasource.

**Priority B — MCP server against real data.** Drive the hosted, GitHub-gated MCP HTTP transport
with a real MCP client and exercise the tools against the real signals: `describe_health`,
`search_incidents`, `search_logs`, `get_cluster_state`, `get_cost`, `get_slo`, `compare_envs`.
Confirm each returns correct real data and that the GitHub oauth2-proxy gate actually blocks an
unauthenticated request (the security boundary, not just "the pod runs").

**Priority C (stretch) — automate the deploy→test→teardown.** Codify this session's manual cycle
as a repeatable, gated test so the deploy-validation discipline (`feedback_deploy_validate_iac_charts`)
is enforceable: either Terratest (Go) or a synchronous Python harness reusing the
`tests/integration/` patterns, or a `workflow_dispatch` GHA job that stands up a throwaway cluster,
runs the per-component checks from `docs/DEPLOYMENT.md`, and destroys. Keep it MANUAL/gated — a real
EKS cluster per run is expensive; never auto-fire on push.

## Operational reminders (bit us this session)

- **EKS API allowlist + your IP:** your operator public IP must be in
  `cluster_endpoint_public_access_cidrs`, or the terraform `kubernetes`/`helm` providers + kubectl
  time out. If your IP shifts mid-work, re-apply with the new `/32` or
  `aws eks update-cluster-config`. You cannot use `0.0.0.0/0` (the validation rejects it).
- **Two-phase apply:** the `helm`/`kubernetes` providers read `~/.kube/config` — target the node
  group, `aws eks update-kubeconfig --name panoptes`, then the full apply.
- **Image for testing:** `docker buildx build --platform linux/arm64 … --push` to an in-account
  ECR repo (the node role has ECR read) — no GHCR token / release tag / pull secret needed.
- **AWS:** account `398265531296`, profile `amplify-admin` (`aws sso login` first).
- **Cost:** a full run is a few dollars; ALWAYS destroy + verify zero orphans when done.
- **Gates before push:** run BOTH `./scripts/precommit.sh sca` AND `./scripts/precommit.sh infra`.

## Definition of done for the testing phase

Panoptes is shown collecting real `dev` signals (≥2 real source types) into the store, rendering
them in Grafana via the provisioned datasource, and answering MCP tool calls with that real data
over the gated transport — with the unauthenticated path provably blocked — and ideally a
repeatable (gated) deploy→test→teardown harness checked in.
