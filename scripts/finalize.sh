#!/usr/bin/env bash
# finalize.sh — merge the completed staging branch into the user's working branch.
#
# Run from the repo root after every wave is integrated and staging is green.
# Leaves the repo checked out on the working branch.
#
# If the merge conflicts — which happens only if the working branch received
# commits DURING the centella run, so it has diverged from where staging was
# branched — the merge is aborted cleanly: the working branch is restored to
# its pre-finalize state, and the script exits non-zero. The orchestrator
# reports this; centella/staging is intact and the run can be finalized manually.
set -euo pipefail

if [ ! -f .centella/working-branch ]; then
  echo "error: .centella/working-branch missing — run setup-staging.sh first" >&2
  exit 2
fi
WORKING_BRANCH="$(cat .centella/working-branch)"

if ! git show-ref --verify --quiet refs/heads/centella/staging; then
  echo "error: centella/staging does not exist — nothing to finalize" >&2
  exit 2
fi

CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$WORKING_BRANCH" ]; then
  git checkout "$WORKING_BRANCH"
fi

# Attempt the merge. On conflict, abort it so the working branch is left clean
# rather than mid-merge with conflict markers.
if git merge --no-ff -m "centella: integrate completed run into ${WORKING_BRANCH}" centella/staging; then
  echo "finalized: centella/staging merged into ${WORKING_BRANCH}"
  exit 0
else
  git merge --abort 2>/dev/null || true
  echo "error: finalize merge conflicts — ${WORKING_BRANCH} diverged from staging" >&2
  echo "       during the run. The merge was aborted; ${WORKING_BRANCH} is clean" >&2
  echo "       and centella/staging is intact. Resolve by merging centella/staging" >&2
  echo "       into ${WORKING_BRANCH} manually." >&2
  exit 1
fi
