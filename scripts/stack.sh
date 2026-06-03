#!/usr/bin/env bash
#
# Panoptes local-stack lifecycle helper — a thin, idempotent convenience wrapper
# over `docker compose` for the v0.1 local proof (VictoriaMetrics + Grafana +
# collector + MCP stdio server). Brings the stack up with the baked Dockerfile
# image (Workstream 3), polls readiness, and gives one-shot status / logs / smoke
# / query subcommands so the operator never has to remember the raw compose flags.
#
# NOTE: never use a `status=` variable — `status` is a reserved word under zsh and
# silently misbehaves. We use `run_status` / `phase` instead. The `status`
# SUBCOMMAND name is a CLI argument, not a shell variable, so it is safe.

set -euo pipefail

# --- locate repo root --------------------------------------------------------------
# Resolve from this script's location so the helper works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Read the handful of values this script needs from .env (if present) so the
# readiness URLs + port checks + smoke config resolve. `docker compose` reads .env
# itself; we deliberately do NOT `source` it — the example ships placeholder values
# like `AWS_REGION=<your-aws-region>` whose shell metacharacters (`<`, `>`) would be
# a syntax error under `.`/`source`. Instead, grep out just the simple `KEY=value`
# lines we care about (no command substitution, no eval) and assign them safely.
read_env_value() {
  local key="$1"
  [[ -f .env ]] || return 0
  # Last matching, non-comment `KEY=...` wins; strip the `KEY=` prefix and any
  # surrounding quotes. No eval — the raw value is taken verbatim.
  local line
  line="$(grep -E "^[[:space:]]*${key}=" .env | grep -v '^[[:space:]]*#' | tail -n1 || true)"
  [[ -n "${line}" ]] || return 0
  local value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "${value}"
}

ENV_VM_HOST_PORT="$(read_env_value VM_HOST_PORT)"
ENV_GRAFANA_HOST_PORT="$(read_env_value GRAFANA_HOST_PORT)"
ENV_CONSUMER_PACK_DIR="$(read_env_value CONSUMER_PACK_DIR)"
ENV_PANOPTES_CONFIG="$(read_env_value PANOPTES_CONFIG)"

# Readiness/port values: prefer an explicit process env, then the .env value, then
# the same defaults the compose file uses.
VM_PORT="${VM_HOST_PORT:-${ENV_VM_HOST_PORT:-8428}}"
GRAFANA_PORT="${GRAFANA_HOST_PORT:-${ENV_GRAFANA_HOST_PORT:-3000}}"

# The compose file hard-requires CONSUMER_PACK_DIR (`${CONSUMER_PACK_DIR:?...}`), so
# EVERY `docker compose` invocation — including read-only `ps`/`logs` in `status`
# before a first `up` — fails interpolation if it is unset. Default it to the .env
# value, else the in-repo demo pack (the same value .env.example ships), so read-only
# subcommands work gracefully on a never-started stack. Compose itself re-reads .env;
# this export only ensures interpolation never fails when we shell out to compose.
export CONSUMER_PACK_DIR="${CONSUMER_PACK_DIR:-${ENV_CONSUMER_PACK_DIR:-./examples/demo-pack}}"

# Config path used by `smoke` when no override is passed (compose also sets it inside
# the container; this is the host-side fallback for the run command).
PANOPTES_CONFIG="${PANOPTES_CONFIG:-${ENV_PANOPTES_CONFIG:-}}"

# All four service names the stack runs; `status` checks every one.
ALL_SERVICES=(victoriametrics grafana collector mcp)

# --- helpers -----------------------------------------------------------------------

die() {
  echo "ERROR: $*" >&2
  exit 1
}

note() {
  echo "==> $*"
}

# Ensure the Docker daemon is reachable before any compose call.
require_docker_daemon() {
  if ! docker info >/dev/null 2>&1; then
    die "Docker daemon is not running (or not reachable). Start Docker and retry."
  fi
}

# Is a host TCP port already bound? Used so `up` can skip the free-port check for a
# service it is about to (idempotently) leave running.
port_in_use() {
  local port="$1"
  # `lsof -iTCP -sTCP:LISTEN` is the most portable listener probe on macOS + Linux.
  lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
}

