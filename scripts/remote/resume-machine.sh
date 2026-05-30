#!/usr/bin/env bash
# scripts/remote/resume-machine.sh — wake a paused Fly Machine and
# clear the pause sentinels from the run sidecar.
#
# Sourced by the pila launcher's RUNTIME=fly branch when the run sidecar
# has `paused_at` set. Replaces provision_machine for the resume path:
# the machine already exists (stopped on its Fly volume), so we just
# start it and re-arm the teardown trap.
#
# Usage (invoked from the pila launcher):
#
#   source scripts/remote/provision.sh    # for wait_for_started + traps
#   source scripts/remote/resume-machine.sh
#   resume_machine "<machine-id>"
#
# Environment variables (set by the launcher):
#
#   PILA_FLY_APP — Fly.io app name (default: "pila")
#   USER_REPO    — host-side path to the user's repo (for sidecar I/O)
#   PILA_RUN_ID  — the run id being resumed
#
# Exports:
#   PILA_MACHINE_ID — the resumed machine's ID
#
# Returns 0 on success, 1 on failure (machine not found / refuses to start).

set -euo pipefail

# Resolved via the same pattern as provision.sh.
_RESUME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$_RESUME_DIR/lib.sh"

# --- resume_machine ------------------------------------------------------
resume_machine() {
  local mid="${1:-}"
  if [ -z "$mid" ]; then
    echo "resume_machine: machine id required" >&2
    return 1
  fi
  local fly_app="${PILA_FLY_APP:-pila}"
  echo "[pila] remote: resuming machine $mid (app=$fly_app)..." >&2

  if ! flyctl machine start "$mid" --app "$fly_app" >/dev/null 2>&1; then
    # The machine might already be running (idempotency on retry).
    # Check the state directly; only error if it's not start-able.
    local state
    state="$(flyctl machine status "$mid" --app "$fly_app" --json 2>/dev/null \
             | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("state",""))' \
             2>/dev/null || true)"
    case "$state" in
      started|starting)
        : # already coming up; fall through to wait
        ;;
      destroyed|"")
        echo "pila: machine $mid does not exist or has been destroyed" >&2
        echo "  The pause sidecar references a machine that is no longer recoverable." >&2
        echo "  Delete .pila/runs/<run-id>/run.json paused_at fields, or destroy" >&2
        echo "  the run and start fresh: scripts/cleanup.sh --run-id <id> --branches" >&2
        return 1
        ;;
      *)
        echo "pila: machine $mid is in state '$state' — cannot resume" >&2
        return 1
        ;;
    esac
  fi

  PILA_MACHINE_ID="$mid"
  export PILA_MACHINE_ID

  # Re-arm the teardown trap (provision.sh's decide_teardown). Sourcing
  # provision.sh before this script gives us the function; the trap is
  # registered fresh here because the launcher process is fresh on resume.
  if declare -f decide_teardown >/dev/null 2>&1; then
    # shellcheck disable=SC2064
    trap 'decide_teardown' EXIT INT TERM
  fi

  # Block until the machine is reachable.
  if declare -f wait_for_started >/dev/null 2>&1; then
    if ! wait_for_started "$mid"; then
      return 1
    fi
  fi

  # Clear the pause sentinels so the run no longer renders as
  # paused-remote in `pila --list-paused` once the resume succeeds.
  if [ -n "${USER_REPO:-}" ] && [ -n "${PILA_RUN_ID:-}" ]; then
    local sidecar="$USER_REPO/.pila/runs/$PILA_RUN_ID/run.json"
    if [ -f "$sidecar" ]; then
      update_run_json "$sidecar" \
        paused_at "" \
        pause_reason "" || true
    fi
  fi

  echo "[pila] remote: machine $mid resumed" >&2
  return 0
}
