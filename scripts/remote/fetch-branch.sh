#!/usr/bin/env bash
# scripts/remote/fetch-branch.sh — stream a completed pila run branch and
# its state back from a Fly Machine to the host repository.
#
# After the orchestrator runs inside a Fly Machine, the run branch
# (pila/runs/<run-id>) and the .pila/runs/<run-id>/ state directory live on
# the machine's filesystem — not on the host.  This script streams both back
# so the existing host-side finalize block (git push + gh pr create) can run
# unchanged with the host's own auth.
#
# Two-channel fetch (mirror of seed-repo.sh's two-channel seed):
#
#   1. Run branch — git bundle piped from machine to host, then fetched into
#      the host's local repo.  A bundle is the correct mechanism because the
#      host's repo shares the same ancestry (origin remote), so the bundle can
#      be fetched by ref name alone — no remote URL configuration on the machine
#      is required.
#
#   2. Run state — .pila/runs/<run-id>/ tarred on the machine and piped to the
#      host.  The host-side finalize reads run.json and state.json from this
#      directory; they are not in git.
#
# Usage (called by the pila launcher after remote orchestration exits 0):
#
#   source scripts/remote/fetch-branch.sh
#   fetch_branch        # blocks until fetch is complete
#
# Environment variables consumed:
#
#   PILA_MACHINE_ID  — ID of the Fly Machine (set by provision.sh)
#   PILA_FLY_APP     — Fly.io app name (default: "pila")
#   USER_REPO        — absolute path to the local git repo (set by launcher)
#
# Exports (set by fetch_branch on success):
#   PILA_REMOTE_RUN_ID  — the run-id of the completed run on the machine

set -euo pipefail

FLY_APP="${PILA_FLY_APP:-pila}"

# ---------------------------------------------------------------------------
# machine_exec <cmd>...
#
# Run a command on the Fly Machine via flyctl machine exec.
# ---------------------------------------------------------------------------
_fetch_machine_exec() {
  flyctl machine exec "$PILA_MACHINE_ID" \
    --app "$FLY_APP" \
    -- "$@"
}

