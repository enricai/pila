"""Tests for OS-signal cleanup (DESIGN §6 / DESIGN §14).

Coverage:
- `_install_signal_handlers` registers handlers for SIGTERM (and SIGHUP
  on POSIX) without disturbing SIGINT (which keeps Python's default).
- The handler raises `InterruptedBySignal`.
- `_cleanup_on_abnormal_exit` removes worktrees; with `full_purge=True`
  it also removes the run dir.
- Source-text pins on main()'s try/except/finally structure ensure the
  per-exception `full_purge` flag selection is preserved across refactors.
"""
from __future__ import annotations

import inspect
import os
import re
import signal as _signal
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CENTELLA_PY = REPO_ROOT / "orchestrator" / "centella.py"


# --- InterruptedBySignal --------------------------------------------------

def test_interrupted_by_signal_is_base_exception(centella):
    """Must subclass BaseException (not Exception) so the broad
    `except Exception` handlers inside orchestrate() don't swallow it."""
    assert issubclass(centella.InterruptedBySignal, BaseException)
    assert not issubclass(centella.InterruptedBySignal, Exception)


# --- _install_signal_handlers --------------------------------------------

def test_install_signal_handlers_registers_sigterm(centella, monkeypatch):
    """SIGTERM gets a custom handler installed."""
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(centella.signal, "signal", fake_signal)
    centella._install_signal_handlers()
    assert _signal.SIGTERM in installed


def test_install_signal_handlers_registers_sighup_on_posix(centella, monkeypatch):
    """SIGHUP gets a handler too, when available."""
    if not hasattr(_signal, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform")
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(centella.signal, "signal", fake_signal)
    centella._install_signal_handlers()
    assert _signal.SIGHUP in installed


def test_install_signal_handlers_does_not_touch_sigint(centella, monkeypatch):
    """SIGINT must keep Python's default (KeyboardInterrupt) — not
    intercepted by InterruptedBySignal. main() handles KeyboardInterrupt
    separately for the full-purge path."""
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(centella.signal, "signal", fake_signal)
    centella._install_signal_handlers()
    assert _signal.SIGINT not in installed


def test_signal_handler_raises_interrupted_by_signal(centella, monkeypatch):
    """When the installed SIGTERM handler is invoked, it raises
    InterruptedBySignal — that's what bubbles up to main()."""
    handlers: dict = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler
    monkeypatch.setattr(centella.signal, "signal", fake_signal)
    centella._install_signal_handlers()
    handler = handlers[_signal.SIGTERM]
    with pytest.raises(centella.InterruptedBySignal):
        handler(_signal.SIGTERM, None)


# --- _cleanup_on_abnormal_exit -------------------------------------------

class _FakeState:
    """Minimal State stand-in: only `run_id` and `run_dir` are read by
    `_cleanup_on_abnormal_exit`."""
    def __init__(self, run_id: str, run_dir: Path):
        self.run_id = run_id
        self.run_dir = run_dir


def test_cleanup_handles_none_state_gracefully(centella):
    """Defensive: cleanup early-returns on a None state rather than
    raising. Used when main() bails before constructing State."""
    centella._cleanup_on_abnormal_exit(None, full_purge=False)  # must not raise


def test_cleanup_removes_worktrees_dir(centella, tmp_path, monkeypatch):
    """_cleanup_on_abnormal_exit calls `git worktree remove --force` for
    each subdir of run_dir/worktrees/. Test by stubbing subprocess.run
    and confirming the calls."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "worktrees" / "staging").mkdir(parents=True)
    (run_dir / "worktrees" / "feat-001").mkdir(parents=True)
    st = _FakeState(run_id, run_dir)

    calls: list[list[str]] = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(centella.subprocess, "run", fake_run)

    centella._cleanup_on_abnormal_exit(st, full_purge=False)

    # Two worktree-remove calls + one prune.
    remove_calls = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert len(remove_calls) == 2
    assert any(c for c in calls if c == ["git", "worktree", "prune"])


def test_cleanup_full_purge_deletes_run_dir(centella, tmp_path, monkeypatch):
    """With full_purge=True, the run_dir is removed via shutil.rmtree."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    st = _FakeState(run_id, run_dir)

    monkeypatch.setattr(centella.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""))

    assert run_dir.exists()
    centella._cleanup_on_abnormal_exit(st, full_purge=True)
    assert not run_dir.exists(), (
        "full_purge=True must remove the run_dir entirely"
    )


