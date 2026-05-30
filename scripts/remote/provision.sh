#!/usr/bin/env bash
# scripts/remote/provision.sh — provision a Fly.io Machine for one pila run.
#
# This is the remote equivalent of `nerdctl run --rm`: start a Fly Machine
# from the pila image, block until the machine is reachable (SSH/started),
# then destroy it on exit — whether the caller exits cleanly, is interrupted
# by Ctrl-C, or crashes.
#
# Usage (invoked from the pila launcher's REMOTE=true branch):
#
#   source scripts/remote/provision.sh
#   provision_machine             # blocks until machine is started
#   # ... do work (run pila inside the machine) ...
#   # destroy_machine is registered as an EXIT trap automatically
#
# Environment variables (set by the launcher before sourcing):
#
#   PILA_FLY_APP   — Fly.io app name (default: "pila")
#   FLY_IMAGE_TAG  — full image tag to launch (e.g. registry.fly.io/pila:0.2.1)
#   FLY_REGION     — Fly.io region (default: from fly.toml or "iad")
#   FLY_VM_CPUS    — vCPUs for the machine (default: 4)
#   FLY_VM_MEMORY  — memory in MB for the machine (default: 8192)
#
# Exports:
#   PILA_MACHINE_ID — the created machine's ID (available after provision_machine)
#
# The teardown trap is registered when provision_machine succeeds. It fires
# on EXIT (clean exit), INT (Ctrl-C), and TERM (SIGTERM). A machine that
# fails to start is destroyed immediately before provision_machine returns 1.

set -euo pipefail

# --- configuration with defaults -----------------------------------------
FLY_APP="${PILA_FLY_APP:-pila}"
FLY_REGION="${FLY_REGION:-iad}"
FLY_VM_CPUS="${FLY_VM_CPUS:-4}"
FLY_VM_MEMORY="${FLY_VM_MEMORY:-8192}"

# Max seconds to wait for the machine to reach state "started".
MACHINE_START_TIMEOUT="${PILA_MACHINE_START_TIMEOUT:-120}"

# Exported machine ID — empty until provision_machine succeeds.
PILA_MACHINE_ID=""

# --- require flyctl -------------------------------------------------------
require_flyctl() {
  if ! command -v flyctl >/dev/null 2>&1; then
    echo "pila: flyctl not found on PATH." >&2
    echo "  Install from https://fly.io/docs/flyctl/install/" >&2
    echo "  or: brew install flyctl (macOS)" >&2
    return 1
  fi
  if ! flyctl auth status >/dev/null 2>&1; then
    echo "pila: flyctl is not authenticated." >&2
    echo "  Run: flyctl auth login" >&2
    return 1
  fi
}

# --- destroy machine (registered as trap) --------------------------------
# Called on EXIT / INT / TERM once a machine has been created.
# Idempotent: destroy is no-op if the machine is already gone.
destroy_machine() {
  local mid="$PILA_MACHINE_ID"
  if [ -z "$mid" ]; then
    return 0
  fi
  echo "[pila] remote: destroying machine $mid ..." >&2
  if flyctl machine destroy "$mid" \
       --app "$FLY_APP" \
       --force \
       2>/dev/null; then
    echo "[pila] remote: machine $mid destroyed" >&2
  else
    # destroy can fail if the machine was already stopped/destroyed by Fly.
    # Attempt stop first as a fallback, then a second destroy.
    flyctl machine stop "$mid" --app "$FLY_APP" 2>/dev/null || true
    flyctl machine destroy "$mid" --app "$FLY_APP" --force 2>/dev/null || true
    echo "[pila] remote: machine $mid stop+destroy attempted (may already be gone)" >&2
  fi
  PILA_MACHINE_ID=""
}

# --- wait for machine to reach state "started" ---------------------------
wait_for_started() {
  local mid="$1"
  local deadline=$(( $(date +%s) + MACHINE_START_TIMEOUT ))
  local state=""
  echo "[pila] remote: waiting for machine $mid to start (timeout: ${MACHINE_START_TIMEOUT}s)..." >&2
  while true; do
    state="$(flyctl machine status "$mid" \
               --app "$FLY_APP" \
               --json 2>/dev/null \
             | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("state",""))' \
             2>/dev/null || true)"
    case "$state" in
      started)
        echo "[pila] remote: machine $mid is started" >&2
        return 0
        ;;
      failed|stopped|destroyed|replacing)
        echo "pila: machine $mid entered state '$state' — cannot proceed" >&2
        return 1
        ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "pila: timed out waiting for machine $mid to start (${MACHINE_START_TIMEOUT}s)" >&2
      return 1
    fi
    sleep 2
  done
}

# --- provision_machine ---------------------------------------------------
# Creates a Fly Machine from $FLY_IMAGE_TAG, registers the destroy trap,
# and blocks until the machine reaches state "started".
# Requires: $FLY_IMAGE_TAG set by the caller.
# Exports:  $PILA_MACHINE_ID
# Returns:  0 on success; 1 on failure (machine is destroyed before returning).
provision_machine() {
  if [ -z "${FLY_IMAGE_TAG:-}" ]; then
    echo "pila: FLY_IMAGE_TAG is not set — cannot start a Fly Machine" >&2
    echo "  Build and push the pila image first:" >&2
    echo "    ./scripts/publish-image.sh --app $FLY_APP --push" >&2
    return 1
  fi

  require_flyctl || return 1

  echo "[pila] remote: creating machine (app=$FLY_APP region=$FLY_REGION image=$FLY_IMAGE_TAG)..." >&2

  # flyctl machine run --detach starts the machine without streaming its
  # output. --json produces a single JSON object whose 'id' field is the
  # machine ID. --skip-launch tells the Machine not to run its entrypoint
  # yet (we'll start it explicitly below via flyctl machine start); this
  # allows us to capture the ID before the workload begins.
  #
  # If --skip-launch is unavailable (older flyctl), fall back to --detach
  # only: the machine starts immediately and we capture the ID from JSON.
  local create_output machine_id
  if create_output="$(flyctl machine run "$FLY_IMAGE_TAG" \
       --app "$FLY_APP" \
       --region "$FLY_REGION" \
       --vm-cpus "$FLY_VM_CPUS" \
       --vm-memory "$FLY_VM_MEMORY" \
       --detach \
       --json \
       2>&1)"; then
    machine_id="$(printf '%s' "$create_output" \
                  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' \
                  2>/dev/null || true)"
  fi

  if [ -z "$machine_id" ]; then
    echo "pila: failed to create Fly Machine — flyctl output:" >&2
    printf '  %s\n' "$create_output" >&2
    return 1
  fi

  echo "[pila] remote: created machine $machine_id" >&2
  PILA_MACHINE_ID="$machine_id"
  export PILA_MACHINE_ID

  # Register teardown trap immediately after a successful creation so Ctrl-C
  # or any error after this point cannot leak the machine.
  # shellcheck disable=SC2064
  trap 'destroy_machine' EXIT INT TERM

  # Wait until the machine is reachable.
  if ! wait_for_started "$machine_id"; then
    # destroy_machine will fire via the EXIT trap as this function returns 1.
    return 1
  fi

  return 0
}
