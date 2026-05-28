#!/usr/bin/env bash
# new-worktree.sh <subtask-id> <run-id> — create (or reuse) an isolated
# worktree for a subtask.
#
# The worktree branches off the CURRENT pila/runs/<run-id> tip, so a
# subtask sees the integrated results of all prior waves. Idempotent: if
# the worktree or branch already exists (e.g. resuming after a handoff),
# it is reused. Prints the absolute worktree path on stdout.
#
# Branch shape: subtask branches live under `pila/subtasks/<run-id>/<sid>`,
# disjoint from the run-branch namespace `pila/runs/<run-id>`. The two
# sub-namespaces must stay disjoint so neither is an ancestor ref of the
# other in git's loose ref store (see DESIGN.md §3).
set -euo pipefail

ID="${1:?usage: new-worktree.sh <subtask-id> <run-id>}"
RUN_ID="${2:?usage: new-worktree.sh <subtask-id> <run-id>}"
WT=".pila/runs/${RUN_ID}/worktrees/${ID}"
BRANCH="pila/subtasks/${RUN_ID}/${ID}"
PARENT_BRANCH="pila/runs/${RUN_ID}"

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