def test_cleanup_no_purge_preserves_run_dir(centella, tmp_path, monkeypatch):
    """full_purge=False leaves the run_dir intact (worktrees may be
    removed, but state.json and the dir itself survive)."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    st = _FakeState(run_id, run_dir)

    monkeypatch.setattr(centella.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""))

    centella._cleanup_on_abnormal_exit(st, full_purge=False)
    assert run_dir.exists(), "full_purge=False must preserve the run_dir"
    assert (run_dir / "state.json").exists(), "state.json must survive non-purge cleanup"


def test_cleanup_full_purge_deletes_branches(centella, tmp_path, monkeypatch):
    """full_purge=True invokes `git for-each-ref` to enumerate branches
    and `git branch -D` to delete each one."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    st = _FakeState(run_id, run_dir)

    branches_to_delete = [
        f"centella/{run_id}",
        f"centella/{run_id}/feat-001",
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "for-each-ref"]:
            # Return all branches on the first matching call; empty on others.
            if cmd[3].startswith(f"refs/heads/centella/{run_id}") and not cmd[3].endswith("/"):
                return subprocess.CompletedProcess(cmd, 0, f"centella/{run_id}\n", "")
            return subprocess.CompletedProcess(cmd, 0, f"centella/{run_id}/feat-001\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(centella.subprocess, "run", fake_run)

    centella._cleanup_on_abnormal_exit(st, full_purge=True)

    delete_calls = [c for c in calls if c[:3] == ["git", "branch", "-D"]]
    assert len(delete_calls) == 2, f"expected 2 branch deletes, got {delete_calls}"


# --- main() try/except/finally pinning -----------------------------------

def _main_body() -> str:
    """Extract main()'s body from centella.py source."""
    src = CENTELLA_PY.read_text()
    m = re.search(
        r"^def main\(\) -> None:\n(.*?)(?=^(?:def |class |if __name__))",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m
    return m.group(1)


def test_main_calls_install_signal_handlers():
    body = _main_body()
    assert "_install_signal_handlers()" in body


def test_main_keyboard_interrupt_full_purge():
    """SIGINT (KeyboardInterrupt) → full_purge=True. Pin the per-exception
    flag selection so a refactor can't silently demote Ctrl-C from
    'throw it away' to 'preserve and resume'."""
    body = _main_body()
    # Find the except KeyboardInterrupt block.
    m = re.search(
        r"except KeyboardInterrupt:(.*?)(?=^\s*except |^\s*finally:)",
        body, re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate except KeyboardInterrupt block in main()"
    block = m.group(1)
    assert "full_purge = True" in block


def test_main_interrupted_by_signal_no_full_purge():
    """SIGTERM/SIGHUP → full_purge=False (preserve for resume)."""
    body = _main_body()
    m = re.search(
        r"except InterruptedBySignal[^:]*:(.*?)(?=^\s*except |^\s*finally:)",
        body, re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate except InterruptedBySignal block in main()"
    block = m.group(1)
    assert "full_purge = False" in block


def test_main_worker_error_no_full_purge():
    """WorkerError → preserve for resume (user can fix the issue and continue)."""
    body = _main_body()
    m = re.search(
        r"except WorkerError[^:]*:(.*?)(?=^\s*except |^\s*finally:)",
        body, re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate except WorkerError block in main()"
    block = m.group(1)
    assert "full_purge = False" in block


def test_main_finally_calls_cleanup():
    body = _main_body()
    assert "_cleanup_on_abnormal_exit(st, full_purge=full_purge)" in body


def test_main_system_exit_not_treated_as_unhandled():
    """`die()` raises SystemExit. main() must catch it explicitly (before
    the catch-all `except BaseException`) and re-raise without logging
    'unhandled exception' — die() is the *clean* exit mechanism and the
    user already got the right error message."""
    body = _main_body()
    # Look for an `except SystemExit` block that appears before the
    # catch-all `except BaseException` block.
    sysexit_pos = body.find("except SystemExit")
    base_pos = body.find("except BaseException")
    assert sysexit_pos != -1, (
        "main() must explicitly catch SystemExit so die() calls aren't "
        "mistakenly logged as unhandled exceptions"
    )
    assert sysexit_pos < base_pos, (
        "except SystemExit must appear BEFORE except BaseException — "
        "otherwise the catch-all matches first (BaseException is the "
        "superclass) and SystemExit gets the unhandled-exception path"
    )
