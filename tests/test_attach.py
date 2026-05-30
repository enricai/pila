"""Tests for Phase 3: PTY-over-SSH attach (`pila --attach`).

Covers:
  - scripts/remote/attach.sh resolution logic
  - provision.sh writes/removes the PID-keyed attach pointer
  - Launcher fast-path routes --attach before runtime preflight
  - Coupling: attach.sh schema matches provision.sh's record

flyctl ssh console itself is stubbed — we assert on the command
attach.sh prints (it logs the command to stderr before exec) without
actually opening a session.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ATTACH_SH = REPO_ROOT / "scripts" / "remote" / "attach.sh"
PROVISION_SH = REPO_ROOT / "scripts" / "remote" / "provision.sh"
LAUNCHER = REPO_ROOT / "pila"


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


def _stub_flyctl(tmp_path: Path) -> Path:
    """Write a flyctl stub that records ssh console invocations."""
    log = tmp_path / "flyctl.log"
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        "  'ssh console') exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


# --- attach.sh: prerequisites --------------------------------------------

def test_attach_sh_exists():
    assert ATTACH_SH.exists()
    assert os.access(ATTACH_SH, os.X_OK)


def test_attach_fails_when_flyctl_missing(tmp_path: Path):
    """attach refuses to run without flyctl on PATH."""
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    result = _run_bash(
        f"{ATTACH_SH} my-run",
        env={
            "PATH": "/usr/bin:/bin",  # no flyctl
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode != 0
    assert "flyctl" in result.stderr.lower()


def test_attach_fails_when_flyctl_unauthenticated(tmp_path: Path):
    """attach refuses to run when flyctl auth status fails."""
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1 $2\" = 'auth status' ]; then exit 1; fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    result = _run_bash(
        f"{ATTACH_SH} my-run",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode != 0
    assert "auth" in result.stderr.lower()


# --- attach.sh: resolution strategies ------------------------------------

def test_attach_resolves_from_fly_machine_json(tmp_path: Path):
    """A .pila/runs/<id>/fly-machine.json record provides the machine id."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run-001"
    run_dir.mkdir(parents=True)
    (run_dir / "fly-machine.json").write_text(json.dumps({
        "fly_app": "pila",
        "fly_machine_id": "mach-from-fly-machine-json",
        "started_at": "2026-05-29T16:00:00+00:00",
        "run_id": "my-run-001",
        "launcher_pid": 1,
    }))
    result = _run_bash(
        f"{ATTACH_SH} my-run-001",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "ssh console" in invocations
    assert "mach-from-fly-machine-json" in invocations


def test_attach_falls_back_to_run_json(tmp_path: Path):
    """If fly-machine.json doesn't exist, attach reads fly_machine_id
    from run.json (Phase 2's sidecar field)."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run-002"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "my-run-002",
        "fly_machine_id": "mach-from-run-json",
        "paused_at": "2026-05-29T16:00:00+00:00",
    }))
    result = _run_bash(
        f"{ATTACH_SH} my-run-002",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "mach-from-run-json" in invocations


def test_attach_resolves_from_pid_record_when_no_run_id(tmp_path: Path):
    """No run-id: scan .pila/remote/<pid>.json; exactly one active record
    means use it."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    remote_dir = user_repo / ".pila" / "remote"
    remote_dir.mkdir(parents=True)
    # Use this process's PID so the kill -0 check succeeds.
    my_pid = os.getpid()
    (remote_dir / f"{my_pid}.json").write_text(json.dumps({
        "fly_app": "pila",
        "fly_machine_id": "mach-from-pid-record",
        "started_at": "2026-05-29T16:00:00+00:00",
        "run_id": None,
        "launcher_pid": my_pid,
    }))
    result = _run_bash(
        f"{ATTACH_SH}",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "mach-from-pid-record" in invocations


def test_attach_errors_on_no_active_records(tmp_path: Path):
    """No records and no run-id → 'no active remote machine' + exit 1."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    result = _run_bash(
        f"{ATTACH_SH}",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 1
    assert "no active remote machine" in result.stderr


def test_attach_errors_on_multiple_active_records(tmp_path: Path):
    """Multiple active records without a disambiguating run-id → exit 1."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    remote_dir = user_repo / ".pila" / "remote"
    remote_dir.mkdir(parents=True)
    my_pid = os.getpid()
    parent_pid = os.getppid()
    for pid, mid in ((my_pid, "mach-A"), (parent_pid, "mach-B")):
        (remote_dir / f"{pid}.json").write_text(json.dumps({
            "fly_app": "pila",
            "fly_machine_id": mid,
            "started_at": "2026-05-29T16:00:00+00:00",
            "run_id": None,
            "launcher_pid": pid,
        }))
    result = _run_bash(
        f"{ATTACH_SH}",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 1
    assert "multiple active" in result.stderr
    assert "mach-A" in result.stderr
    assert "mach-B" in result.stderr


def test_attach_skips_stale_pid_records(tmp_path: Path):
    """A record whose PID no longer exists must be skipped (stale)."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    remote_dir = user_repo / ".pila" / "remote"
    remote_dir.mkdir(parents=True)
    # A PID that's astronomically unlikely to exist.
    stale_pid = 999999
    (remote_dir / f"{stale_pid}.json").write_text(json.dumps({
        "fly_machine_id": "mach-stale",
        "launcher_pid": stale_pid,
    }))
    result = _run_bash(
        f"{ATTACH_SH}",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 1
    assert "no active remote machine" in result.stderr


def test_attach_errors_on_missing_run(tmp_path: Path):
    """A run-id that doesn't exist on disk → exit 1 with actionable message."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    result = _run_bash(
        f"{ATTACH_SH} non-existent-run",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 1
    assert "non-existent-run" in result.stderr


# --- attach.sh: command shapes --------------------------------------------

def test_attach_default_command_is_bash(tmp_path: Path):
    """Default attach drops into bash at /work with $PS1 identifying the run."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run-003"
    run_dir.mkdir(parents=True)
    (run_dir / "fly-machine.json").write_text(json.dumps({
        "fly_machine_id": "mach-default",
    }))
    result = _run_bash(
        f"{ATTACH_SH} my-run-003",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "cd /work" in invocations
    assert "pila@my-run-003" in invocations
    assert "exec bash" in invocations


def test_attach_tail_mode_replaces_bash_with_tail(tmp_path: Path):
    """--tail replaces the bash shell with tail -F of the orchestrator log."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run-004"
    run_dir.mkdir(parents=True)
    (run_dir / "fly-machine.json").write_text(json.dumps({
        "fly_machine_id": "mach-tail",
    }))
    result = _run_bash(
        f"{ATTACH_SH} my-run-004 --tail",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "tail -F" in invocations
    assert "/work/.pila/runs/my-run-004/logs" in invocations
    assert "exec bash" not in invocations


def test_attach_app_flag_overrides_default(tmp_path: Path):
    """--app overrides the default 'pila' app name."""
    _stub_flyctl(tmp_path)
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "my-run-005"
    run_dir.mkdir(parents=True)
    (run_dir / "fly-machine.json").write_text(json.dumps({
        "fly_machine_id": "mach-app",
    }))
    result = _run_bash(
        f"{ATTACH_SH} my-run-005 --app=custom-app",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "--app custom-app" in invocations


# --- provision.sh writes the PID-keyed record ----------------------------

def test_provision_writes_pid_keyed_record(tmp_path: Path):
    """After successful provision, .pila/remote/<pid>.json exists with
    fly_machine_id and run_id fields."""
    log = tmp_path / "flyctl.log"
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        "  'machine run') echo '{\"id\":\"mach-001\",\"state\":\"created\"}'; exit 0 ;;\n"
        "  'machine status') echo '{\"state\":\"started\"}'; exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    # Use a subshell so the EXIT trap (decide_teardown) fires when it exits
    # — we want to inspect the pid record BEFORE destroy_machine removes
    # it. We capture the file mid-flight by printing its contents inside
    # the subshell before exit.
    result = _run_bash(
        f"( source {PROVISION_SH}; "
        f"  PILA_RUN_ID=my-run-006; "
        f"  USER_REPO={user_repo}; "
        f"  export USER_REPO PILA_RUN_ID; "
        f"  provision_machine && "
        f'  cat "{user_repo}/.pila/remote/$$.json"; '
        f")",
        env={
            "FLY_IMAGE_TAG": "registry.fly.io/pila:test",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout.strip())
    assert record["fly_machine_id"] == "mach-001"
    assert record["fly_app"] == "pila"
    assert record["run_id"] == "my-run-006"
    assert isinstance(record["launcher_pid"], int)


def test_destroy_removes_pid_keyed_record(tmp_path: Path):
    """destroy_machine removes the PID-keyed record so subsequent
    `pila --attach` reports 'no active remote machine'."""
    log = tmp_path / "flyctl.log"
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        "  'machine run') echo '{\"id\":\"mach-001\",\"state\":\"created\"}'; exit 0 ;;\n"
        "  'machine status') echo '{\"state\":\"started\"}'; exit 0 ;;\n"
        "  'machine destroy') exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    # Run-in-subshell so the EXIT trap fires; then check the directory.
    result = _run_bash(
        f"( source {PROVISION_SH}; "
        f"  USER_REPO={user_repo}; export USER_REPO; "
        f"  provision_machine; "
        f"  echo \"during=$(ls {user_repo}/.pila/remote/ 2>/dev/null | wc -l)\"; "
        f"); "
        f"echo \"after=$(ls {user_repo}/.pila/remote/ 2>/dev/null | wc -l)\"",
        env={
            "FLY_IMAGE_TAG": "registry.fly.io/pila:test",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    # macOS `wc -l` pads with spaces; just check the count.
    during = result.stdout.split("during=")[1].split("\n")[0].strip()
    after = result.stdout.split("after=")[1].split("\n")[0].strip()
    assert during == "1", f"expected 1 record during run, got {during!r}"
    assert after == "0", f"expected 0 records after destroy, got {after!r}"


# --- launcher fast-path --------------------------------------------------

def test_launcher_routes_attach_fastpath():
    """The launcher's --attach case must be in the early fast-path block
    (before runtime preflight), so attach works on hosts without nerdctl."""
    text = LAUNCHER.read_text()
    # Find the --version case position and the --attach case position.
    version_idx = text.find('--version)')
    attach_idx = text.find('--attach)')
    runtime_preflight_idx = text.find('# --- platform preflight')
    assert version_idx != -1
    assert attach_idx != -1
    assert runtime_preflight_idx != -1
    # --attach must come before the runtime preflight (so no Colima/nerdctl
    # is required) and be in the same fast-path block as --version.
    assert attach_idx < runtime_preflight_idx, (
        "--attach must run before the runtime preflight"
    )


def test_launcher_attach_execs_attach_sh():
    """The --attach branch in pila must exec scripts/remote/attach.sh."""
    text = LAUNCHER.read_text()
    assert 'exec "$PILA_REPO/scripts/remote/attach.sh"' in text


# --- coupling: schema match between provision.sh and attach.sh -----------

def test_pid_record_schema_consumed_by_attach():
    """The fields provision.sh writes must match what attach.sh reads."""
    provision_text = PROVISION_SH.read_text()
    attach_text = ATTACH_SH.read_text()
    # provision.sh writes these field names:
    for field in ("fly_app", "fly_machine_id", "started_at", "run_id", "launcher_pid"):
        assert f'"{field}"' in provision_text, f"provision.sh missing {field}"
    # attach.sh extracts at least fly_machine_id, run_id, launcher_pid:
    # (fly_app is not read by attach.sh — the --app flag overrides it; the
    # default comes from $PILA_FLY_APP for both writer and reader.)
    for field in ("fly_machine_id", "launcher_pid", "run_id"):
        assert field in attach_text, f"attach.sh missing {field}"
