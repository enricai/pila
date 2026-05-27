#!/usr/bin/env bash
# cleanup.sh — remove a centella run's worktrees and (optionally) branches.
#
# Run from the repo root. Default behavior is run-scoped: cleanup never
# touches more than one run at a time unless --all-runs is passed.
#
# Modes:
#   cleanup.sh --run-id <id> [--branches | --subtask-branches]
#     Remove .centella/runs/<id>/worktrees/* and prune git metadata.
#     With --branches also delete centella/runs/<id> and
#     centella/subtasks/<id>/* branches.
#     With --subtask-branches delete only the subtask branches and keep
#     centella/runs/<id> (the post-finalize default — the run branch is
#     the PR head and must outlive the orchestrator).
#
#   cleanup.sh --all-runs [--branches | --subtask-branches]
#     Same as above, applied to every directory under .centella/runs/
#     (excluding _bootstrap-* — use --bootstrap for those).
#
#   cleanup.sh --bootstrap
#     Remove orphaned .centella/runs/_bootstrap-* directories (runs that
#     died before classify completed and so have no stable run_id).
#
#   cleanup.sh --legacy
#     Remove the pre-per-run layout: .centella/state.json,
#     .centella/working-branch, .centella/{subtasks,criteria,checkpoints,
#     logs}, .centella/worktrees/*, and centella/staging plus any
#     non-per-run centella/<sid> branches. Use once after upgrading.
#
#   cleanup.sh  (no flag)
#     Scans .centella/runs/*/state.json for the most recently failed run
#     (most recent without finished_at), prompts y/N, cleans only that run.
set -euo pipefail

# --- helpers -------------------------------------------------------------

clean_one_run() {
  # Args: $1 = run_id
  #       $2 = branch-scope: "0" = keep all branches (audit trail),
  #                          "1" = delete run branch + subtask branches (--branches),
  #                          "2" = delete subtask branches only, keep the run
  #                                branch (--subtask-branches; the post-finalize
  #                                default, since the run branch is the PR head).
  #
  # Removes ${run_dir}/worktrees/* (the worktree directories), prunes git
  # worktree metadata, and optionally deletes branches. The state dir
  # itself (state.json, run.json, criteria/, logs/, checkpoints/) is KEPT
  # as an audit trail. Full nuke-the-run-entirely is the Ctrl-C
  # (full_purge) path inside the orchestrator, not this script.
  local run_id="$1"
  local branch_scope="$2"
  local run_dir=".centella/runs/${run_id}"

  if [ -d "${run_dir}/worktrees" ]; then
    for d in "${run_dir}/worktrees"/*/; do
      [ -d "$d" ] || continue
      git worktree remove --force "$d" 2>/dev/null || true
    done
    # Remove the now-empty worktrees dir if everything went; harmless if
    # something stayed behind (rmdir refuses non-empty).
    rmdir "${run_dir}/worktrees" 2>/dev/null || true
  fi
  git worktree prune

  # Per-run branches live under two disjoint namespaces:
  #   centella/runs/<run-id>           (the run branch itself)
  #   centella/subtasks/<run-id>/<sid> (one branch per subtask)
  # See DESIGN.md §3 for why the namespaces are split.
  case "$branch_scope" in
    1)  # --branches: delete both
      for b in $(git for-each-ref --format='%(refname:short)' \
                 "refs/heads/centella/runs/${run_id}" \
                 "refs/heads/centella/subtasks/${run_id}/"); do
        git branch -D "$b" 2>/dev/null || true
      done
      echo "cleanup: removed worktrees + branches for run ${run_id} "\
"(state dir kept as audit trail at ${run_dir}; rm -rf manually if no longer needed)"
      ;;
    2)  # --subtask-branches: delete subtask branches only
      for b in $(git for-each-ref --format='%(refname:short)' \
                 "refs/heads/centella/subtasks/${run_id}/"); do
        git branch -D "$b" 2>/dev/null || true
      done
      echo "cleanup: removed worktrees + subtask branches for run ${run_id} "\
"(run branch centella/runs/${run_id} and state dir kept; "\
"pass --branches to delete the run branch too)"
      ;;
    *)  # default: keep both
      echo "cleanup: removed worktrees for run ${run_id} "\
"(branches centella/runs/${run_id} + centella/subtasks/${run_id}/* and state dir kept; "\
"pass --subtask-branches or --branches to delete branches too)"
      ;;
  esac
}

most_recent_failed_run() {
  # Find the most-recent run without finished_at. Echo run_id or "".
  local newest=""
  local newest_started=""
  if [ ! -d .centella/runs ]; then
    return
  fi
  for dir in .centella/runs/*/; do
    [ -d "$dir" ] || continue
    local base
    base="$(basename "$dir")"
    case "$base" in _bootstrap-*) continue ;; esac
    local state="${dir}state.json"
    [ -f "$state" ] || continue
    # Parse finished_at and started_at from state.json by grep.
    # JSON parsing in bash is fragile; this is intentionally tolerant.
    if grep -q '"finished_at": *"' "$state"; then
      continue
    fi
    local started
    started="$(grep '"started_at":' "$state" | head -1 \
               | sed 's/.*"started_at": *"\([^"]*\)".*/\1/')"
    if [ -z "$newest" ] || [ "$started" \> "$newest_started" ]; then
      newest="$base"
      newest_started="$started"
    fi
  done
  echo "$newest"
}

# --- argument parsing ----------------------------------------------------

RUN_ID=""
ALL_RUNS=false
BOOTSTRAP=false
LEGACY=false
BRANCHES=false
SUBTASK_BRANCHES=false

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)
      RUN_ID="${2:?--run-id needs an argument}"
      shift 2
      ;;
    --all-runs)
      ALL_RUNS=true
      shift
      ;;
    --bootstrap)
      BOOTSTRAP=true
      shift
      ;;
    --legacy)
      LEGACY=true
      shift
      ;;
    --branches)
      BRANCHES=true
      shift
      ;;
    --subtask-branches)
      SUBTASK_BRANCHES=true
      shift
      ;;
    *)
      echo "cleanup.sh: unrecognized arg: $1" >&2
      echo "usage: cleanup.sh [--run-id <id> | --all-runs | --bootstrap | --legacy] [--branches | --subtask-branches]" >&2
      exit 2
      ;;
  esac
