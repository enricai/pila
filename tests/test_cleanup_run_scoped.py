"""Source-text pins for the run-scoped cleanup.sh modes.

cleanup.sh in commit 5 supports:
- `--run-id <id> [--branches]` — single-run cleanup
- `--all-runs [--branches]` — every per-run dir (excluding _bootstrap-*)
- `--bootstrap` — orphaned _bootstrap-* dirs
- `--legacy` — pre-per-run layout (unchanged from commit 3)
- no flag — most-recently-failed run, with y/N prompt

Plus a small behavioral test: run cleanup.sh against a real tmp_path
repo with a synthetic per-run dir and confirm the dir gets removed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLEANUP_SH = REPO_ROOT / "scripts" / "cleanup.sh"


def _src() -> str:
    return CLEANUP_SH.read_text()


# --- mode declarations ---------------------------------------------------

def test_cleanup_declares_run_id_mode():
    src = _src()
    assert '--run-id)' in src
    assert 'RUN_ID="${2:?--run-id needs an argument}"' in src


def test_cleanup_declares_all_runs_mode():
    src = _src()
    assert '--all-runs)' in src


def test_cleanup_declares_bootstrap_mode():
    src = _src()
    assert '--bootstrap)' in src


def test_cleanup_declares_legacy_mode():
    src = _src()
    assert '--legacy)' in src
    # The legacy mode body itself.
    assert 'rm -f .centella/state.json' in src


def test_cleanup_declares_branches_flag():
    src = _src()
    assert '--branches)' in src


# --- run-scoping safety --------------------------------------------------

def test_cleanup_scopes_worktree_removal_to_run_dir():
    """--run-id only touches .centella/runs/<id>/worktrees/, not the
    legacy .centella/worktrees/ path. The construction is via a local
    `run_dir` variable: `run_dir=".centella/runs/${run_id}"` then
    `"${run_dir}/worktrees"`."""
    src = _src()
    clean_one_run = src.split("clean_one_run() {")[1].split("\n}")[0]
    # The run_dir variable is correctly anchored under runs/.
    assert 'run_dir=".centella/runs/${run_id}"' in clean_one_run
    # The worktrees path is derived from run_dir, not from the legacy
    # top-level .centella/worktrees/.
    assert '${run_dir}/worktrees' in clean_one_run
    # And the legacy path does NOT appear inside clean_one_run.
    assert '.centella/worktrees/' not in clean_one_run


def test_cleanup_branch_delete_scopes_to_run_id():
    """When --branches is passed, only centella/<run-id> and
    centella/<run-id>/* get deleted — NOT every centella/* branch."""
    src = _src()
    # The for-each-ref pattern restricts to the run_id's namespace.
    assert 'refs/heads/centella/${run_id}' in src
    assert 'refs/heads/centella/${run_id}/' in src


def test_cleanup_all_runs_excludes_bootstrap():
    """--all-runs iterates per-run dirs but skips _bootstrap-* — those
    have their own --bootstrap flag."""
    src = _src()
    # Locate the --all-runs body.
    all_runs_body = src.split('ALL_RUNS" = "true"')[1].split('exit 0\nfi')[0]
    assert "_bootstrap-*" in all_runs_body
    assert "continue" in all_runs_body


def test_cleanup_default_mode_uses_most_recently_failed_heuristic():
    """No-flag invocation finds the most-recently-failed run and prompts."""
    src = _src()
    assert "most_recent_failed_run" in src
    # Confirmation prompt — uppercase N as the default ('[y/N]').
    assert "[y/N]" in src


def test_cleanup_unrecognized_arg_exits_nonzero():
    src = _src()
    # The catch-all case in the argument parser.
    assert "cleanup.sh: unrecognized arg:" in src
    assert "exit 2" in src


# --- legacy mode preserved (regression guard) ----------------------------

def test_cleanup_legacy_still_removes_old_state_json():
    """Commit 3 added --legacy; commit 5's rewrite must preserve it."""
    src = _src()
    legacy_body = src.split('LEGACY" = "true"')[1].split("exit 0")[0]
    assert "rm -f .centella/state.json" in legacy_body
    assert "centella/staging" in legacy_body


def test_cleanup_legacy_preserves_per_run_branches():
    """Commit 3's invariant: --legacy deletes ONE-segment centella/<sid>
    branches but leaves TWO-segment centella/<run-id>/<sid> alone."""
    src = _src()
    legacy_body = src.split('LEGACY" = "true"')[1].split("exit 0")[0]
    assert "centella/*/*" in legacy_body


# --- behavioral: single-run cleanup actually removes the dir -------------

def test_cleanup_run_id_removes_worktrees_but_preserves_state(tmp_path):
    """End-to-end: in a fresh git repo, create a per-run dir with a
    state.json + a (fake) worktree, run `cleanup.sh --run-id <id>`,
    confirm worktrees are gone but the state dir + state.json survive
    as an audit trail. Full purge is reserved for the Ctrl-C path
    inside the orchestrator."""
    # Set up a tiny git repo so `git worktree prune` doesn't error.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "file").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    run_id = "feat-test-aaa111"
    run_dir = repo / ".centella" / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text('{"task": "test"}')
    (run_dir / "criteria").mkdir()
    (run_dir / "criteria" / "feat-001.md").write_text("# criteria")

    r = subprocess.run(
        [str(CLEANUP_SH), "--run-id", run_id],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"cleanup.sh failed: {r.stderr}"
    # State dir and state.json must survive as an audit trail.
    assert run_dir.exists(), (
        f"cleanup.sh --run-id must NOT remove the run dir (kept for audit); "
        f"missing: {run_dir}"
    )
    assert (run_dir / "state.json").exists()
    assert (run_dir / "criteria" / "feat-001.md").exists()
    # The worktrees subdirectory should be gone (or empty).
    assert not (run_dir / "worktrees").exists() or not any(
        (run_dir / "worktrees").iterdir()
    ), "cleanup.sh --run-id must clear ${run_dir}/worktrees/*"


def test_cleanup_bootstrap_removes_orphans(tmp_path):
    """End-to-end: `cleanup.sh --bootstrap` removes orphaned bootstrap
    directories."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    boot1 = repo / ".centella" / "runs" / "_bootstrap-aaaaaa"
    boot2 = repo / ".centella" / "runs" / "_bootstrap-bbbbbb"
    real = repo / ".centella" / "runs" / "feat-real-cccccc"
    boot1.mkdir(parents=True)
    boot2.mkdir(parents=True)
    real.mkdir(parents=True)

    r = subprocess.run(
        [str(CLEANUP_SH), "--bootstrap"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    assert not boot1.exists()
    assert not boot2.exists()
    assert real.exists(), "non-bootstrap run dir must NOT be removed by --bootstrap"
