"""Tests for Phase 4: mid-run re-rsync (`pila --re-seed` + auto-on-resume).

Covers:
  - scripts/remote/seed-repo.sh refactor (seed_repo_clone / seed_repo_dirty / seed_repo)
  - scripts/remote/re-seed.sh: re_seed() reads sidecar, wakes machine,
    runs safety check, calls seed_repo_dirty
  - Safety check: refuse re-seed when remote /work has uncommitted tracked
    changes (unless --force)
  - Launcher: --re-seed fast-path
  - Launcher: --no-re-seed and --force are consumed (not forwarded to orchestrator)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_REPO_SH = REPO_ROOT / "scripts" / "remote" / "seed-repo.sh"
RE_SEED_SH = REPO_ROOT / "scripts" / "remote" / "re-seed.sh"
PROVISION_SH = REPO_ROOT / "scripts" / "remote" / "provision.sh"
LAUNCHER = REPO_ROOT / "pila"


def _run_bash(script: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    base_env = {k: v for k, v in os.environ.items()}
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        env=base_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def _make_git_repo(repo_dir: Path) -> None:
    """Initialise a git repo with one committed file."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.com/repo.git"],
                   cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _stub_flyctl(tmp_path: Path, *, remote_status: str = "started",
                 remote_dirty: str = "") -> Path:
    """Stub flyctl with controllable machine state and git-status output.

    The state is stored in a file so `machine start` can flip the
    reported `machine status` from stopped → started, matching real Fly
    behaviour.
    """
    log = tmp_path / "flyctl.log"
    state_file = tmp_path / "stub-machine-state"
    state_file.write_text(remote_status)
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        f'STATE_FILE="{state_file}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        '  "machine status") printf "{\\"state\\":\\"%s\\"}" "$(cat $STATE_FILE)"; exit 0 ;;\n'
        '  "machine start") echo "started" > "$STATE_FILE"; exit 0 ;;\n'
        '  "machine stop") echo "stopped" > "$STATE_FILE"; exit 0 ;;\n'
        '  "machine destroy") echo "destroyed" > "$STATE_FILE"; exit 0 ;;\n'
        "esac\n"
        'if [ "$1" = "machine" ] && [ "$2" = "exec" ]; then\n'
        "  found_dashes=0\n"
        '  for arg in "$@"; do\n'
        '    if [ "$arg" = "--" ]; then found_dashes=1; continue; fi\n'
        "    if [ \"$found_dashes\" = \"1\" ]; then\n"
        '      case "$arg" in\n'
        '        git)\n'
        f'          printf "%s" "{remote_dirty}"\n'
        "          exit 0\n"
        "          ;;\n"
        '        tar)\n'
        "          cat > /dev/null\n"
        "          exit 0\n"
        "          ;;\n"
        "      esac\n"
        "      exit 0\n"
        "    fi\n"
        "  done\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


# --- seed-repo.sh refactor preserved contract -----------------------------

def test_seed_repo_clone_function_exists():
    """seed_repo_clone is a publicly-callable function after refactor."""
    result = _run_bash(
        f"source {SEED_REPO_SH}; declare -f seed_repo_clone >/dev/null && echo OK"
    )
    assert "OK" in result.stdout


def test_seed_repo_dirty_function_exists():
    """seed_repo_dirty is a publicly-callable function after refactor."""
    result = _run_bash(
        f"source {SEED_REPO_SH}; declare -f seed_repo_dirty >/dev/null && echo OK"
    )
    assert "OK" in result.stdout


def test_seed_repo_wrapper_still_exists():
    """seed_repo (the wrapper) is still callable so existing callers don't break."""
    result = _run_bash(
        f"source {SEED_REPO_SH}; declare -f seed_repo >/dev/null && echo OK"
    )
    assert "OK" in result.stdout


# --- re_seed: argument validation -----------------------------------------

def test_re_seed_requires_run_id(tmp_path: Path):
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={"USER_REPO": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "PILA_RUN_ID" in result.stderr


def test_re_seed_requires_sidecar(tmp_path: Path):
    """re_seed errors when the run.json sidecar is missing."""
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": "nonexistent",
        },
    )
    assert result.returncode != 0
    assert "run.json" in result.stderr


