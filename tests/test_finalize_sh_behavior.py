"""Behavioral tests for scripts/finalize.sh.

finalize.sh is a thin verifier: confirm the per-run branch exists and has
work the working branch doesn't already have. It must NOT modify HEAD or
the working branch. These tests build a real tmp_path git repo and
exercise the exit codes + stdout/stderr text for each documented path.

Companion to tests/test_phase_finalize_no_local_merge.py (source-text
pins). Together they cover both "the merge wiring is gone" (pins) and
"the new verifier behaves correctly" (these).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FINALIZE_SH = REPO_ROOT / "scripts" / "finalize.sh"


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo on branch `main` with one commit on
    `main` and one further commit on `pila/runs/test`. Returns the
    repo root path. Caller must populate
    `.pila/runs/test/working-branch` to point at the desired base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a").write_text("a")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "a"], cwd=repo, check=True)
    return repo


def _add_run_branch_with_extra_commit(repo: Path) -> None:
    subprocess.run(["git", "checkout", "-qb", "pila/runs/test"],
                   cwd=repo, check=True)
    (repo / "b").write_text("b")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=repo, check=True)


def _write_working_branch_file(repo: Path, value: str) -> None:
    run_dir = repo / ".pila" / "runs" / "test"
    run_dir.mkdir(parents=True)
    (run_dir / "working-branch").write_text(value)


def test_happy_path_exits_zero(tmp_path):
    """Run branch is 1 commit ahead of working branch → exit 0."""
    repo = _init_repo(tmp_path)
    _add_run_branch_with_extra_commit(repo)
    _write_working_branch_file(repo, "main")

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"expected exit 0; got {r.returncode}, stderr={r.stderr!r}"
    assert "ready to push" in r.stdout
    assert "1 commit(s) ahead of main" in r.stdout


def test_run_branch_missing_exits_two(tmp_path):
    """working-branch file present but the run branch ref does not exist."""
    repo = _init_repo(tmp_path)
    _write_working_branch_file(repo, "main")
    # No pila/runs/test branch created.

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2, f"expected exit 2; got {r.returncode}, stdout={r.stdout!r}"
    assert "does not exist" in r.stderr
    assert "nothing to finalize" in r.stderr


def test_working_branch_missing_exits_two(tmp_path):
    """working-branch file points at a branch that does not exist locally."""
    repo = _init_repo(tmp_path)
    _add_run_branch_with_extra_commit(repo)
    _write_working_branch_file(repo, "branch-that-does-not-exist")

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2, f"expected exit 2; got {r.returncode}, stdout={r.stdout!r}"
    assert "no longer exists" in r.stderr
    # The error must clearly attribute the failure to the working branch,
    # not to "nothing to push" — that was the original silent-degradation
    # defect this test guards against.
    assert "working branch" in r.stderr


def test_ahead_zero_exits_one(tmp_path):
    """Run branch ref exists but has no commits beyond the working branch."""
    repo = _init_repo(tmp_path)
    # Create pila/runs/test pointing at the same commit as main.
    subprocess.run(["git", "branch", "pila/runs/test", "main"],
                   cwd=repo, check=True)
    _write_working_branch_file(repo, "main")

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 1, f"expected exit 1; got {r.returncode}, stdout={r.stdout!r}"
    assert "no commits beyond main" in r.stderr
    assert "nothing to push" in r.stderr


def test_working_branch_file_missing_exits_two(tmp_path):
    """No .pila/runs/<id>/working-branch file → exit 2 with the
    setup-run.sh instruction."""
    repo = _init_repo(tmp_path)
    _add_run_branch_with_extra_commit(repo)
    # Do NOT create the working-branch file.

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 2, f"expected exit 2; got {r.returncode}, stdout={r.stdout!r}"
    assert "working-branch" in r.stderr


def test_does_not_modify_working_branch_head(tmp_path):
    """The whole point of the design change: finalize.sh must NOT change
    HEAD or modify the working branch's tip. This is the behavioral
    invariant that closes the loop on the source-text pins."""
    repo = _init_repo(tmp_path)
    _add_run_branch_with_extra_commit(repo)
    # Now switch back to main so HEAD points at the working branch tip.
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    _write_working_branch_file(repo, "main")

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    main_before = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    current_before = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    r = subprocess.run([str(FINALIZE_SH), "test"], cwd=repo,
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    main_after = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    current_after = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert head_before == head_after, "finalize.sh moved HEAD"
    assert main_before == main_after, "finalize.sh moved the working branch tip"
    assert current_before == current_after, (
        "finalize.sh switched the user off their working branch"
    )
