"""Source-text pins for the run-scoped cleanup.sh modes.

cleanup.sh supports:
- `--run-id <id> [--branches | --subtask-branches]` — single-run cleanup
- `--all-runs [--branches | --subtask-branches]` — every per-run dir
  (excluding _bootstrap-*)
- `--bootstrap` — orphaned _bootstrap-* dirs
- no flag — most-recently-failed run, with y/N prompt

`--subtask-branches` is the post-finalize default (invoked by
phase_finalize): it deletes only the per-subtask branches and keeps
pila/runs/<id> (the PR head). `--branches` is broader and deletes
both the run branch and the subtask branches; they are mutually
exclusive.

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


def test_cleanup_declares_branches_flag():
    src = _src()
    assert '--branches)' in src


# --- run-scoping safety --------------------------------------------------

def test_cleanup_scopes_worktree_removal_to_run_dir():
    """--run-id only touches .pila/runs/<id>/worktrees/, not a
    top-level .pila/worktrees/. The construction is via a local
    `run_dir` variable: `run_dir=".pila/runs/${run_id}"` then
    `"${run_dir}/worktrees"`."""
    src = _src()
    clean_one_run = src.split("clean_one_run() {")[1].split("\n}")[0]
    # The run_dir variable is correctly anchored under runs/.
    assert 'run_dir=".pila/runs/${run_id}"' in clean_one_run
    # The worktrees path is derived from run_dir.
    assert '${run_dir}/worktrees' in clean_one_run
    # And the top-level path must NOT appear inside clean_one_run.
    assert '.pila/worktrees/' not in clean_one_run


def test_cleanup_branch_delete_scopes_to_run_id():
    """When --branches is passed, only pila/runs/<run-id> and
    pila/subtasks/<run-id>/* get deleted — NOT every pila/* branch.
    The two prefixes are disjoint so neither is an ancestor ref of the
    other (see compute_run_branch docstring)."""
    src = _src()
    # The for-each-ref patterns restrict to the run_id's namespace.
    assert 'refs/heads/pila/runs/${run_id}' in src
    assert 'refs/heads/pila/subtasks/${run_id}/' in src


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
    run_dir = repo / ".pila" / "runs" / run_id
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


def test_cleanup_subtask_branches_deletes_only_subtask_branches(tmp_path):
    """End-to-end: `cleanup.sh --run-id <id> --subtask-branches` deletes
    every `pila/subtasks/<id>/*` branch but keeps `pila/runs/<id>`
    (the PR head). State dir and the run branch must both survive."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "file").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    run_id = "feat-test-bbb222"
    run_dir = repo / ".pila" / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text('{"task": "test"}')

    # Create the run branch + three subtask branches off of main.
    subprocess.run(
        ["git", "branch", f"pila/runs/{run_id}", "main"],
        cwd=repo, check=True,
    )
    for sid in ("feat-001", "config-002", "feat-003"):
        subprocess.run(
            ["git", "branch", f"pila/subtasks/{run_id}/{sid}", "main"],
            cwd=repo, check=True,
        )

    r = subprocess.run(
        [str(CLEANUP_SH), "--run-id", run_id, "--subtask-branches"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"cleanup.sh failed: {r.stderr}"

    # The run branch must survive (it's the PR head).
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/pila/"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert f"pila/runs/{run_id}" in refs, (
        "cleanup.sh --subtask-branches must NOT delete the run branch "
        "(it's the PR head and must outlive the orchestrator)."
    )
    # Every subtask branch must be gone.
    for sid in ("feat-001", "config-002", "feat-003"):
        assert f"pila/subtasks/{run_id}/{sid}" not in refs, (
            f"cleanup.sh --subtask-branches must delete "
            f"pila/subtasks/{run_id}/{sid}"
        )
    # State dir survives.
    assert (run_dir / "state.json").exists()


def test_cleanup_branches_and_subtask_branches_mutually_exclusive(tmp_path):
    """Passing both --branches and --subtask-branches must error with
    exit 2 — they would otherwise conflict on whether to delete the
    run branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)

    r = subprocess.run(
        [str(CLEANUP_SH), "--run-id", "anything",
         "--branches", "--subtask-branches"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 2, f"expected exit 2; got {r.returncode}"
    assert "mutually exclusive" in r.stderr


def test_cleanup_bootstrap_removes_orphans(tmp_path):
    """End-to-end: `cleanup.sh --bootstrap` removes orphaned bootstrap
    directories."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    boot1 = repo / ".pila" / "runs" / "_bootstrap-aaaaaa"
    boot2 = repo / ".pila" / "runs" / "_bootstrap-bbbbbb"
    real = repo / ".pila" / "runs" / "feat-real-cccccc"
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
