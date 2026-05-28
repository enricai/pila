#!/usr/bin/env bash
# finalize.sh <run-id> — verify the run branch is ready to push.
#
# Run from the repo root after every wave is integrated and the run branch
# is green. The working branch is NOT modified locally — pila does not
# merge into it. The PR opened by `push_and_open_pr()` (in pila.py) is
# the proposed integration into the working branch.
#
# This script's job is narrow: confirm `pila/runs/<run-id>` exists and
# has at least one commit beyond the working branch. If it doesn't, the
# orchestrator dies before attempting the push.
set -euo pipefail

RUN_ID="${1:?usage: finalize.sh <run-id>}"
RUN_DIR=".pila/runs/${RUN_ID}"
BRANCH="pila/runs/${RUN_ID}"
WORKING_BRANCH_FILE="${RUN_DIR}/working-branch"

if [ ! -f "${WORKING_BRANCH_FILE}" ]; then
  echo "error: ${WORKING_BRANCH_FILE} missing — run setup-run.sh ${RUN_ID} first" >&2
  exit 2
fi
WORKING_BRANCH="$(cat "${WORKING_BRANCH_FILE}")"

if ! git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "error: ${BRANCH} does not exist — nothing to finalize" >&2
  exit 2
fi

# The PR base must exist locally for the rev-list comparison below; if the
# user renamed or deleted it after the run started, surface that distinctly
# rather than letting it look like an "already caught up" state.
if ! git show-ref --verify --quiet "refs/heads/${WORKING_BRANCH}"; then
  echo "error: working branch ${WORKING_BRANCH} (recorded at run start) no longer exists — " >&2
  echo "       recreate it (e.g. \`git branch ${WORKING_BRANCH} <commit>\`) before retrying" >&2
  exit 2
fi

# Confirm the run branch has work the working branch doesn't already have.
# `git rev-list --count <working>..<run>` counts commits reachable from the
# run branch but not from the working branch. Zero means the run produced
# no work — likely a misconfiguration the user should see.
AHEAD="$(git rev-list --count "${WORKING_BRANCH}..${BRANCH}")"
if [ "${AHEAD}" = "0" ]; then
  echo "error: ${BRANCH} has no commits beyond ${WORKING_BRANCH} — nothing to push" >&2
  exit 1
fi

echo "finalized: ${BRANCH} ready to push (${AHEAD} commit(s) ahead of ${WORKING_BRANCH})"
exit 0
