#!/usr/bin/env bash
# cleanup.sh — remove centella worktrees and (optionally) branches.
#
# Run from the repo root.
#
# Modes:
#   cleanup.sh --legacy
#     Remove the pre-per-run layout: .centella/state.json, .centella/working-branch,
#     .centella/{subtasks,criteria,checkpoints,logs}, .centella/worktrees/*, and
#     branches centella/staging plus any non-per-run centella/<sid>.
#     Use this once after upgrading to a per-run version of centella.
#
#   cleanup.sh [--branches]  (legacy default, kept for backward compatibility)
#     Removes every worktree under .centella/worktrees/, prunes stale worktree
#     metadata. With --branches also deletes every refs/heads/centella/* branch.
#     This mode predates per-run namespacing; commit 5 of the parallel-safe
#     refactor will replace it with --run-id / --all-runs scoped variants.
set -euo pipefail

MODE="${1:-}"

if [ "$MODE" = "--legacy" ]; then
  # ----- legacy-layout migration cleanup ----------------------------------
  # Remove every pre-per-run artifact under .centella/. The per-run layout
  # lives under .centella/runs/<run-id>/ and is NOT touched by --legacy.
  rm -f .centella/state.json
  rm -f .centella/working-branch
  rm -f .centella/plan.json
  rm -f .centella/pending-questions.json
  rm -f .centella/pending-clarifications.json
  rm -f .centella/answers.json
  rm -rf .centella/subtasks .centella/criteria .centella/checkpoints .centella/logs

  if [ -d .centella/worktrees ]; then
    for d in .centella/worktrees/*/; do
      [ -d "$d" ] || continue
      git worktree remove --force "$d" 2>/dev/null || true
    done
    rmdir .centella/worktrees 2>/dev/null || true
  fi
  git worktree prune

  # Legacy branches: centella/staging and any centella/<sid> without a /
  # separator after centella/ (per-run branches are centella/<run-id>/<sid>
  # — two segments — which we deliberately leave alone).
  if git show-ref --verify --quiet refs/heads/centella/staging; then
    git branch -D centella/staging 2>/dev/null || true
  fi
  for b in $(git for-each-ref --format='%(refname:short)' refs/heads/centella/); do
    case "$b" in
      centella/*/*) : ;;             # per-run branch — keep
      centella/staging) : ;;          # already deleted above
      centella/*) git branch -D "$b" 2>/dev/null || true ;;
    esac
  done

  echo "legacy layout removed (.centella/ pre-per-run files and centella/staging-style branches)"
  exit 0
fi

# ----- legacy default mode (pre-per-run; kept for backward compatibility) -
# This mode predates per-run namespacing. Commit 5 of the parallel-safe
# refactor will replace it with --run-id / --all-runs scoped variants.
REMOVE_BRANCHES=false
[ "$MODE" = "--branches" ] && REMOVE_BRANCHES=true

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
