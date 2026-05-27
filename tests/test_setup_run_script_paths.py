"""Source-text pins for the per-run shell scripts.

After commit 3 of the parallel-safe refactor:
- `scripts/setup-run.sh` (renamed from setup-staging.sh) takes a RUN_ID
  argument and scopes all paths under `.centella/runs/$RUN_ID/`.
- `scripts/new-worktree.sh` takes a second RUN_ID argument; subtask
  branches are `centella/subtasks/$RUN_ID/$ID`, branched off
  `centella/runs/$RUN_ID`.
- `scripts/integrate.sh` takes a second RUN_ID argument; merge target is
  `centella/runs/$RUN_ID` and merge source is
  `centella/subtasks/$RUN_ID/$ID`.
- `scripts/finalize.sh` takes a RUN_ID argument; merge source is
  `centella/runs/$RUN_ID`.

The run-branch (`centella/runs/…`) and subtask-branch
(`centella/subtasks/…`) prefixes are deliberately disjoint so neither is
an ancestor ref of the other in git's loose ref store — without this,
`git worktree add` for the first subtask fails with `cannot lock ref …`.
See DESIGN.md §3 and `test_branch_namespaces_dont_collide.py`.

None of these scripts should reference the legacy `centella/staging`
branch name (which is removed by `cleanup.sh --legacy`).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text()


# --- setup-run.sh ---------------------------------------------------------

def test_setup_run_script_exists():
    """The renamed-from-setup-staging.sh script exists at the new path."""
    assert (SCRIPTS / "setup-run.sh").exists()


def test_setup_staging_script_is_gone():
    """The pre-refactor script name no longer exists — it was renamed."""
    assert not (SCRIPTS / "setup-staging.sh").exists()


def test_setup_run_takes_run_id_arg():
    src = _script("setup-run.sh")
    assert 'RUN_ID="${1:?usage: setup-run.sh <run-id>}"' in src


def test_setup_run_uses_per_run_paths():
    """The script writes everything under .centella/runs/$RUN_ID/."""
    src = _script("setup-run.sh")
    assert '.centella/runs/${RUN_ID}' in src
    # And it doesn't write to the legacy paths.
    assert '.centella/worktrees/staging' not in src
    assert '.centella/state.json' not in src


def test_setup_run_branch_is_per_run():
    src = _script("setup-run.sh")
    assert 'BRANCH="centella/runs/${RUN_ID}"' in src
    # The legacy 'centella/staging' branch name is gone.
    assert 'centella/staging' not in src


# --- new-worktree.sh ------------------------------------------------------

def test_new_worktree_takes_run_id_arg():
    src = _script("new-worktree.sh")
    assert 'RUN_ID="${2:?usage: new-worktree.sh <subtask-id> <run-id>}"' in src


def test_new_worktree_uses_per_run_paths():
    src = _script("new-worktree.sh")
    assert '.centella/runs/${RUN_ID}/worktrees/${ID}' in src


def test_new_worktree_branch_uses_subtasks_namespace():
    """Subtask branches are centella/subtasks/<run-id>/<sid>, branched off
    centella/runs/<run-id>. The two namespaces must be disjoint so neither
    is an ancestor ref of the other in git's loose ref store."""
    src = _script("new-worktree.sh")
    assert 'BRANCH="centella/subtasks/${RUN_ID}/${ID}"' in src
    assert 'PARENT_BRANCH="centella/runs/${RUN_ID}"' in src


def test_new_worktree_branches_off_run_branch():
    """The fresh-subtask path branches off the per-run branch, not staging."""
    src = _script("new-worktree.sh")
    assert '"$PARENT_BRANCH"' in src
    assert 'centella/staging' not in src


# --- integrate.sh ---------------------------------------------------------

def test_integrate_takes_run_id_arg():
    src = _script("integrate.sh")
    assert 'RUN_ID="${2:?usage: integrate.sh <subtask-id> <run-id>}"' in src


def test_integrate_merges_into_per_run_staging():
    src = _script("integrate.sh")
    assert 'STAGING=".centella/runs/${RUN_ID}/worktrees/staging"' in src


def test_integrate_branch_uses_subtasks_namespace():
    src = _script("integrate.sh")
    assert 'BRANCH="centella/subtasks/${RUN_ID}/${ID}"' in src
    assert 'centella/staging' not in src


# --- finalize.sh ----------------------------------------------------------

def test_finalize_takes_run_id_arg():
    src = _script("finalize.sh")
    assert 'RUN_ID="${1:?usage: finalize.sh <run-id>}"' in src


def test_finalize_references_per_run_branch():
    """finalize.sh resolves the per-run branch from ${RUN_ID}, not the
    legacy global `centella/staging` branch."""
    src = _script("finalize.sh")
    assert 'BRANCH="centella/runs/${RUN_ID}"' in src
    # The legacy centella/staging branch is gone from executable lines.
    # (Comments may still mention it for historical context.)
    non_comment = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'centella/staging' not in non_comment


def test_finalize_uses_per_run_working_branch_file():
    """Working branch is recorded per-run under runs/<id>/working-branch."""
    src = _script("finalize.sh")
    assert '.centella/runs/${RUN_ID}' in src
    # The legacy top-level .centella/working-branch is gone.
    assert "\".centella/working-branch\"" not in src
    assert "'.centella/working-branch'" not in src


# --- cleanup.sh --legacy --------------------------------------------------

def test_cleanup_has_legacy_mode():
    src = _script("cleanup.sh")
    assert '--legacy' in src
    # The legacy mode removes the old top-level state.json.
    assert 'rm -f .centella/state.json' in src
