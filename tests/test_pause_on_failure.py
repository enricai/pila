"""Tests for Phase 2 (pause-on-failure) bash surface.

Covers:
  - lib.sh:  update_run_json atomic merge
  - lib.sh:  iso_now timestamp
  - provision.sh: stop_machine idempotency + no-op when machine id empty
  - provision.sh: decide_teardown classification (rc → stop vs destroy)
  - provision.sh: decide_teardown writes paused_at + pause_reason + fly_machine_id
  - resume-machine.sh: resume_machine starts a stopped machine and clears paused_at

All tests stub flyctl so no real Fly.io calls are made.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SH = REPO_ROOT / "scripts" / "remote" / "lib.sh"
PROVISION_SH = REPO_ROOT / "scripts" / "remote" / "provision.sh"
RESUME_SH = REPO_ROOT / "scripts" / "remote" / "resume-machine.sh"


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


def _make_flyctl_stub(tmp_path: Path, *, behavior: str) -> Path:
    """Write a stub flyctl that records its argv to flyctl.log.

    behavior options:
      "happy"  — auth ok, machine run returns JSON id, status started, stop/destroy ok
      "stop_ok" — machine stop returns 0
    """
    log = tmp_path / "flyctl.log"
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        "  'machine run') echo '{\"id\":\"mach-001\",\"state\":\"created\"}'; exit 0 ;;\n"
        "  'machine status') echo '{\"state\":\"started\"}'; exit 0 ;;\n"
        "  'machine stop') exit 0 ;;\n"
        "  'machine destroy') exit 0 ;;\n"
        "  'machine start') exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake


# --- lib.sh ----------------------------------------------------------------

def test_update_run_json_creates_fields(tmp_path: Path):
    """update_run_json merges new fields into an existing sidecar."""
    sidecar = tmp_path / "run.json"
    sidecar.write_text(json.dumps({"run_id": "test-001", "branch": "pila/runs/test-001"}))
    result = _run_bash(
        f"source {LIB_SH}; update_run_json {sidecar} fly_machine_id mach-abc paused_at 2026-05-29T16:00:00+00:00",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(sidecar.read_text())
    assert data["fly_machine_id"] == "mach-abc"
    assert data["paused_at"] == "2026-05-29T16:00:00+00:00"
    assert data["run_id"] == "test-001"  # preserved
    assert data["branch"] == "pila/runs/test-001"  # preserved


def test_update_run_json_empty_value_clears_to_null(tmp_path: Path):
    """An empty-string value clears the field to null (used by resume to wipe paused_at)."""
    sidecar = tmp_path / "run.json"
    sidecar.write_text(json.dumps({"paused_at": "2026-05-29T16:00:00", "pause_reason": "x"}))
    result = _run_bash(
        f'source {LIB_SH}; update_run_json {sidecar} paused_at "" pause_reason ""',
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(sidecar.read_text())
    assert data["paused_at"] is None
    assert data["pause_reason"] is None


def test_update_run_json_atomic_via_temp_rename(tmp_path: Path):
    """After successful merge, no temp files remain in the sidecar's directory."""
    sidecar = tmp_path / "run.json"
    sidecar.write_text("{}")
    _run_bash(f"source {LIB_SH}; update_run_json {sidecar} key value")
    leftover = [p.name for p in tmp_path.iterdir() if p.name != "run.json"]
    assert leftover == [], f"temp files leaked: {leftover}"


def test_iso_now_returns_iso8601(tmp_path: Path):
    """iso_now emits an ISO-8601 UTC timestamp parseable by Python."""
    result = _run_bash(f"source {LIB_SH}; iso_now")
    assert result.returncode == 0
    import datetime
    parsed = datetime.datetime.fromisoformat(result.stdout.strip())
    assert parsed.tzinfo is not None


# --- provision.sh: stop_machine -------------------------------------------

