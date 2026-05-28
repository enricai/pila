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
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


# --- InterruptedBySignal --------------------------------------------------

def test_interrupted_by_signal_is_base_exception(pila):
    """Must subclass BaseException (not Exception) so the broad
    `except Exception` handlers inside orchestrate() don't swallow it."""
    assert issubclass(pila.InterruptedBySignal, BaseException)
    assert not issubclass(pila.InterruptedBySignal, Exception)


# --- _install_signal_handlers --------------------------------------------

def test_install_signal_handlers_registers_sigterm(pila, monkeypatch):
    """SIGTERM gets a custom handler installed."""
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(pila.signal, "signal", fake_signal)
    pila._install_signal_handlers()
    assert _signal.SIGTERM in installed


def test_install_signal_handlers_registers_sighup_on_posix(pila, monkeypatch):
    """SIGHUP gets a handler too, when available."""
    if not hasattr(_signal, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform")
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(pila.signal, "signal", fake_signal)
    pila._install_signal_handlers()
    assert _signal.SIGHUP in installed


def test_install_signal_handlers_does_not_touch_sigint(pila, monkeypatch):
    """SIGINT must keep Python's default (KeyboardInterrupt) — not
    intercepted by InterruptedBySignal. main() handles KeyboardInterrupt
    separately for the full-purge path."""
    installed: dict = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
    monkeypatch.setattr(pila.signal, "signal", fake_signal)
    pila._install_signal_handlers()
    assert _signal.SIGINT not in installed


def test_signal_handler_raises_interrupted_by_signal(pila, monkeypatch):
    """When the installed SIGTERM handler is invoked, it raises
    InterruptedBySignal — that's what bubbles up to main()."""
    handlers: dict = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler
    monkeypatch.setattr(pila.signal, "signal", fake_signal)
    pila._install_signal_handlers()
    handler = handlers[_signal.SIGTERM]
    with pytest.raises(pila.InterruptedBySignal):
        handler(_signal.SIGTERM, None)


# --- _cleanup_on_abnormal_exit -------------------------------------------

class _FakeState:
    """Minimal State stand-in: only `run_id` and `run_dir` are read by
    `_cleanup_on_abnormal_exit`."""
    def __init__(self, run_id: str, run_dir: Path):
        self.run_id = run_id
        self.run_dir = run_dir


def test_cleanup_handles_none_state_gracefully(pila):
    """Defensive: cleanup early-returns on a None state rather than
    raising. Used when main() bails before constructing State."""
    pila._cleanup_on_abnormal_exit(None, full_purge=False)  # must not raise


def test_cleanup_removes_worktrees_dir(pila, tmp_path, monkeypatch):
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
    monkeypatch.setattr(pila.subprocess, "run", fake_run)

    pila._cleanup_on_abnormal_exit(st, full_purge=False)

    # Two worktree-remove calls + one prune.
    remove_calls = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
    assert len(remove_calls) == 2
    assert any(c for c in calls if c == ["git", "worktree", "prune"])


def test_cleanup_full_purge_deletes_run_dir(pila, tmp_path, monkeypatch):
    """With full_purge=True, the run_dir is removed via shutil.rmtree."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    st = _FakeState(run_id, run_dir)

    monkeypatch.setattr(pila.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""))

    assert run_dir.exists()
    pila._cleanup_on_abnormal_exit(st, full_purge=True)
    assert not run_dir.exists(), (
        "full_purge=True must remove the run_dir entirely"
    )


def test_cleanup_rm_rf_fallback_when_git_leaves_dir(pila, tmp_path,
                                                    monkeypatch):
    """When `git worktree remove` returns nonzero (or zero) but does NOT
    actually delete the directory — e.g. git already pruned the worktree
    from its registry on a previous pass — the cleanup must fall back to
    rm -rf so the surviving directory doesn't block --resume's
    new-worktree.sh from re-creating the worktree at the same path.

    Observed in finalmemoriam on 2026-05-28: an overnight run timed out
    on node_modules under the old 30s cap, cleanup logged a failure,
    git later pruned its registry, and the surviving worktree dir
    blocked --resume the next morning with
    `fatal: '...' already exists`."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    wt_a = run_dir / "worktrees" / "feat-001"
    wt_a.mkdir(parents=True)
    # Put something in the worktree (simulates leftover node_modules).
    (wt_a / "leftover.txt").write_text("stale")
    st = _FakeState(run_id, run_dir)

    # Simulate git's behavior in the failure scenario: subprocess.run
    # succeeds (no exception) but git does nothing on disk (returns
    # nonzero because the worktree isn't tracked).
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: not a worktree")
    monkeypatch.setattr(pila.subprocess, "run", fake_run)

    assert wt_a.exists()
    pila._cleanup_on_abnormal_exit(st, full_purge=False)
    assert not wt_a.exists(), (
        "cleanup must rm -rf the worktree dir when git worktree remove "
        "leaves it behind, otherwise --resume's new-worktree.sh will "
        "fail with 'already exists' when it tries to re-create the "
        "worktree at the same path."
    )


def test_cleanup_rm_rf_fallback_after_timeout(pila, tmp_path, monkeypatch):
    """Mirror of the above for the timeout case: subprocess.TimeoutExpired
    is raised mid-removal, but the directory survives (with partial
    contents). Cleanup must still fall back to rm -rf so the surviving
    dir doesn't block --resume."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    wt_a = run_dir / "worktrees" / "feat-001"
    wt_a.mkdir(parents=True)
    (wt_a / "leftover.txt").write_text("stale")
    st = _FakeState(run_id, run_dir)

    def fake_run(cmd, **kwargs):
        # Only timeout for the worktree-remove call; let prune succeed.
        if cmd[:3] == ["git", "worktree", "remove"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(pila.subprocess, "run", fake_run)

    pila._cleanup_on_abnormal_exit(st, full_purge=False)
    assert not wt_a.exists(), (
        "cleanup must rm -rf after a TimeoutExpired so the surviving "
        "dir doesn't persist across runs."
    )


def test_cleanup_rm_rf_skips_when_path_escapes_sandbox(pila, tmp_path,
                                                      monkeypatch):
    """Belt-and-suspenders: the rm -rf fallback must verify the
    resolved path lies within the worktrees dir before deleting. If a
    refactor or symlink ever caused entry.resolve().parent to escape
    the sandbox, the rm would be a no-op rather than a destructive
    misfire."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    worktrees_dir = run_dir / "worktrees"
    worktrees_dir.mkdir(parents=True)
    # Create a real file outside the sandbox.
    outside = tmp_path / "outside_target"
    outside.mkdir()
    (outside / "important.txt").write_text("do not delete")
    # Symlink from inside the worktrees dir to the outside path.
    sym = worktrees_dir / "feat-001"
    sym.symlink_to(outside)
    st = _FakeState(run_id, run_dir)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "")
    monkeypatch.setattr(pila.subprocess, "run", fake_run)

    pila._cleanup_on_abnormal_exit(st, full_purge=False)
    # The symlink itself may or may not survive (resolve depends on
    # what counts as a directory iteration), but the OUTSIDE target
    # must survive — that's the load-bearing invariant.
    assert outside.exists()
    assert (outside / "important.txt").exists()
    assert (outside / "important.txt").read_text() == "do not delete"


