"""Source-text pins for the per-run shell scripts.

After commit 3 of the parallel-safe refactor:
- `scripts/setup-run.sh` (renamed from setup-staging.sh) takes a RUN_ID
  argument and scopes all paths under `.pila/runs/$RUN_ID/`.
- `scripts/new-worktree.sh` takes a second RUN_ID argument; subtask
  branches are `pila/subtasks/$RUN_ID/$ID`, branched off
  `pila/runs/$RUN_ID`.
- `scripts/integrate.sh` takes a second RUN_ID argument; merge target is
  `pila/runs/$RUN_ID` and merge source is
  `pila/subtasks/$RUN_ID/$ID`.
- `scripts/finalize.sh` takes a RUN_ID argument; merge source is
  `pila/runs/$RUN_ID`.

The run-branch (`pila/runs/…`) and subtask-branch
(`pila/subtasks/…`) prefixes are deliberately disjoint so neither is
an ancestor ref of the other in git's loose ref store — without this,
`git worktree add` for the first subtask fails with `cannot lock ref …`.
See DESIGN.md §3 and `test_branch_namespaces_dont_collide.py`.

None of these scripts should reference the `pila/staging` branch
name — it does not exist in the per-run layout.
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
    """The script writes everything under .pila/runs/$RUN_ID/."""
    src = _script("setup-run.sh")
    assert '.pila/runs/${RUN_ID}' in src
    # And it doesn't write to top-level paths.
    assert '.pila/worktrees/staging' not in src
    assert '.pila/state.json' not in src


def test_setup_run_branch_is_per_run():
    src = _script("setup-run.sh")
    assert 'BRANCH="pila/runs/${RUN_ID}"' in src
    # The 'pila/staging' branch name must not appear.
    assert 'pila/staging' not in src


# --- new-worktree.sh ------------------------------------------------------

def test_new_worktree_takes_run_id_arg():
    src = _script("new-worktree.sh")
    assert 'RUN_ID="${2:?usage: new-worktree.sh <subtask-id> <run-id>}"' in src


def test_new_worktree_uses_per_run_paths():
    src = _script("new-worktree.sh")
    assert '.pila/runs/${RUN_ID}/worktrees/${ID}' in src


def test_new_worktree_branch_uses_subtasks_namespace():
    """Subtask branches are pila/subtasks/<run-id>/<sid>, branched off
    pila/runs/<run-id>. The two namespaces must be disjoint so neither
    is an ancestor ref of the other in git's loose ref store."""
    src = _script("new-worktree.sh")
    assert 'BRANCH="pila/subtasks/${RUN_ID}/${ID}"' in src
    assert 'PARENT_BRANCH="pila/runs/${RUN_ID}"' in src


def test_new_worktree_branches_off_run_branch():
    """The fresh-subtask path branches off the per-run branch, not staging."""
    src = _script("new-worktree.sh")
    assert '"$PARENT_BRANCH"' in src
    assert 'pila/staging' not in src


# --- integrate.sh ---------------------------------------------------------

def test_integrate_takes_run_id_arg():
    src = _script("integrate.sh")
    assert 'RUN_ID="${2:?usage: integrate.sh <subtask-id> <run-id>}"' in src


def test_integrate_merges_into_per_run_staging():
    src = _script("integrate.sh")
    assert 'STAGING=".pila/runs/${RUN_ID}/worktrees/staging"' in src


def test_integrate_branch_uses_subtasks_namespace():
    src = _script("integrate.sh")
    assert 'BRANCH="pila/subtasks/${RUN_ID}/${ID}"' in src
    assert 'pila/staging' not in src


# --- finalize.sh ----------------------------------------------------------

def test_finalize_takes_run_id_arg():
    src = _script("finalize.sh")
    assert 'RUN_ID="${1:?usage: finalize.sh <run-id>}"' in src


def test_finalize_references_per_run_branch():
    """finalize.sh resolves the per-run branch from ${RUN_ID}."""
    src = _script("finalize.sh")
    assert 'BRANCH="pila/runs/${RUN_ID}"' in src
    # The 'pila/staging' branch must not appear in executable lines.
    non_comment = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'pila/staging' not in non_comment


def test_finalize_uses_per_run_working_branch_file():
    """Working branch is recorded per-run under runs/<id>/working-branch."""
    src = _script("finalize.sh")
    assert '.pila/runs/${RUN_ID}' in src
    # The top-level .pila/working-branch must not appear.
    assert "\".pila/working-branch\"" not in src
    assert "'.pila/working-branch'" not in src