def test_stop_machine_noop_when_no_machine_id(tmp_path: Path):
    """stop_machine returns 0 when PILA_MACHINE_ID is empty (idempotency)."""
    result = _run_bash(
        f"source {PROVISION_SH}; PILA_MACHINE_ID=''; stop_machine; echo ok",
    )
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_stop_machine_calls_flyctl_stop(tmp_path: Path):
    """stop_machine invokes flyctl machine stop with the machine id."""
    _make_flyctl_stub(tmp_path, behavior="happy")
    log = tmp_path / "flyctl.log"
    result = _run_bash(
        f"source {PROVISION_SH}; PILA_MACHINE_ID=mach-xyz; stop_machine",
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    invocations = log.read_text() if log.exists() else ""
    assert "machine stop mach-xyz" in invocations, invocations


# --- provision.sh: decide_teardown classification -------------------------

def _decide_teardown_with_rc(tmp_path: Path, rc: str, run_id: str = "test-run-001") -> tuple[subprocess.CompletedProcess, Path]:
    """Run decide_teardown with PILA_REMOTE_EXIT_RC=$rc.

    Sets up:
      - stub flyctl that records argv
      - USER_REPO with a .pila/runs/$run_id/run.json sidecar
      - PILA_MACHINE_ID=mach-test

    Returns (CompletedProcess, sidecar Path).
    """
    _make_flyctl_stub(tmp_path, behavior="happy")
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / run_id
    run_dir.mkdir(parents=True)
    sidecar = run_dir / "run.json"
    sidecar.write_text(json.dumps({"run_id": run_id, "branch": f"pila/runs/{run_id}"}))
    script = (
        f"source {PROVISION_SH}; "
        f"PILA_MACHINE_ID=mach-test; "
        f"decide_teardown"
    )
    result = _run_bash(
        script,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": run_id,
            "PILA_REMOTE_EXIT_RC": rc,
        },
    )
    return result, sidecar


def test_decide_teardown_rc0_destroys(tmp_path: Path):
    """rc=0 (success) → destroy_machine (full reap)."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "0")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine destroy mach-test" in invocations
    assert "machine stop mach-test" not in invocations
    data = json.loads(sidecar.read_text())
    assert data.get("paused_at") is None


def test_decide_teardown_rc10_destroys(tmp_path: Path):
    """rc=10 (EXIT_NEEDS_ANSWERS) → destroy."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "10")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine destroy mach-test" in invocations


def test_decide_teardown_rc75_destroys(tmp_path: Path):
    """rc=75 (EX_TEMPFAIL, rate-limit) → destroy."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "75")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine destroy mach-test" in invocations


def test_decide_teardown_rc130_destroys(tmp_path: Path):
    """rc=130 (SIGINT) → destroy."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "130")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine destroy mach-test" in invocations


def test_decide_teardown_rc143_destroys(tmp_path: Path):
    """rc=143 (SIGTERM) → destroy."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "143")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine destroy mach-test" in invocations


def test_decide_teardown_rc1_pauses(tmp_path: Path):
    """rc=1 (worker error) → stop (pause), write paused_at sidecar."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "1")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine stop mach-test" in invocations
    assert "machine destroy mach-test" not in invocations
    data = json.loads(sidecar.read_text())
    assert data["paused_at"] is not None
    assert data["fly_machine_id"] == "mach-test"
    assert data["pause_reason"] == "worker-error"
    assert "PAUSED: machine mach-test" in result.stderr


def test_decide_teardown_rc2_pauses(tmp_path: Path):
    """Any unknown non-zero rc → pause (the default safety mode)."""
    result, sidecar = _decide_teardown_with_rc(tmp_path, "2")
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine stop mach-test" in invocations


def test_decide_teardown_prints_resume_command(tmp_path: Path):
    """The pause notification includes the resume command verbatim."""
    result, _ = _decide_teardown_with_rc(tmp_path, "1", run_id="my-run-abc")
    assert "pila --resume --run-id my-run-abc --runtime fly" in result.stderr


def test_decide_teardown_pause_reason_overridable(tmp_path: Path):
    """PILA_PAUSE_REASON env var overrides the default 'worker-error' tag."""
    _make_flyctl_stub(tmp_path, behavior="happy")
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "test-001"
    run_dir.mkdir(parents=True)
    sidecar = run_dir / "run.json"
    sidecar.write_text(json.dumps({"run_id": "test-001"}))
    result = _run_bash(
        f"source {PROVISION_SH}; PILA_MACHINE_ID=mach-test; decide_teardown",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": "test-001",
            "PILA_REMOTE_EXIT_RC": "1",
            "PILA_PAUSE_REASON": "finalize-failed",
        },
    )
    assert result.returncode == 0
    data = json.loads(sidecar.read_text())
    assert data["pause_reason"] == "finalize-failed"


