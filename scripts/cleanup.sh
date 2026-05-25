#!/usr/bin/env bash
# cleanup.sh — remove centella worktrees after a run.
#
# Run from the repo root. Removes every worktree under .centella/worktrees/,
# including staging, and prunes stale worktree metadata. Branches (centella/*)
# are deliberately KEPT as an audit trail of the run. To remove them too,
# pass --branches.
set -euo pipefail

REMOVE_BRANCHES=false
[ "${1:-}" = "--branches" ] && REMOVE_BRANCHES=true

if [ -d .centella/worktrees ]; then
  for d in .centella/worktrees/*/; do
    [ -d "$d" ] || continue
    git worktree remove --force "$d" 2>/dev/null || true
  done
fi
git worktree prune

if [ "$REMOVE_BRANCHES" = true ]; then
  for b in $(git for-each-ref --format='%(refname:short)' refs/heads/centella/); do
    git branch -D "$b" 2>/dev/null || true
  done
  echo "worktrees and centella/* branches removed"
else
  echo "worktrees removed; centella/* branches kept (pass --branches to delete them)"
fi
