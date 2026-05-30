#!/usr/bin/env bash
# scripts/remote/lib.sh — shared helpers for the remote (Fly.io) lifecycle.
#
# Sourced by provision.sh, resume-machine.sh, and (Phase 4) re-seed.sh.
# Pure functions — no global state, no traps. Callers own their own
# lifecycle decisions; this file only provides reusable building blocks.

# --- update_run_json -----------------------------------------------------
# Atomically merge key/value pairs into a run.json sidecar on the host.
#
# Usage:
#   update_run_json "$USER_REPO/.pila/runs/<run-id>/run.json" \
#                   key1 value1 [key2 value2 ...]
#
# Values are treated as strings and JSON-encoded. The merge is read →
# patch → temp-file write → rename, mirroring the orchestrator's
# State.save() + _write_run_json() atomicity contract (DESIGN §6).
#
# Returns 0 on success. Returns 1 (and writes to stderr) if the sidecar
# directory does not exist or the rewrite fails.
update_run_json() {
  local sidecar="$1"
  shift
  local dir
  dir="$(dirname "$sidecar")"
  if [ ! -d "$dir" ]; then
    echo "update_run_json: $dir does not exist" >&2
    return 1
  fi
  local tmp
  tmp="$(mktemp "$sidecar.XXXXXX")"
  # Python handles the read+merge+write so we don't reimplement JSON
  # escaping in bash. The trailing args are key/value pairs; odd-count
  # is a programming error.
  if ! python3 - "$sidecar" "$tmp" "$@" <<'PY'
import json, os, sys
sidecar, tmp, *rest = sys.argv[1:]
if len(rest) % 2 != 0:
    print(f"update_run_json: expected even number of key/value args, got {len(rest)}", file=sys.stderr)
    sys.exit(1)
data = {}
if os.path.exists(sidecar):
    try:
        with open(sidecar) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
for i in range(0, len(rest), 2):
    k, v = rest[i], rest[i+1]
    # Empty string clears the key (sets to null).
    data[k] = None if v == "" else v
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  then
    rm -f "$tmp"
    echo "update_run_json: python merge failed for $sidecar" >&2
    return 1
  fi
  mv "$tmp" "$sidecar"
}

# --- iso_now -------------------------------------------------------------
# Emit an ISO-8601 UTC timestamp (sub-second precision). Used as
# paused_at / similar event markers.
iso_now() {
  python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))'
}