def test_decide_teardown_pause_notify_cmd_invoked(tmp_path: Path):
    """PILA_PAUSE_NOTIFY_CMD is eval'd on pause for outbound notifications."""
    _make_flyctl_stub(tmp_path, behavior="happy")
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "test-001"
    run_dir.mkdir(parents=True)
    sidecar = run_dir / "run.json"
    sidecar.write_text(json.dumps({"run_id": "test-001"}))
    notify_marker = tmp_path / "notify-fired"
    result = _run_bash(
        f"source {PROVISION_SH}; PILA_MACHINE_ID=mach-test; decide_teardown",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": "test-001",
            "PILA_REMOTE_EXIT_RC": "1",
            "PILA_PAUSE_NOTIFY_CMD": f"touch {notify_marker}",
        },
    )
    assert result.returncode == 0
    assert notify_marker.exists(), "PILA_PAUSE_NOTIFY_CMD was not eval'd on pause"


# --- resume-machine.sh ----------------------------------------------------

def test_resume_machine_requires_machine_id(tmp_path: Path):
    """resume_machine errors when no machine id is passed."""
    result = _run_bash(
        f"source {RESUME_SH}; resume_machine",
    )
    assert result.returncode != 0
    assert "machine id required" in result.stderr


def test_resume_machine_calls_start_and_clears_paused_at(tmp_path: Path):
    """resume_machine starts the machine, waits for started, clears paused_at."""
    _make_flyctl_stub(tmp_path, behavior="happy")
    user_repo = tmp_path / "user-repo"
    run_dir = user_repo / ".pila" / "runs" / "test-001"
    run_dir.mkdir(parents=True)
    sidecar = run_dir / "run.json"
    sidecar.write_text(json.dumps({
        "run_id": "test-001",
        "paused_at": "2026-05-29T16:00:00+00:00",
        "fly_machine_id": "mach-resumed",
        "pause_reason": "worker-error",
    }))
    # Source provision.sh first so wait_for_started is available.
    result = _run_bash(
        f"source {PROVISION_SH}; source {RESUME_SH}; resume_machine mach-resumed",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "USER_REPO": str(user_repo),
            "PILA_RUN_ID": "test-001",
        },
    )
    assert result.returncode == 0, result.stderr
    invocations = (tmp_path / "flyctl.log").read_text()
    assert "machine start mach-resumed" in invocations
    data = json.loads(sidecar.read_text())
    assert data["paused_at"] is None
    assert data["pause_reason"] is None
    # fly_machine_id is preserved — useful for post-resume inspection.
    assert data["fly_machine_id"] == "mach-resumed"


def test_resume_machine_refuses_destroyed_machine(tmp_path: Path):
    """resume_machine errors when the machine has been destroyed."""
    log = tmp_path / "flyctl.log"
    fake = tmp_path / "flyctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "case \"$1 $2\" in\n"
        "  'auth status') exit 0 ;;\n"
        "  'machine start') exit 1 ;;\n"
        "  'machine status') echo '{\"state\":\"destroyed\"}'; exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    result = _run_bash(
        f"source {PROVISION_SH}; source {RESUME_SH}; resume_machine mach-gone",
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )
    assert result.returncode == 1
    assert "destroyed" in result.stderr or "no longer recoverable" in result.stderr


# --- coupling: launcher pause-print includes the resume command ----------

def test_launcher_resume_command_format_matches_decide_teardown():
    """Coupling: the resume command printed by decide_teardown must match
    the shape consumed by the launcher's --resume + --run-id parsing.

    decide_teardown prints: pila --resume --run-id <id> --runtime fly
    The launcher parses --run-id from $@ (see PILA_RUN_ID extraction).
    Both halves must use the same flag shape.
    """
    launcher = (REPO_ROOT / "pila").read_text()
    provision = PROVISION_SH.read_text()
    assert "pila --resume --run-id $PILA_RUN_ID --runtime fly" in provision, (
        "decide_teardown's resume hint string drifted"
    )
    assert '--run-id' in launcher, "launcher no longer extracts --run-id"
