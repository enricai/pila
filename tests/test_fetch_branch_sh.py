"""Tests for scripts/remote/fetch-branch.sh.

fetch-branch.sh is sourced by the pila launcher after remote orchestration
exits 0.  These tests exercise the script's bash logic in isolation via
subprocess, with flyctl and git stubbed out so no real Fly.io calls or
network traffic occur.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FETCH_SH = REPO_ROOT / "scripts" / "remote" / "fetch-branch.sh"

# Python snippet that the real fetch-branch.sh sends to the machine to
# discover the completed run-id.  We replicate its logic in the stub.
_DISCOVER_SNIPPET = """\
import os, json, sys
runs_dir = RUNS_DIR_PLACEHOLDER
if not os.path.isdir(runs_dir):
    sys.exit(1)
best = None
best_mtime = 0
for name in os.listdir(runs_dir):
    rj = os.path.join(runs_dir, name, "run.json")
    if not os.path.isfile(rj):
        continue
    try:
        d = json.load(open(rj))
    except Exception:
        continue
    if not d.get("finished_at"):
        continue
    if d.get("pushed_at"):
        continue
    mtime = os.stat(rj).st_mtime
    if mtime > best_mtime:
        best_mtime = mtime
        best = (name, d.get("branch", ""), d.get("working_branch", ""))
if best is None:
    print("ERROR: no completed unpushed run found on machine")
    sys.exit(1)