done

if [ "$BRANCHES" = "true" ] && [ "$SUBTASK_BRANCHES" = "true" ]; then
  echo "cleanup.sh: --branches and --subtask-branches are mutually exclusive" >&2
  exit 2
fi

# Branch-scope: 0 = keep all, 1 = --branches, 2 = --subtask-branches
BR_FLAG=0
[ "$BRANCHES" = "true" ] && BR_FLAG=1
[ "$SUBTASK_BRANCHES" = "true" ] && BR_FLAG=2

# --- mode dispatch -------------------------------------------------------

if [ "$LEGACY" = "true" ]; then
  # ----- legacy-layout migration cleanup ----------------------------------
  # Remove every pre-per-run artifact under .centella/. The per-run layout
  # lives under .centella/runs/<run-id>/ and is NOT touched by --legacy.
  rm -f .centella/state.json
  rm -f .centella/working-branch
  rm -f .centella/plan.json
  rm -f .centella/pending-questions.json
  rm -f .centella/pending-clarifications.json
  rm -f .centella/answers.json
  rm -rf .centella/subtasks .centella/criteria .centella/checkpoints .centella/logs

  if [ -d .centella/worktrees ]; then
    for d in .centella/worktrees/*/; do
      [ -d "$d" ] || continue
      git worktree remove --force "$d" 2>/dev/null || true
    done
    rmdir .centella/worktrees 2>/dev/null || true
  fi
  git worktree prune

  # Legacy branches: centella/staging and any one-segment centella/<sid>
  # (a single name segment after centella/, no further /). Current per-run
  # branches are centella/runs/<run-id> and centella/subtasks/<run-id>/<sid>
  # — both have at least one extra / and are deliberately left alone by the
  # centella/*/*) keep guard below.
  if git show-ref --verify --quiet refs/heads/centella/staging; then
    git branch -D centella/staging 2>/dev/null || true
  fi
  for b in $(git for-each-ref --format='%(refname:short)' refs/heads/centella/); do
    case "$b" in
      centella/*/*) : ;;             # per-run branch — keep
      centella/staging) : ;;          # already deleted above
      centella/*) git branch -D "$b" 2>/dev/null || true ;;
    esac
  done

  echo "legacy layout removed (.centella/ pre-per-run files and centella/staging-style branches)"
  exit 0
fi

if [ "$BOOTSTRAP" = "true" ]; then
  # ----- orphaned bootstrap directories -----------------------------------
  if [ ! -d .centella/runs ]; then
    echo "cleanup: no .centella/runs/ to scan"
    exit 0
  fi
  removed=0
  for dir in .centella/runs/_bootstrap-*/; do
    [ -d "$dir" ] || continue
    rm -rf "$dir"
    echo "cleanup: removed orphaned $(basename "$dir")"
    removed=$((removed + 1))
  done
  if [ "$removed" -eq 0 ]; then
    echo "cleanup: no orphaned bootstrap directories"
  fi
  exit 0
fi

if [ "$ALL_RUNS" = "true" ]; then
  # ----- every per-run directory (excluding _bootstrap-*) -----------------
  if [ ! -d .centella/runs ]; then
    echo "cleanup: no .centella/runs/ to clean"
    exit 0
  fi
  cleaned=0
  for dir in .centella/runs/*/; do
    [ -d "$dir" ] || continue
    base="$(basename "$dir")"
    case "$base" in _bootstrap-*) continue ;; esac
    clean_one_run "$base" "$BR_FLAG"
    cleaned=$((cleaned + 1))
  done
  if [ "$cleaned" -eq 0 ]; then
    echo "cleanup: no runs to clean"
  fi
  exit 0
fi

if [ -n "$RUN_ID" ]; then
  # ----- single-run cleanup ----------------------------------------------
  if [ ! -d ".centella/runs/${RUN_ID}" ]; then
    echo "cleanup: no run directory at .centella/runs/${RUN_ID}" >&2
    exit 1
  fi
  clean_one_run "$RUN_ID" "$BR_FLAG"
  exit 0
fi

# ----- default: most recently failed run, with confirmation --------------
target="$(most_recent_failed_run)"
if [ -z "$target" ]; then
  echo "cleanup: no in-progress / failed runs found. " \
       "Use --run-id <id>, --all-runs, --bootstrap, or --legacy."
  exit 0
fi
printf "cleanup: most-recently-failed run is %s — remove? [y/N] " "$target"
read -r answer
case "$answer" in
  [yY]|[yY][eE][sS])
    clean_one_run "$target" "$BR_FLAG"
    ;;
  *)
    echo "cleanup: aborted"
    exit 0
    ;;
esac
