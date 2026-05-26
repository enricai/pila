#!/usr/bin/env bash
# finalize.sh <run-id> — merge the completed run branch into the working branch.
#
# Run from the repo root after every wave is integrated and the run branch
# is green. Leaves the repo checked out on the working branch.
#
# Push and PR are added in a subsequent commit. For now this script does
# the same local merge it always did, just against centella/<run-id>
# instead of the global centella/staging.
#
# If the merge conflicts — which happens only if the working branch received
# commits DURING the centella run, so it has diverged from where the run
# branch was branched — the merge is aborted cleanly: the working branch
# is restored to its pre-finalize state, and the script exits non-zero. The
# orchestrator reports this; centella/<run-id> is intact and the run can be
# finalized manually.
set -euo pipefail

RUN_ID="${1:?usage: finalize.sh <run-id>}"
RUN_DIR=".centella/runs/${RUN_ID}"
BRANCH="centella/${RUN_ID}"
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

CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$WORKING_BRANCH" ]; then
  git checkout "$WORKING_BRANCH"
fi

# Attempt the merge. On conflict, abort it so the working branch is left
# clean rather than mid-merge with conflict markers.
if git merge --no-ff -m "centella: integrate completed run into ${WORKING_BRANCH}" "$BRANCH"; then
  echo "finalized: ${BRANCH} merged into ${WORKING_BRANCH}"
  exit 0
else
  git merge --abort 2>/dev/null || true
  echo "error: finalize merge conflicts — ${WORKING_BRANCH} diverged from" >&2
  echo "       ${BRANCH} during the run. The merge was aborted;" >&2
  echo "       ${WORKING_BRANCH} is clean and ${BRANCH} is intact." >&2
  echo "       Resolve by merging ${BRANCH} into ${WORKING_BRANCH} manually." >&2
  exit 1
fi
