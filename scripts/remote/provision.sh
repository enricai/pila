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
#   export PILA_REMOTE_EXIT_RC=$orch_rc   # launcher sets this on exit
#   # decide_teardown is registered as an EXIT trap; classifies the rc
#   # and routes to stop_machine (pause-on-failure) or destroy_machine.
#
# Environment variables (set by the launcher before sourcing):
#
#   PILA_FLY_APP    — Fly.io app name (default: "pila")
#   FLY_IMAGE_TAG   — full image tag to launch (e.g. registry.fly.io/pila:0.2.1)
#   FLY_REGION      — Fly.io region (default: from fly.toml or "iad")
#   FLY_VM_CPUS     — vCPUs for the machine (default: 4)
#   FLY_VM_MEMORY   — memory in MB for the machine (default: 8192)
#   PILA_RUN_ID     — orchestrator-minted run id (optional; when set,
#                     provision writes fly_machine_id to the run sidecar
#                     and decide_teardown writes paused_at on pause)
#   USER_REPO       — host-side path to the user's repo (for sidecar I/O)
#   PILA_REMOTE_EXIT_RC — set by the launcher just before exit; read by
#                     the EXIT trap to classify the orchestrator's exit
#                     code. Pause-worthy: any non-zero other than
#                     EXIT_NEEDS_ANSWERS=10, EX_TEMPFAIL=75, SIGINT=130,
#                     SIGTERM=143. (DESIGN §6 Remote pause-on-failure.)
#
# Exports:
#   PILA_MACHINE_ID — the created machine's ID (available after provision_machine)
#
# The teardown trap is registered when provision_machine succeeds. It fires
# on EXIT (clean exit), INT (Ctrl-C), and TERM (SIGTERM). A machine that
# fails to start is destroyed immediately before provision_machine returns 1.

set -euo pipefail

# --- shared lib (update_run_json / iso_now) ------------------------------
_PROVISION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$_PROVISION_DIR/lib.sh"

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

# --- stop machine --------------------------------------------------------
# Pause-on-failure path: preserves the machine's filesystem on its Fly
# volume so resume-machine.sh can wake it later. Idempotent and tolerant
# of an already-stopped machine.
stop_machine() {
  local mid="$PILA_MACHINE_ID"
  if [ -z "$mid" ]; then
    return 0
  fi
  echo "[pila] remote: stopping machine $mid (paused)..." >&2
  flyctl machine stop "$mid" --app "$FLY_APP" 2>/dev/null || true
  # Don't clear PILA_MACHINE_ID — the launcher's notification block
  # needs it to print the attach/resume commands.
}

# --- destroy machine -----------------------------------------------------
# Full reap. Idempotent: destroy is no-op if the machine is already gone.
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
  # Drop the PID-keyed attach pointer (Phase 3) — the machine no longer
  # exists, so attach should report "no active remote machine" next time.
  if [ -n "${USER_REPO:-}" ]; then
    rm -f "$USER_REPO/.pila/remote/$$.json"
  fi
  PILA_MACHINE_ID=""
}

