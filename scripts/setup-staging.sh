#!/usr/bin/env bash
# setup-staging.sh — initialize the centella staging branch and worktree.
#
# Records the current working branch, creates `centella/staging` off the
# current HEAD, adds a worktree for it, and excludes .centella/ from git.
#
# GENUINELY idempotent: if `centella/staging` already exists (a run is in
# progress, or this is a --resume), the branch is LEFT WHERE IT IS. It is
# never force-reset — doing so would discard every integration commit from
# the waves already completed. The branch is created only when absent.
set -euo pipefail

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 1
fi

mkdir -p .centella/worktrees .centella/subtasks .centella/criteria .centella/checkpoints

# Record the working branch only on first setup. On a resume the file already
# exists and the live HEAD may be anything; the original value must be kept.
if [ ! -f .centella/working-branch ]; then
  git rev-parse --abbrev-ref HEAD > .centella/working-branch
fi
WORKING_BRANCH="$(cat .centella/working-branch)"

# Create the staging branch ONLY if it does not already exist. An existing
# centella/staging carries the integrated work of completed waves — never reset it.
if git show-ref --verify --quiet refs/heads/centella/staging; then
  echo "staging-branch: centella/staging (existing — preserved, not reset)"
else
  git branch centella/staging HEAD
  echo "staging-branch: centella/staging (created at HEAD)"
fi

# Add the staging worktree if it is not already present.
if ! git worktree list --porcelain | grep -q "worktree .*/.centella/worktrees/staging$"; then
  git worktree add .centella/worktrees/staging centella/staging >/dev/null
fi

# Keep centella artifacts out of git without touching the user's tracked .gitignore.
EXCLUDE_FILE="$(git rev-parse --git-dir)/info/exclude"
grep -qxF '.centella/' "$EXCLUDE_FILE" 2>/dev/null || echo '.centella/' >> "$EXCLUDE_FILE"

echo "working-branch: $WORKING_BRANCH"
echo "staging-worktree: $(cd .centella/worktrees/staging && pwd)"
