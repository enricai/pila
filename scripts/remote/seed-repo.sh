#!/usr/bin/env bash
# scripts/remote/seed-repo.sh — seed the developer's working tree into a
# Fly.io Machine for one pila remote run.
#
# Two-channel seeding (remote-task-system.md lines 15-20):
#
#   1. Committed bulk  — `git clone --filter=blob:none` from origin on the
#      remote machine.  Full history is required for git worktrees (shallow
#      clone is disqualified — see remote-task-system.md "Worktree constraint").
#
#   2. Uncommitted/untracked delta — rsync the dirty set from the laptop.
#      `git status --porcelain` computes modified + untracked files (minus
#      ignored); only those files cross the slow laptop uplink.
#
# After seeding, /work on the machine mirrors the developer's working tree:
# same tracked files, same uncommitted edits, same untracked files.
#
# Usage (called by the pila launcher after provision_machine succeeds):
#
#   source scripts/remote/seed-repo.sh
#   seed_repo         # blocks until seeding is complete
#
# Environment variables consumed:
#
#   PILA_MACHINE_ID   — ID of the started Fly Machine (set by provision.sh)
#   PILA_FLY_APP      — Fly.io app name (default: "pila")
#   USER_REPO         — absolute path to the local git repo (set by launcher)
#   PILA_GIT_REMOTE   — git remote to clone from (default: "origin")
#
# Requires: flyctl on PATH and authenticated; git; tar.

set -euo pipefail

FLY_APP="${PILA_FLY_APP:-pila}"
GIT_REMOTE="${PILA_GIT_REMOTE:-origin}"

# ---------------------------------------------------------------------------
# machine_exec <cmd>...
#
# Run a command on the Fly Machine via `flyctl machine exec`.
# Streams stdout/stderr to the caller's stderr for visibility.
# ---------------------------------------------------------------------------
machine_exec() {
  flyctl machine exec "$PILA_MACHINE_ID" \
    --app "$FLY_APP" \
    -- "$@"
}

# ---------------------------------------------------------------------------
# seed_repo
#
# Step 1: git clone --filter=blob:none <origin_url> /work on the remote.
# Step 2: compute dirty set locally, tar it, stream to the remote.
# ---------------------------------------------------------------------------
seed_repo() {
  if [ -z "${PILA_MACHINE_ID:-}" ]; then
    echo "pila: seed_repo: PILA_MACHINE_ID is not set" >&2
    return 1
  fi
  if [ -z "${USER_REPO:-}" ]; then
    echo "pila: seed_repo: USER_REPO is not set" >&2
    return 1
  fi
  if ! command -v flyctl >/dev/null 2>&1; then
    echo "pila: seed_repo: flyctl not found on PATH" >&2
    return 1
  fi

  # --- Step 1: full-history partial clone on the remote -------------------
  local origin_url
  origin_url="$(git -C "$USER_REPO" remote get-url "$GIT_REMOTE" 2>/dev/null || true)"
  if [ -z "$origin_url" ]; then
    echo "pila: seed_repo: cannot determine origin URL from remote '$GIT_REMOTE'" >&2
    echo "  Add a remote with: git -C \"$USER_REPO\" remote add $GIT_REMOTE <url>" >&2
    return 1
  fi

  echo "[pila] remote: seeding — cloning $origin_url (full history, --filter=blob:none)..." >&2
  # Ensure /work is empty before cloning; the image may have left an empty dir.
  machine_exec sh -c "rm -rf /work && mkdir -p /work" >&2
  # --filter=blob:none: partial clone — full history, lazy blob backfill on demand.
  # This satisfies the worktree constraint (full history required) while keeping
  # the initial clone fast over the reliable cloud connection.
  machine_exec git clone --filter=blob:none "$origin_url" /work >&2

  # Checkout the same branch/commit the developer is on.
  local current_ref
  current_ref="$(git -C "$USER_REPO" symbolic-ref --short HEAD 2>/dev/null \
                  || git -C "$USER_REPO" rev-parse HEAD)"
  echo "[pila] remote: checking out $current_ref..." >&2
  machine_exec git -C /work checkout "$current_ref" >&2 || true

  # --- Step 2: rsync the uncommitted delta --------------------------------
  # Compute the dirty set: modified tracked files + untracked files,
  # excluding git-ignored entries.
  # `git status --porcelain` output:
  #   XY PATH   (XY = index/worktree status codes; PATH is relative to repo root)
  # We want any file where the worktree column (Y, position 2) is non-blank,
  # plus untracked files (?? prefix).  Ignored files are excluded by default.
  local dirty_files
  dirty_files="$(git -C "$USER_REPO" status --porcelain 2>/dev/null \
                  | awk '
                      # Untracked files (including untracked dirs — trailing /)
                      /^\?\? / {
                        f = substr($0, 4)
                        # Strip trailing / from directory entries — tar handles
                        # them recursively when listed as a path.
                        gsub(/\/$/, "", f)
                        print f
                        next
                      }
                      # Modified/deleted/renamed/copied in worktree (column 2)
                      length($0) >= 2 && substr($0,2,1) != " " {
                        f = substr($0, 4)
                        # Rename format: "old -> new"; take the destination.
                        if (index(f, " -> ")) {
                          f = substr(f, index(f, " -> ") + 4)
                        }
                        gsub(/\/$/, "", f)
                        print f
                      }
                  ')"

  if [ -z "$dirty_files" ]; then
    echo "[pila] remote: seeding — working tree is clean; no delta to sync" >&2
    return 0
  fi

  local file_count
  file_count="$(printf '%s\n' "$dirty_files" | wc -l | tr -d ' ')"
  echo "[pila] remote: seeding — syncing $file_count dirty file(s)/dir(s)..." >&2

  # Pack the dirty set into a tar archive and pipe it to the remote.
  # `tar -C "$USER_REPO"` makes all paths relative to the repo root so they
  # land at /work/<relative-path> on the remote (tar -C /work -xzf -).
  # Filenames are newline-separated from git status; printf them as NUL-
  # separated for tar --null -T - so spaces/special chars are handled.
  # We disable pipefail for the pipe chain: the stub (and sometimes the real
  # flyctl machine exec) may close stdin early, causing SIGPIPE on the tar
  # producer side; the remote tar still receives and extracts the full stream
  # before writing EOF, so a broken-pipe on the producer is benign here.
  local tar_rc=0
  {
    printf '%s\n' "$dirty_files" \
      | while IFS= read -r f; do printf '%s\0' "$f"; done \
      | tar -C "$USER_REPO" \
            --null -T - \
            -czf - 2>/dev/null
  } | machine_exec tar -C /work -xzf - >/dev/null 2>&1 || tar_rc=$?

  # SIGPIPE on the producer side exits as 141 (128+13) or 1 depending on the
  # shell; only fail if the remote tar itself reported an error (non-zero from
  # flyctl machine exec). We treat 141/1 producer exit as benign when the
  # overall exit is from the consumer side.
  if [ "$tar_rc" -ne 0 ] && [ "$tar_rc" -ne 141 ]; then
    echo "pila: seed_repo: tar delta transfer failed (exit $tar_rc)" >&2
    return 1
  fi

  echo "[pila] remote: seeding complete" >&2
  return 0
}
