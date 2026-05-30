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
# _seed_repo_preflight
#
# Common validation for both seed_repo_clone and seed_repo_dirty. Returns 0
# when all required env vars and binaries are present; 1 with an actionable
# stderr message otherwise.
# ---------------------------------------------------------------------------
_seed_repo_preflight() {
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
}

# ---------------------------------------------------------------------------
# seed_repo_clone
#
# Step 1 of two-channel seeding: full-history partial clone on the remote.
# Always wipes /work and reclones — call this only on a fresh provision,
# never on resume (the existing /work has run state worth preserving).
# ---------------------------------------------------------------------------
seed_repo_clone() {
  _seed_repo_preflight || return 1

  local origin_url
  origin_url="$(git -C "$USER_REPO" remote get-url "$GIT_REMOTE" 2>/dev/null || true)"
  if [ -z "$origin_url" ]; then
    echo "pila: seed_repo: cannot determine origin URL from remote '$GIT_REMOTE'" >&2
    echo "  Add a remote with: git -C \"$USER_REPO\" remote add $GIT_REMOTE <url>" >&2
    return 1
  fi

  echo "[pila] remote: seeding — cloning $origin_url (full history, --filter=blob:none)..." >&2
  machine_exec sh -c "rm -rf /work && mkdir -p /work" >&2
  machine_exec git clone --filter=blob:none "$origin_url" /work >&2

  local current_ref
  current_ref="$(git -C "$USER_REPO" symbolic-ref --short HEAD 2>/dev/null \
                  || git -C "$USER_REPO" rev-parse HEAD)"
  echo "[pila] remote: checking out $current_ref..." >&2
  machine_exec git -C /work checkout "$current_ref" >&2 || true
}

# ---------------------------------------------------------------------------
# seed_repo_dirty
#
# Step 2 of two-channel seeding: tar the host's `git status --porcelain`
# dirty set and stream it into /work on the remote. Reusable on its own
# — Phase 4 re-seed.sh calls it (without seed_repo_clone) when the user
# resumes a paused run after editing files locally.
#
# Defensive excludes (.pila/runs/*/worktrees/* and .git/*) protect against
# a future change that lets the dirty set name worktree paths — currently
# the host can't produce those paths because worktrees live only on the
# machine, but the safety belt prevents silent clobbering.
# ---------------------------------------------------------------------------
seed_repo_dirty() {
  _seed_repo_preflight || return 1

  # Compute the dirty set: modified tracked files + untracked files,
  # excluding git-ignored entries.
  local dirty_files
  dirty_files="$(git -C "$USER_REPO" status --porcelain 2>/dev/null \
                  | awk '
                      # Untracked files (including untracked dirs — trailing /)
                      /^\?\? / {
                        f = substr($0, 4)
                        gsub(/\/$/, "", f)
                        print f
                        next
                      }
                      # Modified/deleted/renamed/copied in worktree (column 2)
                      length($0) >= 2 && substr($0,2,1) != " " {
                        f = substr($0, 4)
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

  # Defensive --exclude flags: structural protection in case a future
  # change lets host-side dirty paths cross the boundary.
  local tar_rc=0
  {
    printf '%s\n' "$dirty_files" \
      | while IFS= read -r f; do printf '%s\0' "$f"; done \
      | tar -C "$USER_REPO" \
            --exclude='.pila/runs/*/worktrees/*' \
            --exclude='.git/*' \
            --null -T - \
            -czf - 2>/dev/null
  } | machine_exec tar -C /work -xzf - >/dev/null 2>&1 || tar_rc=$?

  if [ "$tar_rc" -ne 0 ] && [ "$tar_rc" -ne 141 ]; then
    echo "pila: seed_repo: tar delta transfer failed (exit $tar_rc)" >&2
    return 1
  fi

  echo "[pila] remote: seeding complete" >&2
}

# ---------------------------------------------------------------------------
# seed_repo
#
# Thin wrapper preserving the public contract: clone then dirty-rsync.
# Used on fresh provisions. Resume + mid-run re-rsync (Phase 4) call
# seed_repo_dirty directly.
# ---------------------------------------------------------------------------
seed_repo() {
  seed_repo_clone || return 1
  seed_repo_dirty || return 1
  return 0
}