def test_cleanup_no_purge_preserves_run_dir(pila, tmp_path, monkeypatch):
    """full_purge=False leaves the run_dir intact (worktrees may be
    removed, but state.json and the dir itself survive)."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "worktrees").mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    st = _FakeState(run_id, run_dir)

    monkeypatch.setattr(pila.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "", ""))

    pila._cleanup_on_abnormal_exit(st, full_purge=False)
    assert run_dir.exists(), "full_purge=False must preserve the run_dir"
    assert (run_dir / "state.json").exists(), "state.json must survive non-purge cleanup"


def test_cleanup_full_purge_deletes_branches(pila, tmp_path, monkeypatch):
    """full_purge=True invokes `git for-each-ref` to enumerate branches
    and `git branch -D` to delete each one."""
    run_id = "feat-x-aaa111"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    st = _FakeState(run_id, run_dir)

    branches_to_delete = [
        f"pila/runs/{run_id}",
        f"pila/subtasks/{run_id}/feat-001",
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "for-each-ref"]:
            # The cleanup walks two globs: refs/heads/pila/runs/<id>
            # (the run branch, exact match) and refs/heads/pila/subtasks/<id>/
            # (the subtask-branch prefix). Distinguish by the runs/ vs subtasks/
            # segment so each glob returns the matching branch.
            glob = cmd[3]
            if glob == f"refs/heads/pila/runs/{run_id}":
                return subprocess.CompletedProcess(cmd, 0, f"pila/runs/{run_id}\n", "")
            if glob == f"refs/heads/pila/subtasks/{run_id}/":
                return subprocess.CompletedProcess(cmd, 0, f"pila/subtasks/{run_id}/feat-001\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(pila.subprocess, "run", fake_run)

    pila._cleanup_on_abnormal_exit(st, full_purge=True)

    delete_calls = [c for c in calls if c[:3] == ["git", "branch", "-D"]]
    assert len(delete_calls) == 2, f"expected 2 branch deletes, got {delete_calls}"


# --- main() try/except/finally pinning -----------------------------------

def _main_body() -> str:
    """Extract main()'s body from pila.py source."""
    src = PILA_PY.read_text()
    m = re.search(
        r"^def main\(\) -> None:\n(.*?)(?=^(?:def |class |if __name__))",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m
    return m.group(1)


def test_main_calls_install_signal_handlers():
    body = _main_body()
    assert "_install_signal_handlers()" in body


def test_main_keyboard_interrupt_no_full_purge():
    """SIGINT (KeyboardInterrupt) → full_purge=False. Pin the per-exception
    flag selection so a refactor can't silently regress Ctrl-C from
    'preserve and resume' back to the old 'throw it away' behavior
    (DESIGN §6 *Cleanup on abnormal exit*: every abnormal exit
    preserves state and branches; only worktrees are torn down).

    Anchor on outer-try indentation (4 spaces) so an inner
    `except KeyboardInterrupt` nested under a deeper indent — e.g.
    the RateLimitedExit arm's sleep-interrupt guard — doesn't shadow
    the outer clause."""
    body = _main_body()
    # Find the OUTER except KeyboardInterrupt block (4-space indent).
    m = re.search(
        r"\n    except KeyboardInterrupt:(.*?)(?=^\s*except |^\s*finally:)",
        body, re.DOTALL | re.MULTILINE,
    )
    assert m, ("could not locate outer except KeyboardInterrupt block "
               "in main() at the 4-space indent")
    block = m.group(1)
    assert "full_purge = False" in block
    assert "full_purge = True" not in block


def test_main_rate_limit_sleep_catches_keyboard_interrupt():
    """Ctrl-C during the auto-resume sleep must produce the friendly
    'state preserved' log message, not a silent exit. The outer
    KeyboardInterrupt arm of main() is reached *outside* the
    RateLimitedExit arm — when the user Ctrl-C's while we're inside
    `time.sleep` within the RateLimitedExit arm, the KeyboardInterrupt
    would escape to Python's default handler without our friendly
    message unless it's caught locally. Pin that the local catch
    exists."""
    body = _main_body()
    # Find the OUTER except RateLimitedExit block (4-space indent).
    # The lookahead anchors on the same outer-try indent to avoid
    # truncating at inner `except BaseException` clauses nested inside
    # the arm (e.g. the cleanup-failure guard).
    m = re.search(
        r"\n    except RateLimitedExit[^:]*:(.*?)(?=\n    except |\n    finally:)",
        body, re.DOTALL,
    )
    assert m, ("could not locate outer except RateLimitedExit block in "
               "main() at the 4-space indent")
    block = m.group(1)
    # The block must contain a local KeyboardInterrupt catch wrapping
    # time.sleep — otherwise Ctrl-C during the wait silently kills the
    # process without the user-facing "state preserved" message.
    assert "time.sleep" in block
    assert "except KeyboardInterrupt" in block, (
        "RateLimitedExit arm must locally catch KeyboardInterrupt "
        "around time.sleep so Ctrl-C during the auto-resume wait "
        "produces the standard 'state preserved' message rather than "
        "a silent exit."
    )


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
    # catch-all `except BaseException` block. Anchor on the outer
    # try-block indentation (4 spaces) so an inner `except BaseException`
    # nested under a deeper indent — e.g. the RateLimitedExit arm's
    # cleanup-failure guard — doesn't shadow the outer clause.
    sysexit_pos = body.find("\n    except SystemExit")
    base_pos = body.find("\n    except BaseException")
    assert sysexit_pos != -1, (
        "main() must explicitly catch SystemExit so die() calls aren't "
        "mistakenly logged as unhandled exceptions"
    )
    assert base_pos != -1, (
        "main() must have a catch-all except BaseException clause at "
        "the outer try-block indent"
    )
    assert sysexit_pos < base_pos, (
        "except SystemExit must appear BEFORE except BaseException — "
        "otherwise the catch-all matches first (BaseException is the "
        "superclass) and SystemExit gets the unhandled-exception path"
    )