# Is a given compose service currently running?
service_running() {
  local service="$1"
  docker compose ps --status running --services 2>/dev/null | grep -qx "${service}"
}

# Poll an HTTP endpoint until it returns 2xx/3xx, up to ~120s. Returns non-zero on
# timeout so the caller can surface a clear failure.
wait_for_http() {
  local label="$1" url="$2"
  local deadline=$(( $(date +%s) + 120 ))
  note "Waiting for ${label} at ${url} ..."
  while (( $(date +%s) < deadline )); do
    if curl -fsS -o /dev/null --max-time 3 "${url}" 2>/dev/null; then
      note "${label} is ready."
      return 0
    fi
    sleep 2
  done
  echo "WARN: ${label} did not become ready within 120s (${url})." >&2
  return 1
}

# Curl a health endpoint for `status` — non-fatal, prints UP/DOWN.
probe_http() {
  local label="$1" url="$2"
  if curl -fsS -o /dev/null --max-time 3 "${url}" 2>/dev/null; then
    echo "  ${label}: UP   (${url})"
  else
    echo "  ${label}: DOWN (${url})"
  fi
}

# --- subcommands -------------------------------------------------------------------

cmd_up() {
  require_docker_daemon

  # First-run convenience: seed .env from the example. The demo pack works without
  # any real creds, so this is a notice, NOT an abort.
  if [[ ! -f .env ]]; then
    note "No .env found — creating one from .env.example."
    cp .env.example .env
    note "Fill in real read-only AWS/Sentry creds in .env when you point at a live"
    note "env; the in-repo demo pack works without them."
  fi

  # Preflight: ports must be free UNLESS the owning service is already up (idempotent
  # re-run). VM publishes VM_PORT, Grafana publishes GRAFANA_PORT.
  if ! service_running victoriametrics && port_in_use "${VM_PORT}"; then
    die "Port ${VM_PORT} (VictoriaMetrics) is already in use by another process."
  fi
  if ! service_running grafana && port_in_use "${GRAFANA_PORT}"; then
    die "Port ${GRAFANA_PORT} (Grafana) is already in use by another process."
  fi

  # --build because the Python services now use the repo Dockerfile (Workstream 3).
  note "Bringing the stack up (docker compose up -d --build) ..."
  docker compose up -d --build

  # Bounded readiness poll for the two HTTP faces.
  wait_for_http "VictoriaMetrics" "http://localhost:${VM_PORT}/health" || true
  wait_for_http "Grafana" "http://localhost:${GRAFANA_PORT}/api/health" || true

  echo
  note "Stack is up:"
  echo "  Grafana:         http://localhost:${GRAFANA_PORT}"
  echo "  VictoriaMetrics: http://localhost:${VM_PORT}"
  echo
  docker compose ps
}

cmd_down() {
  require_docker_daemon
  local drop_volumes=false
  if [[ "${1:-}" == "-v" || "${1:-}" == "--volumes" ]]; then
    drop_volumes=true
  elif [[ -n "${1:-}" ]]; then
    die "Unknown 'down' option '${1}' (use -v / --volumes to also drop vm-data)."
  fi

  if [[ "${drop_volumes}" == true ]]; then
    # Dropping vm-data destroys all stored metrics — warn loudly, never the default.
    echo "WARNING: -v will DELETE the vm-data volume (all stored metrics)." >&2
    echo "         Press Ctrl-C within 5s to abort ..." >&2
    sleep 5
    note "Tearing down (docker compose down -v) — vm-data volume will be removed."
    docker compose down -v
  else
    note "Tearing down (docker compose down) — vm-data volume is preserved."
    docker compose down
  fi
}

cmd_restart() {
  require_docker_daemon
  if [[ "${1:-}" == "--hard" ]]; then
    note "Hard restart: full down + up."
    cmd_down
    cmd_up
  elif [[ -n "${1:-}" ]]; then
    die "Unknown 'restart' option '${1}' (use --hard for a full down+up)."
  else
    note "Soft restart (docker compose restart) ..."
    docker compose restart
    docker compose ps
  fi
}

