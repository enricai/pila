#!/usr/bin/env bash
# integrate.sh <subtask-id> — merge a completed subtask branch into staging.
#
# Run from the repo root. Merges centella/<id> into centella/staging inside the
# staging worktree. Exit 0 on a clean merge; non-zero if the merge conflicts,
# leaving the staging worktree mid-merge for the centella-integrator to resolve.
set -euo pipefail

ID="${1:?usage: integrate.sh <subtask-id>}"
STAGING=".centella/worktrees/staging"
BRANCH="centella/${ID}"

if [ ! -d "$STAGING" ]; then
  echo "error: staging worktree missing — run setup-staging.sh first" >&2
  exit 2
fi
if ! git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "error: branch ${BRANCH} does not exist" >&2
  exit 2
fi

cd "$STAGING"

if git merge --no-ff -m "centella: integrate ${ID}" "$BRANCH"; then
  echo "integrated: ${ID}"
  exit 0
else
  echo "conflict: ${ID} — staging worktree left mid-merge for the integrator" >&2
  exit 1
fi