def test_re_seed_requires_fly_machine_id(tmp_path: Path):
    """re_seed errors when the sidecar exists but has no fly_machine_id."""
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "my-run"}))
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": "my-run",
        },
    )
    assert result.returncode != 0
    assert "fly_machine_id" in result.stderr


# --- re_seed: happy path on a clean machine -------------------------------

def test_re_seed_starts_stopped_machine_and_calls_dirty(tmp_path: Path):
    """re_seed wakes a stopped machine and runs seed_repo_dirty."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    # Add an uncommitted edit so seed_repo_dirty has something to send.
    (repo / "edit.txt").write_text("new file\n")

    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-001",
        "paused_at": "2026-05-29T16:00:00+00:00",
    }))

    _stub_flyctl(tmp_path, remote_status="stopped", remote_dirty="")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PILA_MACHINE_START_TIMEOUT": "5",
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine start mach-001" in invocations
    # tar pipe should have been invoked for the dirty file.
    assert "machine exec" in invocations


def test_re_seed_skips_start_when_machine_already_started(tmp_path: Path):
    """re_seed doesn't try to start a machine that's already 'started'."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-001",
    }))
    _stub_flyctl(tmp_path, remote_status="started", remote_dirty="")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine start" not in invocations


def test_re_seed_refuses_destroyed_machine(tmp_path: Path):
    """re_seed errors when the machine has been destroyed."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-gone",
    }))
    _stub_flyctl(tmp_path, remote_status="destroyed")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 1
    assert "destroyed" in result.stderr


# --- re_seed: safety check on machine-side dirty state --------------------

def test_re_seed_refuses_when_machine_has_dirty_tracked_files(tmp_path: Path):
    """re_seed refuses (without --force) when /work on the machine has
    uncommitted tracked changes that aren't under .pila/."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-001",
    }))
    # Stub the machine's git status to show a dirty src/foo.py.
    _stub_flyctl(tmp_path, remote_status="started",
                 remote_dirty=" M src/foo.py\n")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert "src/foo.py" in result.stderr
    assert "--force" in result.stderr


def test_re_seed_ignores_pila_dirty_paths(tmp_path: Path):
    """Dirty paths under .pila/ are expected (worker state) and don't trip
    the safety check."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-001",
    }))
    _stub_flyctl(tmp_path, remote_status="started",
                 remote_dirty=" M .pila/runs/my-run/state.json\n M .pila/runs/my-run/logs/orch.log\n")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr


def test_re_seed_force_bypasses_safety_check(tmp_path: Path):
    """PILA_RE_SEED_FORCE=1 bypasses the dirty-state safety check."""
    repo = tmp_path / "user-repo"
    _make_git_repo(repo)
    run_dir = repo / ".pila" / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run",
        "fly_machine_id": "mach-001",
    }))
    _stub_flyctl(tmp_path, remote_status="started",
                 remote_dirty=" M src/foo.py\n")
    result = _run_bash(
        f"source {PROVISION_SH}; source {SEED_REPO_SH}; source {RE_SEED_SH}; re_seed",
        env={
            "USER_REPO": str(repo),
            "PILA_RUN_ID": "my-run",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PILA_RE_SEED_FORCE": "1",
        },
    )
    assert result.returncode == 0, result.stderr


# --- launcher fast-path ----------------------------------------------------

def test_launcher_re_seed_fastpath_present():
    """The launcher has a --re-seed fast-path before runtime preflight."""
    text = LAUNCHER.read_text()
    assert "--re-seed)" in text
    re_seed_idx = text.find("--re-seed)")
    preflight_idx = text.find("# --- platform preflight")
    assert re_seed_idx < preflight_idx, (
        "--re-seed fast-path must run before runtime preflight"
    )


def test_launcher_consumes_re_seed_flags():
    """--no-re-seed and --force are launcher-only — not forwarded to the
    orchestrator's argparse via REWRITTEN_ARGS."""
    text = LAUNCHER.read_text()
    assert "--no-re-seed)" in text
    assert "NO_RE_SEED=true" in text
    assert "RE_SEED_FORCE=true" in text


def test_launcher_re_seed_requires_run_id_arg():
    """`pila --re-seed` without <run-id> errors with usage."""
    result = _run_bash(
        f"{LAUNCHER} --re-seed",
    )
    assert result.returncode != 0
    assert "requires a <run-id>" in result.stderr