# ---------------------------------------------------------------------------
# fetch_branch
#
# Find the completed run on the machine, stream its git branch and state
# directory back to the host repo.
# ---------------------------------------------------------------------------
fetch_branch() {
  local machine_id="${PILA_MACHINE_ID:-}"
  if [ -z "$machine_id" ]; then
    echo "pila: fetch_branch: PILA_MACHINE_ID is not set" >&2
    return 1
  fi
  if [ -z "${USER_REPO:-}" ]; then
    echo "pila: fetch_branch: USER_REPO is not set" >&2
    return 1
  fi
  if ! command -v flyctl >/dev/null 2>&1; then
    echo "pila: fetch_branch: flyctl not found on PATH" >&2
    return 1
  fi

  echo "[pila] remote: fetching completed run from machine $machine_id ..." >&2

  # --- Step 1: discover the completed run-id on the machine ----------------
  # The orchestrator writes .pila/runs/<run-id>/run.json with finished_at
  # when phase_finalize completes.  Pick the run whose run.json has
  # finished_at set and pushed_at absent — same logic the host-side finalize
  # block uses.  Use python3 (always available in the pila image) to parse
  # the JSON safely.
  local run_id run_branch working_branch
  local discover_output
  discover_output="$(_fetch_machine_exec python3 -c '
import os, json, sys

runs_dir = "/work/.pila/runs"
if not os.path.isdir(runs_dir):
    sys.exit(1)

best = None
best_mtime = 0
for name in os.listdir(runs_dir):
    rj = os.path.join(runs_dir, name, "run.json")
    if not os.path.isfile(rj):
        continue
    try:
        d = json.load(open(rj))
    except Exception:
        continue
    if not d.get("finished_at"):
        continue
    if d.get("pushed_at"):
        continue
    mtime = os.stat(rj).st_mtime
    if mtime > best_mtime:
        best_mtime = mtime
        best = (name, d.get("branch", ""), d.get("working_branch", ""))

if best is None:
    print("ERROR: no completed unpushed run found on machine")
    sys.exit(1)

print(best[0])
print(best[1])
print(best[2])
' 2>&1)" || {
    echo "pila: fetch_branch: failed to discover completed run on machine $machine_id" >&2
    echo "  Output: $discover_output" >&2
    return 1
  }

  # Check for ERROR prefix from the Python script
  if printf '%s' "$discover_output" | grep -q "^ERROR:"; then
    echo "pila: fetch_branch: $discover_output" >&2
    return 1
  fi

  run_id="$(printf '%s' "$discover_output" | sed -n '1p')"
  run_branch="$(printf '%s' "$discover_output" | sed -n '2p')"
  working_branch="$(printf '%s' "$discover_output" | sed -n '3p')"

  if [ -z "$run_id" ] || [ -z "$run_branch" ]; then
    echo "pila: fetch_branch: could not parse run-id or branch from machine output" >&2
    echo "  Output was: $discover_output" >&2
    return 1
  fi

  echo "[pila] remote: discovered run $run_id (branch: $run_branch)" >&2
  export PILA_REMOTE_RUN_ID="$run_id"

  # --- Step 2: stream the run branch via git bundle -------------------------
  # Create a bundle on the machine containing the run branch and all its
  # ancestry, then pipe it to the host and fetch from it.  The host already
  # has all history from origin (seeded via clone), so the bundle resolves
  # cleanly against the local repo objects.
  #
  # Bundle path is a tmpfile on the machine; we stream via stdout and consume
  # on the host side via `git fetch` reading from a temp bundle file.
  local host_bundle
  host_bundle="$(mktemp "${TMPDIR:-/tmp}/pila-bundle-XXXXXX.bundle")"
  # shellcheck disable=SC2064
  trap "rm -f '$host_bundle'" RETURN

  echo "[pila] remote: streaming git bundle for $run_branch ..." >&2
  # git bundle create writes the bundle to stdout when given "-" as the file.
  # flyctl machine exec streams that stdout back to the host.
  if ! _fetch_machine_exec \
       git -C /work bundle create - "$run_branch" \
       > "$host_bundle" 2>/dev/null; then
    echo "pila: fetch_branch: failed to create git bundle on machine $machine_id" >&2
    rm -f "$host_bundle"
    return 1
  fi

  if [ ! -s "$host_bundle" ]; then
    echo "pila: fetch_branch: git bundle is empty — run branch may not exist on machine" >&2
    rm -f "$host_bundle"
    return 1
  fi

  # Verify the bundle is valid before attempting fetch.
  if ! git -C "$USER_REPO" bundle verify "$host_bundle" >/dev/null 2>&1; then
    echo "pila: fetch_branch: bundle verification failed — possible transfer corruption" >&2
    rm -f "$host_bundle"
    return 1
  fi

  # Fetch the run branch into the host repo from the bundle.
  # `git fetch <bundle> <refspec>` creates the local branch.
  if ! git -C "$USER_REPO" fetch "$host_bundle" \
         "+$run_branch:$run_branch" 2>/dev/null; then
    echo "pila: fetch_branch: git fetch from bundle failed" >&2
    rm -f "$host_bundle"
    return 1
  fi
  rm -f "$host_bundle"
  echo "[pila] remote: run branch $run_branch fetched to host" >&2

  # --- Step 3: stream the .pila run state directory back -------------------
  # The host-side finalize reads .pila/runs/<run-id>/run.json (for finished_at,
  # branch, working_branch) and state.json (for PR body composition).
  # Tar the whole run directory from the machine and extract it under $USER_REPO.
  local run_state_dir="/work/.pila/runs/$run_id"
  local host_pila_runs="$USER_REPO/.pila/runs"
  mkdir -p "$host_pila_runs"

  echo "[pila] remote: streaming .pila/runs/$run_id state directory ..." >&2
  if ! _fetch_machine_exec \
       tar -cC /work/.pila/runs "$run_id" \
       | tar -xC "$host_pila_runs" 2>/dev/null; then
    echo "pila: fetch_branch: failed to stream run state directory from machine $machine_id" >&2
    return 1
  fi

  echo "[pila] remote: run state directory fetched to $host_pila_runs/$run_id" >&2
  echo "[pila] remote: fetch complete — run $run_id ready for host-side finalize" >&2
  return 0
}
