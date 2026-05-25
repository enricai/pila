#!/usr/bin/env bash
# new-worktree.sh <subtask-id> — create (or reuse) an isolated worktree for a subtask.
#
# The worktree branches off the CURRENT centella/staging tip, so a subtask
# sees the integrated results of all prior waves. Idempotent: if the worktree
# or branch already exists (e.g. resuming after a handoff), it is reused.
# Prints the absolute worktree path on stdout.
set -euo pipefail

ID="${1:?usage: new-worktree.sh <subtask-id>}"
WT=".centella/worktrees/${ID}"
BRANCH="centella/${ID}"

if git worktree list --porcelain | grep -q "worktree .*/${WT}$"; then
  : # already present — reuse it
elif git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  # branch exists but worktree was removed — re-attach
  git worktree add "$WT" "$BRANCH" >/dev/null
else
  # fresh subtask — branch off the current staging tip
  git worktree add "$WT" -b "$BRANCH" centella/staging >/dev/null
fi

(cd "$WT" && pwd)
