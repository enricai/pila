#!/usr/bin/env bash
# integrate.sh <subtask-id> <run-id> — merge a completed subtask branch
# into the run branch.
#
# Run from the repo root. Merges pila/subtasks/<run-id>/<id> into
# pila/runs/<run-id> inside the run-branch worktree at
# .pila/runs/<run-id>/worktrees/staging. Exit 0 on a clean merge;
# non-zero if the merge conflicts, leaving the worktree mid-merge for
# the pila-integrator to resolve.
set -euo pipefail

ID="${1:?usage: integrate.sh <subtask-id> <run-id>}"
RUN_ID="${2:?usage: integrate.sh <subtask-id> <run-id>}"
STAGING=".pila/runs/${RUN_ID}/worktrees/staging"
BRANCH="pila/subtasks/${RUN_ID}/${ID}"

if [ ! -d "$STAGING" ]; then
  echo "error: run-branch worktree missing — run setup-run.sh ${RUN_ID} first" >&2
  exit 2
fi
if ! git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "error: branch ${BRANCH} does not exist" >&2
  exit 2
fi

cd "$STAGING"

if git merge --no-ff -m "pila: integrate ${ID}" "$BRANCH"; then
  echo "integrated: ${ID}"
  exit 0
else
  echo "conflict: ${ID} — run-branch worktree left mid-merge for the integrator" >&2
  exit 1
fi
