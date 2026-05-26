#!/usr/bin/env bash
# setup-run.sh <run-id> — initialize the per-run branch and worktree.
#
# Each centella invocation has a unique run_id; this script sets up the
# directory and branch for that run. Records the current working branch,
# creates `centella/<run-id>` off the current HEAD, adds a worktree at
# `.centella/runs/<run-id>/worktrees/staging`, and ensures `.centella/`
# is git-excluded.
#
# GENUINELY idempotent: if `centella/<run-id>` already exists (a run is
# in progress, or this is a --resume), the branch is LEFT WHERE IT IS.
# It is never force-reset — doing so would discard every integration
# commit from the waves already completed.
set -euo pipefail

RUN_ID="${1:?usage: setup-run.sh <run-id>}"
RUN_DIR=".centella/runs/${RUN_ID}"
BRANCH="centella/${RUN_ID}"
STAGING_WT="${RUN_DIR}/worktrees/staging"
WORKING_BRANCH_FILE="${RUN_DIR}/working-branch"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 1
fi

mkdir -p "${RUN_DIR}/worktrees" "${RUN_DIR}/subtasks" "${RUN_DIR}/criteria" "${RUN_DIR}/checkpoints"

# Record the working branch only on first setup. On a resume the file already
# exists and the live HEAD may be anything; the original value must be kept.
if [ ! -f "${WORKING_BRANCH_FILE}" ]; then
  git rev-parse --abbrev-ref HEAD > "${WORKING_BRANCH_FILE}"
fi
WORKING_BRANCH="$(cat "${WORKING_BRANCH_FILE}")"

# Create the run branch ONLY if it does not already exist. An existing
# centella/<run-id> carries the integrated work of completed waves — never reset it.
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "run-branch: ${BRANCH} (existing — preserved, not reset)"
else
  git branch "${BRANCH}" HEAD
  echo "run-branch: ${BRANCH} (created at HEAD)"
fi

# Add the run-branch worktree if it is not already present.
if ! git worktree list --porcelain | grep -q "worktree .*/${STAGING_WT}$"; then
  git worktree add "${STAGING_WT}" "${BRANCH}" >/dev/null
fi

# Keep centella artifacts out of git without touching the user's tracked .gitignore.
EXCLUDE_FILE="$(git rev-parse --git-dir)/info/exclude"
grep -qxF '.centella/' "${EXCLUDE_FILE}" 2>/dev/null || echo '.centella/' >> "${EXCLUDE_FILE}"

echo "working-branch: ${WORKING_BRANCH}"
echo "staging-worktree: $(cd "${STAGING_WT}" && pwd)"