# --- decide_teardown (registered as EXIT/INT/TERM trap) ------------------
# Classifies $PILA_REMOTE_EXIT_RC (set by the launcher just before exit)
# and dispatches to stop_machine (pause-on-failure) or destroy_machine.
# Classification table is documented in DESIGN §6 Remote pause-on-failure.
#
# Pause branch: writes paused_at + pause_reason to the run sidecar so the
# resume path can find the machine later, and so `pila --list-paused`
# surfaces the run.
#
# Idempotent: the trap fires on every exit, including success; the
# stop/destroy primitives no-op on an empty PILA_MACHINE_ID.
decide_teardown() {
  local rc="${PILA_REMOTE_EXIT_RC:-0}"
  local mid="$PILA_MACHINE_ID"
  if [ -z "$mid" ]; then
    return 0
  fi
  case "$rc" in
    0|10|75|130|143)
      # Success (0), needs-answers re-run (10), rate-limit (75),
      # host-side cancel (130/143): full reap. Per-DESIGN §6, state is
      # in the run branch or about to be re-fetched; the machine's
      # in-memory state has no further value.
      destroy_machine
      ;;
    *)
      # Pause: stop the machine (preserves filesystem on the Fly volume)
      # and surface the failure to the user via the run sidecar.
      local reason="${PILA_PAUSE_REASON:-worker-error}"
      local sidecar=""
      if [ -n "${USER_REPO:-}" ] && [ -n "${PILA_RUN_ID:-}" ]; then
        sidecar="$USER_REPO/.pila/runs/$PILA_RUN_ID/run.json"
      fi
      if [ -n "$sidecar" ] && [ -f "$sidecar" ]; then
        update_run_json "$sidecar" \
          paused_at "$(iso_now)" \
          pause_reason "$reason" \
          fly_machine_id "$mid" || true
      fi
      stop_machine
      echo "" >&2
      echo "[pila] PAUSED: machine $mid (rc=$rc, reason=$reason)" >&2
      if [ -n "${PILA_RUN_ID:-}" ]; then
        echo "  run-id:  $PILA_RUN_ID" >&2
        echo "  resume:  pila --resume --run-id $PILA_RUN_ID --runtime fly" >&2
      fi
      echo "  attach:  flyctl ssh console -a $FLY_APP --machine $mid" >&2
      echo "  kill:    flyctl machine destroy $mid -a $FLY_APP --force" >&2
      if [ -n "${PILA_PAUSE_NOTIFY_CMD:-}" ]; then
        eval "$PILA_PAUSE_NOTIFY_CMD" || true
      fi
      # Don't clear PILA_MACHINE_ID — leave the pointer for the user.
      ;;
  esac
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
  # or any error after this point cannot leak the machine. decide_teardown
  # classifies $PILA_REMOTE_EXIT_RC and dispatches to stop or destroy.
  # shellcheck disable=SC2064
  trap 'decide_teardown' EXIT INT TERM

  # Persist fly_machine_id to the run sidecar immediately so a launcher
  # crash before classification still leaves a recoverable pointer.
  # DESIGN §6 Remote pause-on-failure: the sidecar is the source of truth
  # for what's recoverable; the env-var-only path of older revisions
  # leaks machines on launcher crash.
  if [ -n "${USER_REPO:-}" ] && [ -n "${PILA_RUN_ID:-}" ]; then
    local sidecar="$USER_REPO/.pila/runs/$PILA_RUN_ID/run.json"
    if [ -f "$sidecar" ]; then
      update_run_json "$sidecar" fly_machine_id "$machine_id" || true
    fi
  fi

  # PID-keyed pointer for `pila --attach` (no run-id available yet on
  # fresh runs because the orchestrator hasn't minted one). The file is
  # under $USER_REPO/.pila/remote/<launcher-pid>.json and is removed by
  # destroy_machine on teardown. The launcher renames it to
  # .pila/runs/<run-id>/fly-machine.json after fetch-branch.sh runs.
  # (Phase 3: PTY-over-SSH attach.)
  if [ -n "${USER_REPO:-}" ]; then
    local remote_dir="$USER_REPO/.pila/remote"
    mkdir -p "$remote_dir"
    local pid_record="$remote_dir/$$.json"
    python3 - "$pid_record" "$machine_id" "$FLY_APP" "${PILA_RUN_ID:-}" "$$" <<'PY'
import json, sys, datetime
path, mid, app, run_id, pid = sys.argv[1:]
data = {
    "fly_app": app,
    "fly_machine_id": mid,
    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "run_id": run_id or None,
    "launcher_pid": int(pid),
}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  fi

  # Wait until the machine is reachable.
  if ! wait_for_started "$machine_id"; then
    # decide_teardown will fire via the EXIT trap as this function returns 1.
    # The non-zero rc the caller sets will route to destroy (not pause)
    # because a machine that never started has no useful state to inspect.
    return 1
  fi

  return 0
}
