#!/usr/bin/env bash
# new-worktree.sh <subtask-id> <run-id> — create (or reuse) an isolated
# worktree for a subtask.
#
# The worktree branches off the CURRENT centella/<run-id> tip, so a
# subtask sees the integrated results of all prior waves. Idempotent: if
# the worktree or branch already exists (e.g. resuming after a handoff),
# it is reused. Prints the absolute worktree path on stdout.
set -euo pipefail

ID="${1:?usage: new-worktree.sh <subtask-id> <run-id>}"
RUN_ID="${2:?usage: new-worktree.sh <subtask-id> <run-id>}"
WT=".centella/runs/${RUN_ID}/worktrees/${ID}"
BRANCH="centella/${RUN_ID}/${ID}"
PARENT_BRANCH="centella/${RUN_ID}"

if git worktree list --porcelain | grep -q "worktree .*/${WT}$"; then
  : # already present — reuse it
elif git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  # branch exists but worktree was removed — re-attach
  git worktree add "$WT" "$BRANCH" >/dev/null
else
  # fresh subtask — branch off the current run-branch tip
  git worktree add "$WT" -b "$BRANCH" "$PARENT_BRANCH" >/dev/null
fi

(cd "$WT" && pwd)
