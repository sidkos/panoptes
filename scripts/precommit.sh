#!/usr/bin/env bash
#
# Panoptes local pre-commit gate. Mirrors the authoritative CI gate
# (`.github/workflows/ci-checks.yml`) 1:1 so "green locally" means "green in CI".
#
# NOTE: never use a `status=` variable — `status` is a reserved word under zsh and
# silently misbehaves. We use `run_status` / `phase` instead.
#
# CI parity map (sca mode step  ->  ci-checks.yml job/step):
#   1. ruff check .                          -> lint-type:  `ruff check .`
#   2. ruff format --check .                 -> lint-type:  `ruff format --check .`
#   3. yamllint .                            -> lint-type:  `yamllint .`
#   4. actionlint (best-effort local)        -> lint-type:  actionlint download-and-run
#   5. mypy --strict .                       -> lint-type:  `mypy --strict .`
#   6. mypy --strict (example packs)         -> lint-type:  one run per `examples/*/pack.py`
#   7. pytest (--cov=core --fail-under=85)   -> unit:       first coverage run
#   8. pytest (--cov=core.sources,core.mcp)  -> unit:       second (--cov-append) run
#   9. boundary guards                       -> guards:     the two purity guards
#  10. brand-neutrality grep                 -> (local-only invariant; CI has its own)
# The integration suite maps to the `integration` job and runs via the `integration`
# mode (Docker-gated), kept out of the default `sca` loop.
#
# The hermetic v0.2 IaC + chart gates (the `terraform` + `helm` CI jobs) are NOT part of
# the default `sca` loop because they need extra binaries (terraform/tflint/helm/
# kubeconform) the venv does not carry; run them explicitly with the `infra` mode below
# when those binaries are installed. The two coverage pytest runs deselect the
# `terraform`/`helm` markers so the default loop never tries to exec a missing binary.

set -euo pipefail

# --- locate repo root + venv tools -------------------------------------------------
# Resolve the repo root from this script's location so the gate works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

VENV_BIN="${REPO_ROOT}/.venv/bin"
if [[ ! -d "${VENV_BIN}" ]]; then
  echo "ERROR: virtualenv not found at ${VENV_BIN}" >&2
  echo "       create it with: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

RUFF="${VENV_BIN}/ruff"
MYPY="${VENV_BIN}/mypy"
PYTEST="${VENV_BIN}/pytest"
YAMLLINT="${VENV_BIN}/yamllint"

for tool in "${RUFF}" "${MYPY}" "${PYTEST}" "${YAMLLINT}"; do
  if [[ ! -x "${tool}" ]]; then
    echo "ERROR: required tool missing: ${tool}" >&2
    echo "       (re)install dev deps: .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
  fi
done

# --- step harness ------------------------------------------------------------------
# Each labelled step prints `[n/total] <name> ... PASS/FAIL (Xs)`, fail-fast.
STEP_INDEX=0
STEP_TOTAL=0
FAILED_STEP=""

# run_step "<human name>" <cmd> [args...]
# Runs the command; on failure prints its captured output and exits non-zero.
run_step() {
  local name="$1"
  shift
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '[%d/%d] %s ... ' "${STEP_INDEX}" "${STEP_TOTAL}" "${name}"

  local start_seconds
  start_seconds="$(date +%s)"
  local output
  local run_status=0
  # Capture combined output so a clean run stays quiet but a failure shows everything.
  output="$("$@" 2>&1)" || run_status=$?
  local elapsed=$(( $(date +%s) - start_seconds ))

  if [[ "${run_status}" -eq 0 ]]; then
    printf 'PASS (%ds)\n' "${elapsed}"
  else
    printf 'FAIL (%ds)\n' "${elapsed}"
    echo "----- ${name} output -----"
    echo "${output}"
    echo "--------------------------"
    FAILED_STEP="${name}"
    exit "${run_status}"
  fi
}

# Brand-neutrality invariant: ZERO literal brand mentions in shipped/test/example code.
# grep exits 1 when there are no matches (the desired state); any match is a failure.
check_brand_neutral() {
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '[%d/%d] %s ... ' "${STEP_INDEX}" "${STEP_TOTAL}" "brand-neutrality (grep -rin fida)"
  local hits
  # v0.2: the brand grep also covers the distributable IaC + chart surfaces. Generated
  # trees (`.terraform/` provider plugins) are excluded — they carry vendored tokens we
  # do not own and would false-positive the gate. Roots that may not exist yet
  # (modules/, deploy/, charts/) are tolerated by grep's missing-path handling.
  hits="$(grep -rin --exclude-dir=.terraform fida \
    core/ tests/ examples/ modules/ deploy/ charts/ 2>/dev/null || true)"
  if [[ -z "${hits}" ]]; then
    printf 'PASS (0s)\n'
  else
    printf 'FAIL (0s)\n'
    echo "----- brand-neutrality violations -----"
    echo "${hits}"
    echo "---------------------------------------"
    FAILED_STEP="brand-neutrality"
    exit 1
  fi
}