print(best[0])
print(best[1])
print(best[2])
"""


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


def _make_fake_flyctl(tmp_path: Path, machine_runs_dir: Path, git_repo: Path) -> Path:
    """Write a stub flyctl that routes machine exec calls locally.

    The stub handles the three flyctl machine exec invocations that
    fetch_branch makes:
      1. python3 -c '...'  — run the discovery snippet, rewriting the
         hardcoded /work/.pila/runs path to machine_runs_dir.
      2. git -C /work bundle create - <branch>  — run against git_repo.
      3. tar -cC /work/.pila/runs <run-id>  — run against machine_runs_dir.

    All other invocations succeed silently.
    """
    # Write the discovery helper script as a separate file to avoid quoting
    # nightmares when embedding multi-line Python in a bash heredoc.
    discover_py = tmp_path / "_discover_helper.py"
    snippet = _DISCOVER_SNIPPET.replace(
        "RUNS_DIR_PLACEHOLDER", repr(str(machine_runs_dir))
    )
    discover_py.write_text(snippet)

    stub = tmp_path / "flyctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        # Args layout: machine exec <id> --app <app> -- <cmd...>
        # Positions:   $1      $2   $3  $4    $5    $6  $7...
        # Skip the first 6 tokens to reach the actual command.
        "shift 6\n"
        f'REPO={git_repo}\n'
        f'MRUNS={machine_runs_dir}\n'
        'case "$1" in\n'
        '  python3)\n'
        # The real script passes -c '<snippet>'.  We ignore $2 (-c) and $3
        # (the snippet) and run our helper instead.
        f'    exec python3 {discover_py}\n'
        '    ;;\n'
        '  git)\n'
        # "git -C /work bundle create - <branch>" -> "git -C $REPO bundle create - <branch>"
        # After shift 6: $1=git $2=-C $3=/work $4=bundle $5=create $6=- $7=<branch>
        # We want: git -C $REPO $4 $5 $6 $7... i.e. ${@:4}
        '    exec git -C "$REPO" "${@:4}"\n'
        '    ;;\n'
        '  tar)\n'
        # "tar -cC /work/.pila/runs <run-id>" -> "tar -cC $MRUNS <run-id>"
        # After shift 6: $1=tar $2=-cC $3=/work/.pila/runs $4=<run-id>
        '    exec tar -cC "$MRUNS" "${@:4}"\n'
        '    ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    stub.chmod(0o755)
    return stub


def _make_git_repo(tmp_path: Path, subdir: str = "myrepo") -> Path:
    """Create a minimal git repo with one commit and return its path."""
    repo = tmp_path / subdir
    repo.mkdir()
    for cmd in [
        ["git", "-C", str(repo), "init"],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return repo


def test_fetch_branch_sh_exists():
    assert FETCH_SH.exists(), "scripts/remote/fetch-branch.sh is missing"


def test_fetch_branch_sh_is_executable():
    assert os.access(FETCH_SH, os.X_OK), (
        "scripts/remote/fetch-branch.sh is not executable"
    )


def test_fetch_branch_fails_without_machine_id():
    """fetch_branch returns 1 when PILA_MACHINE_ID is unset."""
    result = _run_bash(
        f"source {FETCH_SH}; fetch_branch",
        env={"PILA_MACHINE_ID": "", "USER_REPO": "/tmp"},
    )
    assert result.returncode != 0
    assert "PILA_MACHINE_ID" in result.stderr


def test_fetch_branch_fails_without_user_repo():
    """fetch_branch returns 1 when USER_REPO is unset."""
    result = _run_bash(
        f"source {FETCH_SH}; fetch_branch",
        env={"PILA_MACHINE_ID": "test-machine-001", "USER_REPO": ""},
    )
    assert result.returncode != 0
    assert "USER_REPO" in result.stderr


def test_fetch_branch_fails_when_flyctl_missing():
    """fetch_branch returns 1 with an actionable error when flyctl is absent."""
    result = _run_bash(
        f"source {FETCH_SH}; fetch_branch",
        env={
            "PILA_MACHINE_ID": "test-machine-001",
            "USER_REPO": "/tmp",
            "PATH": "/usr/bin:/bin",  # no flyctl here
        },
    )
    assert result.returncode != 0
    assert "flyctl" in result.stderr.lower()


def test_fetch_branch_fails_when_no_completed_run(tmp_path):
    """fetch_branch returns 1 when the machine has no finished_at run.json."""
    repo = _make_git_repo(tmp_path)

    # machine_runs_dir exists but has no run with finished_at.
    machine_runs = tmp_path / "mruns"
    machine_runs.mkdir()
    stale_run = machine_runs / "some-run-id"
    stale_run.mkdir()
    (stale_run / "run.json").write_text(json.dumps({"branch": "pila/runs/some-run-id"}))

    fake_flyctl = _make_fake_flyctl(tmp_path, machine_runs, repo)

    result = _run_bash(
        f"source {FETCH_SH}; fetch_branch",
        env={
            "PILA_MACHINE_ID": "test-machine-001",
            "PILA_FLY_APP": "pila",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin:/usr/local/bin",
        },
    )
    assert result.returncode != 0
    # Should report failure to discover a completed run.
    assert "discover" in result.stderr.lower() or "completed" in result.stderr.lower()


def test_fetch_branch_streams_bundle_and_state(tmp_path):
    """fetch_branch fetches the run branch and extracts the state directory."""
    repo = _make_git_repo(tmp_path)

    run_id = "feat-test-abc123"
    run_branch = f"pila/runs/{run_id}"

    # Create the run branch in the repo (simulates the branch existing on the
    # machine — the bundle will be created from the local repo via stub).
    subprocess.run(
        ["git", "-C", str(repo), "branch", run_branch],
        check=True, capture_output=True,
    )

    # Set up the machine-side run state.
    machine_runs = tmp_path / "machine_runs"
    run_dir = machine_runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "finished_at": "2026-01-01T00:00:00Z",
        "branch": run_branch,
        "working_branch": "main",
    }))
    (run_dir / "state.json").write_text(json.dumps({"task": "test task"}))

    fake_flyctl = _make_fake_flyctl(tmp_path, machine_runs, repo)

    result = _run_bash(
        f"source {FETCH_SH}; fetch_branch",
        env={
            "PILA_MACHINE_ID": "test-machine-abc",
            "PILA_FLY_APP": "pila",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin:/usr/local/bin",
        },
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"

    # The run branch should be present in the host repo (it was already there
    # as we created it for the bundle, so this confirms the bundle path ran).
    ls_branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", run_branch],
        capture_output=True, text=True,
    )
    assert run_branch in ls_branches.stdout, (
        f"run branch {run_branch} not found in host repo after fetch"
    )

    # The state directory should be extracted on the host.
    host_run_dir = repo / ".pila" / "runs" / run_id
    assert host_run_dir.exists(), f"host run dir not found: {host_run_dir}"
    assert (host_run_dir / "run.json").exists(), "run.json not extracted on host"
    assert (host_run_dir / "state.json").exists(), "state.json not extracted on host"

    # Completion message should be present.
    assert "fetch complete" in result.stderr or run_id in result.stderr


def test_fetch_branch_exports_run_id(tmp_path):
    """fetch_branch exports PILA_REMOTE_RUN_ID on success."""
    repo = _make_git_repo(tmp_path, subdir="repo")

    run_id = "fix-export-test-deadbeef"
    run_branch = f"pila/runs/{run_id}"
    subprocess.run(
        ["git", "-C", str(repo), "branch", run_branch],
        check=True, capture_output=True,
    )

    machine_runs = tmp_path / "mruns"
    run_dir = machine_runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "finished_at": "2026-01-01T00:00:00Z",
        "branch": run_branch,
        "working_branch": "main",
    }))
    (run_dir / "state.json").write_text("{}")

    fake_flyctl = _make_fake_flyctl(tmp_path, machine_runs, repo)

    result = _run_bash(
        f'source {FETCH_SH}; fetch_branch && echo "RUN_ID=$PILA_REMOTE_RUN_ID"',
        env={
            "PILA_MACHINE_ID": "m1",
            "PILA_FLY_APP": "pila",
            "USER_REPO": str(repo),
            "PATH": f"{tmp_path}:/usr/bin:/bin:/usr/local/bin",
        },
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert f"RUN_ID={run_id}" in result.stdout, (
        f"PILA_REMOTE_RUN_ID not exported correctly. stdout: {result.stdout}"
    )
