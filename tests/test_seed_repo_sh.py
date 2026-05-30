"""Tests for scripts/remote/seed-repo.sh.

seed-repo.sh is sourced by the pila launcher after provision_machine()
succeeds.  These tests exercise the script's bash logic in isolation via
subprocess, with flyctl and git stubbed out so no real Fly.io calls or
network traffic occur.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SH = REPO_ROOT / "scripts" / "remote" / "seed-repo.sh"


def _run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base_env = {k: v for k, v in os.environ.items()}
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        env=base_env,
        capture_output=True,
        text=True,
    )


def test_seed_repo_sh_exists():
    assert SEED_SH.exists(), "scripts/remote/seed-repo.sh is missing"


def test_seed_repo_sh_is_executable():
    assert os.access(SEED_SH, os.X_OK), (
        "scripts/remote/seed-repo.sh is not executable"
    )


def test_seed_repo_fails_without_machine_id():
    """seed_repo returns 1 when PILA_MACHINE_ID is unset."""
    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={"PILA_MACHINE_ID": "", "USER_REPO": "/tmp"},
    )
    assert result.returncode != 0
    assert "PILA_MACHINE_ID" in result.stderr


def test_seed_repo_fails_without_user_repo():
    """seed_repo returns 1 when USER_REPO is unset."""
    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={"PILA_MACHINE_ID": "test-machine-001", "USER_REPO": ""},
    )
    assert result.returncode != 0
    assert "USER_REPO" in result.stderr


def test_seed_repo_fails_when_flyctl_missing():
    """seed_repo returns 1 with an actionable error when flyctl is absent."""
    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={
            "PILA_MACHINE_ID": "test-machine-001",
            "USER_REPO": "/tmp",
            "PATH": "/usr/bin:/bin",  # no flyctl here
        },
    )
    assert result.returncode != 0
    assert "flyctl" in result.stderr.lower()


def test_seed_repo_fails_when_origin_missing(tmp_path):
    """seed_repo returns 1 when the git repo has no origin remote."""
    # Create a bare local git repo with no remote.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    # Create a stub flyctl that is present.
    fake_flyctl = tmp_path / "flyctl"
    fake_flyctl.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_flyctl.chmod(0o755)

    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={
            "PILA_MACHINE_ID": "test-machine-001",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode != 0
    assert "origin" in result.stderr.lower() or "remote" in result.stderr.lower()


def test_seed_repo_clones_and_syncs_delta(tmp_path):
    """seed_repo runs git clone on remote then tars the dirty set."""
    # Set up a local git repo with an origin remote and a dirty file.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    # Add a committed file and an untracked dirty file.
    tracked = repo / "README.md"
    tracked.write_text("hello")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    untracked = repo / "local_notes.txt"
    untracked.write_text("my notes")

    # Add a fake origin remote pointing at a local bare clone (won't be
    # actually cloned — we stub flyctl machine exec).
    origin_dir = tmp_path / "origin.git"
    origin_dir.mkdir()
    subprocess.run(["git", "init", "--bare", str(origin_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(origin_dir)],
        check=True, capture_output=True,
    )

    # Stub flyctl: machine exec records its arguments to a log file.
    # Drain stdin so the tar producer doesn't get SIGPIPE (simulates flyctl
    # forwarding stdin to the remote command, which consumes it).
    exec_log = tmp_path / "exec_log.txt"
    fake_flyctl = tmp_path / "flyctl"
    fake_flyctl.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"flyctl $*\" >> {exec_log}\n"
        # Consume all stdin so the tar producer doesn't get SIGPIPE.
        "cat > /dev/null\n"
        "exit 0\n"
    )
    fake_flyctl.chmod(0o755)

    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={
            "PILA_MACHINE_ID": "test-machine-abc",
            "PILA_FLY_APP": "pila",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    # Verify git clone was invoked on the remote.
    assert exec_log.exists(), "flyctl was never called"
    log_text = exec_log.read_text()
    assert "git clone" in log_text, f"git clone not in flyctl calls:\n{log_text}"
    assert "--filter=blob:none" in log_text, (
        "partial clone flag missing — shallow clone disqualified by worktree constraint"
    )
    assert "/work" in log_text, "clone target /work not specified"

    # Verify tar/rsync step was invoked (dirty set includes local_notes.txt).
    assert "tar" in log_text, f"tar delta step not called:\n{log_text}"


def test_seed_repo_clean_tree_skips_delta(tmp_path):
    """seed_repo skips the delta step when the working tree is clean."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    tracked = repo / "README.md"
    tracked.write_text("hello")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    # No untracked/modified files — clean tree.
    origin_dir = tmp_path / "origin.git"
    origin_dir.mkdir()
    subprocess.run(["git", "init", "--bare", str(origin_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(origin_dir)],
        check=True, capture_output=True,
    )

    exec_log = tmp_path / "exec_log.txt"
    fake_flyctl = tmp_path / "flyctl"
    fake_flyctl.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"flyctl $*\" >> {exec_log}\n"
        "exit 0\n"
    )
    fake_flyctl.chmod(0o755)

    result = _run_bash(
        f"source {SEED_SH}; seed_repo",
        env={
            "PILA_MACHINE_ID": "test-machine-clean",
            "PILA_FLY_APP": "pila",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "clean" in result.stderr.lower() or "no delta" in result.stderr.lower(), (
        f"Expected clean-tree message in stderr:\n{result.stderr}"
    )

    log_text = exec_log.read_text() if exec_log.exists() else ""
    # git clone should still run; tar delta should NOT.
    assert "git clone" in log_text
    assert "tar" not in log_text, (
        "tar was called for a clean tree — unnecessary uplink traffic"
    )