# actionlint is best-effort locally (the official installer is download-on-demand in
# CI). Run it when present; otherwise note that CI is the enforcing layer.
run_actionlint_step() {
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '[%d/%d] %s ... ' "${STEP_INDEX}" "${STEP_TOTAL}" "actionlint"
  if command -v actionlint >/dev/null 2>&1; then
    local output
    local run_status=0
    output="$(actionlint -color .github/workflows/* 2>&1)" || run_status=$?
    if [[ "${run_status}" -eq 0 ]]; then
      printf 'PASS (0s)\n'
    else
      printf 'FAIL (0s)\n'
      echo "----- actionlint output -----"
      echo "${output}"
      echo "-----------------------------"
      FAILED_STEP="actionlint"
      exit "${run_status}"
    fi
  else
    printf 'SKIP (actionlint not installed — CI enforces it)\n'
  fi
}

# Each example consumer pack ships a top-level `pack.py` under a HYPHENATED dir, so the two
# `pack.py` files collide as one module name `pack` under a single `mypy .` run (and a
# hyphenated dir can't host an `__init__.py`). They are EXCLUDED from the project-wide
# `mypy --strict .` (pyproject `[tool.mypy] exclude`) and type-checked HERE — one strict
# invocation PER pack — so each keeps full strict coverage without the same-name collision.
run_example_packs_mypy_step() {
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '[%d/%d] %s ... ' "${STEP_INDEX}" "${STEP_TOTAL}" "mypy --strict (example packs)"
  local start_seconds
  start_seconds="$(date +%s)"
  local output=""
  local run_status=0
  local pack
  # One mypy run per pack.py — separate invocations avoid the duplicate-module-name error.
  for pack in examples/*/pack.py; do
    [[ -e "${pack}" ]] || continue
    output+="$("${MYPY}" --strict "${pack}" 2>&1)" || run_status=$?
  done
  local elapsed=$(( $(date +%s) - start_seconds ))
  if [[ "${run_status}" -eq 0 ]]; then
    printf 'PASS (%ds)\n' "${elapsed}"
  else
    printf 'FAIL (%ds)\n' "${elapsed}"
    echo "----- mypy --strict (example packs) output -----"
    echo "${output}"
    echo "------------------------------------------------"
    FAILED_STEP="mypy --strict (example packs)"
    exit "${run_status}"
  fi
}

# --- mode: sca (default) -----------------------------------------------------------
run_sca() {
  echo "Panoptes pre-commit gate — sca (full local mirror of CI)"
  STEP_TOTAL=10
  STEP_INDEX=0
  run_step "ruff check" "${RUFF}" check .
  run_step "ruff format --check" "${RUFF}" format --check .
  run_step "yamllint" "${YAMLLINT}" .
  run_actionlint_step
  run_step "mypy --strict" "${MYPY}" --strict .
  run_example_packs_mypy_step
  run_step "pytest (core coverage >= 85%)" \
    "${PYTEST}" -m "not integration and not terraform and not helm" \
    --cov=core --cov-fail-under=85
  run_step "pytest (sources+mcp coverage >= 80%)" \
    "${PYTEST}" -m "not integration and not terraform and not helm" \
    --cov=core.sources --cov=core.mcp --cov-append --cov-fail-under=80
  run_step "boundary guards" \
    "${PYTEST}" tests/unit/test_core_purity_guard.py tests/unit/test_no_write_actions_guard.py
  check_brand_neutral
  echo "All ${STEP_TOTAL} steps passed."
}

# --- mode: fast --------------------------------------------------------------------
# The tight inner loop: static analysis + guards + brand check, skipping the two
# (slow) coverage pytest runs. Steps 1-6 + 9 + 10 of the sca sequence.
run_fast() {
  echo "Panoptes pre-commit gate — fast (static + guards, no coverage)"
  STEP_TOTAL=8
  STEP_INDEX=0
  run_step "ruff check" "${RUFF}" check .
  run_step "ruff format --check" "${RUFF}" format --check .
  run_step "yamllint" "${YAMLLINT}" .
  run_actionlint_step
  run_step "mypy --strict" "${MYPY}" --strict .
  run_example_packs_mypy_step
  run_step "boundary guards" \
    "${PYTEST}" tests/unit/test_core_purity_guard.py tests/unit/test_no_write_actions_guard.py
  check_brand_neutral
  echo "All ${STEP_TOTAL} steps passed."
}

# --- mode: integration -------------------------------------------------------------
# Docker-gated. Mirrors the `integration` CI job: assert the anti-rot floor (>= 5
# collected) BEFORE the slow container run, with no exit-5 tolerance.
run_integration() {
  echo "Panoptes pre-commit gate — integration (Docker required)"
  echo "[1/2] integration anti-rot floor (>= 5 collected) ... "
  local summary collected
  summary="$("${PYTEST}" -m integration --collect-only -q 2>/dev/null \
    | grep -Eo '[0-9]+(/[0-9]+)? tests? collected' || true)"
  collected="$(printf '%s' "${summary}" | grep -Eo '^[0-9]+' || echo 0)"
  echo "      collected ${collected} integration test(s)."
  if [[ "${collected}" -lt 5 ]]; then
    echo "ERROR: integration anti-rot floor: expected >= 5, collected ${collected}." >&2
    exit 1
  fi
  echo "[2/2] pytest -m integration (testcontainers: VictoriaMetrics + Grafana) ... "
  "${PYTEST}" -m integration
  echo "Integration suite passed."
}

# --- mode: infra -------------------------------------------------------------------
# The hermetic IaC + chart gates, mirroring the `terraform` + `helm` CI jobs. Needs the
# extra binaries (terraform, tflint, helm, kubeconform) installed locally; each half is
# gated on binary presence so a partial toolchain runs only what it can. NO AWS creds,
# NO backend, NO cluster — `init -backend=false`/`validate`/`tflint` for Terraform and
# `helm template | kubeconform -strict` (offline schema validation) for the chart.
run_infra() {
  echo "Panoptes pre-commit gate — infra (hermetic terraform + helm; no creds, no cluster)"
  local ran_any=0

  if command -v terraform >/dev/null 2>&1; then
    echo "[terraform] fmt -check / init -backend=false / validate ..."
    terraform -chdir=modules/stack fmt -check -recursive
    terraform -chdir=modules/stack init -backend=false -input=false
    terraform -chdir=modules/stack validate
    if command -v tflint >/dev/null 2>&1; then
      tflint --chdir=modules/stack
    else
      echo "      (tflint not installed — CI enforces it)"
    fi
    "${PYTEST}" -m terraform
    ran_any=1
  else
    echo "[terraform] SKIP (terraform not installed — CI enforces it)"
  fi

  if command -v helm >/dev/null 2>&1 && command -v kubeconform >/dev/null 2>&1; then
    echo "[helm] lint / template | kubeconform -strict ..."
    # Lint WITH the CI fixture values: values.schema.json requires a non-empty
    # oauth2Proxy.githubOrg (the fail-closed GitHub gate); the default values.yaml ships it
    # empty so a bare `helm lint` fails the schema. Lint against a valid install fixture.
    helm lint charts/panoptes -f charts/panoptes/ci/test-values.yaml
    helm template charts/panoptes -f charts/panoptes/ci/test-values.yaml | kubeconform -strict -summary
    "${PYTEST}" -m helm
    ran_any=1
  else
    echo "[helm] SKIP (helm and/or kubeconform not installed — CI enforces them)"
  fi

  if [[ "${ran_any}" -eq 0 ]]; then
    echo "No infra toolchain found; nothing ran. Install terraform/tflint/helm/kubeconform."
  else
    echo "Infra gate complete."
  fi
}

# --- mode: --fix -------------------------------------------------------------------
# Mutating autofix: format then lint-fix. No tests are run (this only rewrites files).
run_fix() {
  echo "Panoptes pre-commit gate — --fix (mutating autofix; no tests)"
  "${RUFF}" format .
  "${RUFF}" check --fix .
  echo "Autofix complete. Re-run './scripts/precommit.sh' to verify the gate."
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/precommit.sh [MODE]

Mirrors the Panoptes CI gate (.github/workflows/ci-checks.yml) locally.

Modes:
  sca           (default) Full local gate, fail-fast: ruff check, ruff format --check,
                yamllint, actionlint (if installed), mypy --strict, the two coverage
                pytest runs (core >= 85%, sources+mcp >= 80%), the boundary guards,
                and the brand-neutrality grep.
  --fast        Tight loop: static analysis + guards + brand check, skipping the two
                coverage pytest runs.
  integration   Docker-gated: assert the >= 5 anti-rot floor, then run
                `pytest -m integration` (spins VictoriaMetrics + Grafana containers).
  infra         Hermetic IaC + chart gates (mirrors the CI `terraform` + `helm` jobs):
                terraform fmt/init -backend=false/validate/tflint + helm lint +
                helm template | kubeconform -strict. Each half is gated on its binaries
                being installed. NO AWS creds, NO backend, NO cluster.
  --fix         Mutating autofix only: `ruff format .` then `ruff check --fix .`.
  --help, -h    Show this help.

All modes exit non-zero on the first failure.
USAGE
}

# --- dispatch ----------------------------------------------------------------------
MODE="${1:-sca}"
case "${MODE}" in
  sca) run_sca ;;
  --fast | fast) run_fast ;;
  integration) run_integration ;;
  infra) run_infra ;;
  --fix | fix) run_fix ;;
  --help | -h | help) usage ;;
  *)
    echo "ERROR: unknown mode '${MODE}'" >&2
    echo >&2
    usage >&2
    exit 2
    ;;
esac