cmd_status() {
  require_docker_daemon
  note "Compose services:"
  docker compose ps

  echo
  note "Health probes (non-fatal):"
  probe_http "VictoriaMetrics" "http://localhost:${VM_PORT}/health"
  probe_http "Grafana" "http://localhost:${GRAFANA_PORT}/api/health"

  echo
  note "Recent collector logs:"
  # `|| true` so a not-yet-started collector doesn't abort the status report.
  docker compose logs --tail=5 collector 2>/dev/null || true

  # Exit 0 iff all four services are running; non-zero otherwise (a clean signal for
  # scripts/CI without being noisy).
  local missing=()
  local service
  for service in "${ALL_SERVICES[@]}"; do
    if ! service_running "${service}"; then
      missing+=("${service}")
    fi
  done
  echo
  if [[ "${#missing[@]}" -eq 0 ]]; then
    note "All ${#ALL_SERVICES[@]} services running."
    return 0
  fi
  note "Not running: ${missing[*]}"
  return 1
}

cmd_logs() {
  require_docker_daemon
  # Optional single SERVICE arg; no arg = all services.
  docker compose logs -f "$@"
}

cmd_smoke() {
  require_docker_daemon
  # One collection cycle via the W3 clean entrypoint. --config defaults to the
  # PANOPTES_CONFIG env (set in compose / .env) or an explicit override passed
  # through as extra args.
  note "Running a single collector cycle (--once) ..."
  if [[ -n "$*" ]]; then
    docker compose run --rm collector python -m core.collector --once "$@"
  elif [[ -n "${PANOPTES_CONFIG:-}" ]]; then
    docker compose run --rm collector python -m core.collector --once --config "${PANOPTES_CONFIG}"
  else
    # The compose service already sets PANOPTES_CONFIG in its environment, so the
    # in-container default applies; pass the mounted demo config explicitly as a
    # safe fallback path.
    docker compose run --rm collector python -m core.collector --once \
      --config /packs/consumer/panoptes.yaml
  fi
}

cmd_query() {
  require_docker_daemon
  # Default to the canonical health-up series if no PromQL is given.
  local promql="${1:-panoptes_health_up}"
  local url="http://localhost:${VM_PORT}/api/v1/query"
  note "Querying VictoriaMetrics: ${promql}"
  # --data-urlencode handles spaces/operators in the PromQL expression safely.
  curl -fsS --max-time 5 -G "${url}" --data-urlencode "query=${promql}" \
    | python3 -m json.tool
}

usage() {
  cat <<'USAGE'
Panoptes local-stack lifecycle helper (thin wrapper over `docker compose`).

Usage: bash scripts/stack.sh <command> [args]

Commands:
  up                 Build + start the full stack detached, then poll readiness.
                     Creates .env from .env.example on first run (demo pack works
                     without real creds). Idempotent: skips the free-port check for
                     a service already running.
  down [-v|--volumes]
                     Stop + remove containers. With -v / --volumes ALSO drops the
                     vm-data volume (5s warning first). Volumes are kept by default.
  restart [--hard]   Soft `docker compose restart`, or --hard for a full down + up.
  status             `docker compose ps` + the two health probes + last 5 collector
                     log lines. Exits 0 iff all four services are running.
  logs [SERVICE]     Follow logs (all services, or just SERVICE).
  smoke [--config P] Run ONE collector cycle (`--once`) via the baked image.
  query [PROMQL]     Query VictoriaMetrics (default: panoptes_health_up), pretty-
                     printed JSON.
  --help, -h         Show this help.

Reads VM_HOST_PORT / GRAFANA_HOST_PORT / PANOPTES_CONFIG from .env for the
readiness URLs, port checks, and smoke config.
USAGE
}

# --- dispatch ----------------------------------------------------------------------
COMMAND="${1:-}"
if [[ -n "${COMMAND}" ]]; then
  shift
fi

case "${COMMAND}" in
  up) cmd_up "$@" ;;
  down) cmd_down "$@" ;;
  restart) cmd_restart "$@" ;;
  status) cmd_status "$@" ;;
  logs) cmd_logs "$@" ;;
  smoke) cmd_smoke "$@" ;;
  query) cmd_query "$@" ;;
  --help | -h | help | "") usage ;;
  *)
    echo "ERROR: unknown command '${COMMAND}'" >&2
    echo >&2
    usage >&2
    exit 2
    ;;
esac
