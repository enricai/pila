#!/usr/bin/env bash
# scripts/remote/re-seed.sh — mid-run re-rsync of the host's working tree
# into a paused/running Fly Machine.
#
# Phase 4: realizes "Mid-run correction" from remote-task-system.md line 50
# ("a second rsync of current laptop state into the task, user-triggered").
# Only meaningful when there's a controlled moment to "pick" — i.e., after
# a pause (Phase 2 sets paused_at + fly_machine_id in the sidecar) or when
# the user explicitly invokes `pila --re-seed <run-id>`.
#
# Usage (invoked from the pila launcher):
#
#   source scripts/remote/provision.sh    # for wait_for_started, machine_exec
#   source scripts/remote/seed-repo.sh    # for seed_repo_dirty
#   source scripts/remote/re-seed.sh
#   re_seed                                # reads fly_machine_id from sidecar
#
# Three operations, in order:
#   1. flyctl machine start (if stopped) + wait_for_started.
#   2. Refuse re-seed if /work has tracked-file dirty state on the machine
#      (unless PILA_RE_SEED_FORCE=1) — prevents silent clobbering of
#      in-flight worker edits that haven't yet been committed to a
#      per-subtask branch.
#   3. Run seed_repo_dirty — recompute the host's dirty set, tar it, pipe
#      to the machine. The full-history clone is preserved (no re-clone).
#
# Environment variables (set by the launcher):
#
#   PILA_RUN_ID         — run id whose sidecar holds fly_machine_id
#   USER_REPO           — host-side repo path (for git status + tar source)
#   PILA_FLY_APP        — Fly.io app name (default: "pila")
#   PILA_RE_SEED_FORCE  — set to "1" to bypass the dirty-machine safety check

set -euo pipefail

_RESEED_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$_RESEED_DIR/lib.sh"

# --- re_seed -------------------------------------------------------------
re_seed() {
  if [ -z "${PILA_RUN_ID:-}" ]; then
    echo "pila: re_seed: PILA_RUN_ID is not set" >&2
    return 1
  fi
  if [ -z "${USER_REPO:-}" ]; then
    echo "pila: re_seed: USER_REPO is not set" >&2
    return 1
  fi
  local sidecar="$USER_REPO/.pila/runs/$PILA_RUN_ID/run.json"
  if [ ! -f "$sidecar" ]; then
    echo "pila: re_seed: no run.json at $sidecar" >&2
    return 1
  fi
  local mid
  mid="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get('fly_machine_id') or '')
except Exception:
    pass
" "$sidecar" 2>/dev/null || true)"
  if [ -z "$mid" ]; then
    echo "pila: re_seed: no fly_machine_id recorded in $sidecar" >&2
    return 1
  fi

  PILA_MACHINE_ID="$mid"
  export PILA_MACHINE_ID

  local fly_app="${PILA_FLY_APP:-pila}"
  FLY_APP="$fly_app"
  export FLY_APP

  # --- Step 1: wake the machine if it's stopped ---------------------------
  local state
  state="$(flyctl machine status "$mid" --app "$fly_app" --json 2>/dev/null \
           | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("state",""))' \
           2>/dev/null || true)"
  case "$state" in
    started|starting) : ;;
    stopped)
      echo "[pila] remote: re-seed: starting paused machine $mid..." >&2
      if ! flyctl machine start "$mid" --app "$fly_app" >/dev/null 2>&1; then
        echo "pila: re_seed: flyctl machine start failed for $mid" >&2
        return 1
      fi
      if declare -f wait_for_started >/dev/null 2>&1; then
        wait_for_started "$mid" || return 1
      fi
      ;;
    destroyed|"")
      echo "pila: re_seed: machine $mid is destroyed or missing — cannot re-seed" >&2
      return 1
      ;;
    *)
      echo "pila: re_seed: machine $mid is in state '$state' — cannot re-seed" >&2
      return 1
      ;;
  esac

  # --- Step 2: refuse if machine has dirty tracked files ------------------
  # Skip the safety check when --force is set. The check is one flyctl exec
  # (~1s); it catches the high-cost case where an implementer edited files
  # mid-task that the orchestrator hadn't committed to a per-subtask branch
  # yet. Re-seeding over those files silently produces a wrong PR.
  if [ "${PILA_RE_SEED_FORCE:-0}" != "1" ]; then
    local remote_dirty
    remote_dirty="$(flyctl machine exec "$mid" --app "$fly_app" \
                      -- git -C /work status --porcelain 2>/dev/null || true)"
    # Filter out .pila/ paths (worker state lives there and is expected to change).
    remote_dirty="$(printf '%s\n' "$remote_dirty" \
                      | awk 'length($0) > 0 && substr($0,4) !~ /^\.pila\// { print }')"
    if [ -n "$remote_dirty" ]; then
      echo "pila: re_seed: machine /work has uncommitted tracked changes:" >&2
      printf '%s\n' "$remote_dirty" | head -10 >&2
      echo "" >&2
      echo "  These edits would be clobbered by re-seed." >&2
      echo "  Inspect via: pila --attach $PILA_RUN_ID" >&2
      echo "  Or bypass:   pila --re-seed $PILA_RUN_ID --force" >&2
      return 1
    fi
  fi

  # --- Step 3: rsync the host dirty set -----------------------------------
  if ! declare -f seed_repo_dirty >/dev/null 2>&1; then
    echo "pila: re_seed: seed_repo_dirty not loaded — source seed-repo.sh first" >&2
    return 1
  fi
  seed_repo_dirty
}
