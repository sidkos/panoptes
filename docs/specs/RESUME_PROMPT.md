# Resume Prompt — Continue From Where We Stopped (Panoptes)

> Session-close handoff written 2026-06-07. Read this first, then the linked docs. It is the
> single entry point for the next session to pick up the Panoptes work with no lost context.

## What Panoptes is (one line)

A standalone OSS (Apache-2.0) **normalizing monitoring meta-layer** — adapters pull read-only
from the tools you already run (CloudWatch, Sentry, Kubernetes, Prometheus, …) into one
canonical store, served through **two faces that never drift**: a single-pane Grafana and an
SSO-gated **MCP server**. Its own dedicated EKS home (failure-domain independence). Separate git
repo `github.com/sidkos/panoptes`, symlinked + gitignored inside FIDA at `/panoptes` (work
in-place on `main`; commits are allowed in this repo).

## Where we are (state as of this handoff)

- **v0.1 + v0.2 + v0.3 are all SHIPPED + pushed** (CI-green): the core adapter framework, the
  hosted EKS module + Helm chart + GHCR image, and the depth/genericity work (the genericity
  thesis is proven — byte-identical core baseline across two unrelated consumer packs).
- **The hosting module + Helm chart are now DEPLOY-PROVEN end-to-end.** Over 2026-06-04 a series
  of live `apply → verify → destroy` cycles on the FIDA AWS account hardened them from
  "`helm template`-tested only" to "a plain `terraform apply` + `helm install` stands up a fully
  working stack with all 8 components functional out-of-box, zero orphans." Fixed + pushed
  CI-green this arc: ARM `ami_type`, `cluster_version` → 1.34, removed the two state-leaking
  sensitive outputs, two HIGH network findings (private subnets + NAT; the EKS API endpoint is
  now private + CIDR-allowlisted, never `0.0.0.0/0`), and the 6 app-deploy gaps (Dockerfile
  non-root, VM `runAsUser`/`fsGroup`, EBS CSI addon + IRSA + gp3 default StorageClass, `node_min=2`,
  Grafana datasource provisioning, opt-in ClusterIssuer).
- **Docs are synced** to that reality: `docs/DEPLOYMENT.md` (NEW — the operator runbook),
  `docs/ARCHITECTURE.md`, `docs/IAM.md`, `docs/ROADMAP.md` (v0.2 marked DEPLOY-PROVEN),
  `README.md`, `modules/stack/README.md`.
- **Git state: the docs commits are LOCAL, not yet pushed.** `ec57cc1` (docs) and this
  resume-prompt commit sit on local `main` ahead of `origin/main`. Working tree otherwise clean.

## ⏭️ Immediate next actions (do these first)

1. **Push the pending docs commits** and confirm CI: `git push origin main`, then
   `gh run watch` the latest run (all 8 jobs should be green; the changes are docs-only).
2. **Start the testing phase** — the real work that hasn't been done yet. Full brief in
   [`NEXT_SESSION_TESTING_PROMPT.md`](NEXT_SESSION_TESTING_PROMPT.md); in short:
   - **Prove Panoptes observes a REAL environment** (the core value prop, NOT yet proven — every
     deploy so far used the self-contained demo config: the collector http-health-probes the MCP's
     own `/healthz`). Point a deployed stack at the actual FIDA `dev` env with REAL sources
     (`cloudwatch`, `sentry`, the `kubernetes` source for the FIDA cluster) wired via the IRSA
     `read_role_arns`, and verify real signals flow source → store → Grafana + MCP.
   - **Drive the hosted MCP against real data** + confirm the GitHub oauth2-proxy gate actually
     blocks an unauthenticated request (the security boundary, not just "the pod runs").
   - **(Stretch) Automate the deploy → test → teardown cycle** (Terratest / a synchronous Python
     harness / a gated `workflow_dispatch`) so the deploy-validation discipline is repeatable.
     Keep it MANUAL/gated — a real EKS cluster per run costs money; never auto-fire on push.

## Operational quick-reference (these bit us — see DEPLOYMENT.md for detail)

- **AWS:** account `398265531296`, profile `amplify-admin` (`aws sso login --profile amplify-admin` first).
- **EKS API allowlist + your IP:** the `cluster_endpoint_public_access_cidrs` var has no default
  and rejects `0.0.0.0/0`; your operator public IP MUST be in it or the terraform `kubernetes`/`helm`
  providers + `kubectl` time out. If your IP shifts, re-apply with the new `/32` or
  `aws eks update-cluster-config`.
- **Two-phase apply:** the `helm`/`kubernetes` providers read `~/.kube/config` — target the node
  group, `aws eks update-kubeconfig --name panoptes`, then the full apply.
- **Image for testing:** `docker buildx build --platform linux/arm64 … --push` to an in-account
  ECR repo (the node role has ECR read) — no GHCR token / release tag / pull secret needed.
- **Cost:** a full deploy is a few dollars; ALWAYS `destroy` + verify zero orphans when done.
- **Gates before push:** run BOTH `./scripts/precommit.sh sca` AND `./scripts/precommit.sh infra`
  (the terraform/helm gate does NOT cover Python lint/types). Docs-only `.md` commits skip the gate.

## Key references

- [`docs/DEPLOYMENT.md`](../DEPLOYMENT.md) — the full apply → verify → destroy runbook + the
  8-component verification table.
- [`NEXT_SESSION_TESTING_PROMPT.md`](NEXT_SESSION_TESTING_PROMPT.md) — the detailed testing brief.
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) / [`docs/IAM.md`](../IAM.md) — topology + the
  single read-only credential domain.
- Module: `modules/stack/` (+ `modules/stack/README.md`); chart: `charts/panoptes/`.
- FIDA memory (loaded automatically next session): `project_panoptes.md` (the full session record),
  `feedback_deploy_validate_iac_charts` (deploy-validate IaC/charts — a real apply finds what
  static gates can't), `feedback_panoptes_agent_repo_guard` (how to run FIDA agents/skills against
  this repo), `feedback_multilens_audit_iac_security`.
