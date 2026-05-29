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


# --- Subprocess-tree termination (DESIGN §6 "Worker subtree termination") -
#
# Pin the discipline that satisfies the design contract: every subprocess
# spawn passes start_new_session=True (isolating the worker into its own
# POSIX session); every exception-cleanup path routes through
# _terminate_proc_tree (which combines killpg with a PPID walk to reach
# detached descendants); and `_invoke` wires a _DescendantTracker that
# observes the worker's descendants throughout its lifetime so they can
# be reaped even on a clean exit (Claude Code's run_in_background
# subprocesses outlive the worker and reparent to PID 1).

def test_every_subprocess_spawn_uses_start_new_session():
    """Static: every `asyncio.create_subprocess_exec` in pila.py must
    pass `start_new_session=True` so the worker is isolated into its own
    POSIX session. This is required so that on cleanup, `os.killpg(proc.pid)`
    does not accidentally signal the orchestrator's own process group."""
    src = PILA_PY.read_text()
    # Find every create_subprocess_exec(...) call. Match across lines
    # via DOTALL; bound on the closing `)` at the natural call indent.
    calls = re.findall(
        r"asyncio\.create_subprocess_exec\((.*?)\n    \)",
        src, re.DOTALL,
    )
    assert calls, ("expected at least one create_subprocess_exec call "
                   "in pila.py")
    for i, body in enumerate(calls):
        assert "start_new_session=True" in body, (
            f"create_subprocess_exec call #{i + 1} is missing "
            f"start_new_session=True. Without session isolation, "
            f"`os.killpg(proc.pid, ...)` in the cleanup path could "
            f"signal the orchestrator's own process group. "
            f"DESIGN §6 'Worker subtree termination on every exit'. "
            f"Call body:\n{body}"
        )


def test_no_bare_proc_kill_outside_terminate_proc_tree(pila):
    """Static: `proc.kill()` (which kills only the direct child PID)
    must not appear anywhere in pila.py. Every subprocess-cleanup path
    must instead route through `_terminate_proc_tree`, which combines
    `killpg` on the leader's group with a PPID walk to reach detached
    descendants (Claude Code's Bash tool runs in its own POSIX session,
    so `killpg(claude_p_pgid)` alone does not reach it).

    A regression that puts `proc.kill()` back into `run_proc` or
    `_invoke`'s exception handlers would silently re-leak the
    detached descendants. This test pins that against drift."""
    src = PILA_PY.read_text()
    # Locate _terminate_proc_tree's body so we can exclude it from
    # the scan (defensive — the current implementation doesn't call
    # proc.kill() either, but we don't want this test to lock the
    # helper's internal mechanism).
    helper_src = inspect.getsource(pila._terminate_proc_tree)
    src_outside_helper = src.replace(helper_src, "")
    matches = re.findall(r"\bproc\.kill\(\)", src_outside_helper)
    assert not matches, (
        f"found {len(matches)} bare proc.kill() call(s) outside "
        f"_terminate_proc_tree. Every subprocess cleanup path must "
        f"route through _terminate_proc_tree to reach descendants in "
        f"detached POSIX sessions (Claude Code's Bash tool spawns its "
        f"command in its own session, so `killpg` on the worker's "
        f"group does not reach it)."
    )


def test_run_proc_and_invoke_exception_handlers_call_terminate_proc_tree():
    """Static: both subprocess wrappers' `except` blocks must invoke
    `_terminate_proc_tree`. Source-pin to catch the case where someone
    refactors and accidentally drops one of the four handlers."""
    src = PILA_PY.read_text()
    # `run_proc`: from its def to the matching `return subprocess.CompletedProcess`
    m_run = re.search(
        r"async def run_proc\(.*?\n    return subprocess\.CompletedProcess",
        src, re.DOTALL,
    )
    assert m_run, "could not locate run_proc body in pila.py"
    run_proc_body = m_run.group(0)
    # `_invoke` is a top-level `async def`. Bound on the next top-level
    # def (also flush-left) so we don't bleed into _capture_call or
    # claude_p downstream.
    m_inv = re.search(
        r"\nasync def _invoke\(.*?\n(?=async def |def )",
        src, re.DOTALL,
    )
    assert m_inv, "could not locate _invoke body in pila.py"
    invoke_body = m_inv.group(0)

    for label, body in [("run_proc", run_proc_body), ("_invoke", invoke_body)]:
        # Each function must terminate the proc tree on TimeoutError
        # and on the catch-all BaseException. Count occurrences rather
        # than slicing nested blocks (which is brittle to inner
        # try/except inside _invoke's coroutines like _read_stream).
        # Both regexes allow non-await statements (e.g. a synchronous
        # watchdog_task.cancel()) between the `except` line and the
        # _terminate_proc_tree call — the invariant being pinned is
        # "the handler calls _terminate_proc_tree", not "_terminate is
        # the literal next line."
        timeout_present = re.search(
            r"except asyncio\.TimeoutError:.*?\n\s*await _terminate_proc_tree\(proc\)",
            body, re.DOTALL,
        )
        base_present = re.search(
            r"except BaseException:.*?\n\s*await _terminate_proc_tree\(proc\)",
            body, re.DOTALL,
        )
        assert timeout_present, (
            f"{label}'s `except asyncio.TimeoutError` handler must "
            f"`await _terminate_proc_tree(proc)`."
        )
        assert base_present, (
            f"{label}'s `except BaseException` handler must call "
            f"`await _terminate_proc_tree(proc)` to terminate the "
            f"worker's whole process group before re-raising."
        )


@pytest.mark.skipif(
    os.name == "nt",
    reason="start_new_session is a no-op on Windows; the POSIX "
           "process-group semantics this test exercises don't apply.",
)
def test_terminate_proc_tree_reaps_grandchildren(pila):
    """Behavioral: spawn a subprocess with start_new_session=True that
    itself launches a long-running grandchild, then call
    _terminate_proc_tree and assert the grandchild is gone.

    Static tests above pin the spelling (`start_new_session=True`,
    `_terminate_proc_tree` calls); this one pins the semantics — the
    actual property the DESIGN §6 contract promises."""
    import asyncio
    import time

    async def _run():
        # Parent shell: spawn a `sleep 60` in the background, print its
        # PID on stdout, then wait. When we kill the group, the sleep
        # must die too. `exec sleep` would replace the parent — we want
        # a *separate* grandchild PID to verify the group kill reaches
        # past the immediate child.
        script = (
            "sleep 60 & "
            "child=$!; "
            "echo $child; "
            # Hold the parent alive so the group exists when we signal
            # it; without this the parent exits after the background
            # spawn and the test races.
            "wait $child"
        )
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        # Read the grandchild PID off the parent's stdout.
        line = await proc.stdout.readline()
        grandchild_pid = int(line.strip())
        # Sanity: parent and grandchild are alive.
        assert _pid_alive(proc.pid), "parent died before test could run"
        assert _pid_alive(grandchild_pid), "grandchild never started"

        try:
            await pila._terminate_proc_tree(proc)
        finally:
            # Safety net: if the helper somehow didn't reap the
            # grandchild, do it ourselves so a failing test doesn't
            # leak a 60-second sleeper.
            if _pid_alive(grandchild_pid):
                try:
                    os.kill(grandchild_pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass

        # The parent must be reaped.
        assert proc.returncode is not None, "parent was not reaped"
        # The grandchild must be gone within a small window. The
        # helper's grace is _PROC_TREE_GRACE_SEC (2s) plus the
        # SIGKILL pass — give 3s total for the kernel to flush.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _pid_alive(grandchild_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(grandchild_pid), (
            f"grandchild PID {grandchild_pid} survived "
            f"_terminate_proc_tree — the process-group kill is not "
            f"reaching past the immediate child. This is the DESIGN "
            f"§6 'Worker subtree termination' contract failing."
        )

    asyncio.run(_run())


def _pid_alive(pid: int) -> bool:
    """True if a process with the given PID exists and we can signal
    it. `os.kill(pid, 0)` is the POSIX idiom — no signal is delivered;
    it only does the permission/existence check."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The PID exists but we don't own it — for our test's purposes
        # (it's a process we spawned ourselves) this should never
        # happen, but treat it as "alive" to avoid false negatives.
        return True


# --- PPID-walk + detached-session reaping ----------------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="PPID walk + start_new_session semantics are POSIX-only.",
)
def test_terminate_proc_tree_reaps_detached_session_grandchildren(pila):
    """Pila's worker (`claude -p`) spawns the Claude Code Bash tool via
    `spawn({detached: true})`, which puts the Bash tool subprocess into a
    NEW POSIX session — its PGID == its own PID, distinct from the
    worker's PGID. `os.killpg(worker_pgid)` does NOT reach it.

    The helper must instead walk the PPID chain (which stays intact while
    the parent lives) and signal every descendant by PID.

    This test exercises that exact shape: a "worker" Python process whose
    immediate child is in a *new session*, and the child has grandchildren.
    The helper must reach all the way down."""
    import asyncio
    import subprocess
    import time

    # Mimic Claude Code's spawn({detached: true}) by having the worker
    # spawn its child with start_new_session=True. Pila wouldn't use
    # this pattern for its own subprocesses, but `claude -p` does, and
    # `_terminate_proc_tree` must handle it.
    WORKER_PYTHON = (
        "import subprocess, time\n"
        "child = subprocess.Popen(\n"
        "    ['bash', '-c', 'sleep 47474 & sleep 47474 & wait'],\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    start_new_session=True,\n"
        ")\n"
        "time.sleep(300)\n"
    )

    async def _run():
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", WORKER_PYTHON,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for the detached bash + its sleeps to appear
        for _ in range(40):
            await asyncio.sleep(0.1)
            descs = pila._enumerate_descendants(proc.pid)
            if len(descs) >= 3:  # bash + 2 sleeps
                break
        else:
            try:
                proc.kill()
                await proc.wait()
            except BaseException:
                pass
            pytest.fail("worker never produced expected descendants")

        # Confirm the detached child has its own PGID
        detached_pids = [d for d in descs
                         if subprocess.run(
                             ["ps", "-p", str(d), "-o", "command="],
                             capture_output=True, text=True
                         ).stdout.strip().startswith("bash")]
        assert detached_pids, "no detached bash found among descendants"
        detached_pid = detached_pids[0]
        detached_pgid = int(subprocess.run(
            ["ps", "-p", str(detached_pid), "-o", "pgid="],
            capture_output=True, text=True
        ).stdout.strip())
        worker_pgid = proc.pid  # start_new_session=True ⇒ PGID == PID
        assert detached_pgid != worker_pgid, (
            "test setup invalid: detached child is in the same PGID as "
            "the worker. This test must exercise the detached-session "
            "case, which requires the child to be in a NEW session."
        )

        sleep_descs = [d for d in descs if subprocess.run(
            ["ps", "-p", str(d), "-o", "command="],
            capture_output=True, text=True
        ).stdout.strip().startswith("sleep 47474")]
        assert len(sleep_descs) == 2, f"expected 2 sleeps, got {len(sleep_descs)}"

        # Run the fix. All descendants must die — including the ones in
        # the detached session that killpg(worker_pgid) cannot reach.
        try:
            await pila._terminate_proc_tree(proc)
        finally:
            # Safety net so a broken helper doesn't leak a 5-minute sleep
            for d in descs:
                try: os.kill(d, _signal.SIGKILL)
                except ProcessLookupError: pass

        # All sleeps must be gone
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            alive = [d for d in sleep_descs if _pid_alive(d)]
            if not alive:
                break
            await asyncio.sleep(0.05)
        survivors = [d for d in sleep_descs if _pid_alive(d)]
        assert not survivors, (
            f"detached-session sleeps survived _terminate_proc_tree: "
            f"{survivors}. The PPID-walk did not reach descendants whose "
            f"PGID differs from the worker's. Claude Code's Bash tool "
            f"runs in its own session, so `os.killpg(worker_pgid)` "
            f"alone cannot reach it — the cleanup helper must combine "
            f"killpg with a PPID-walk."
        )

    asyncio.run(_run())


# --- Success-path cleanup via _DescendantTracker ---------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only test.",
)
def test_descendant_tracker_reaps_orphaned_backgrounded_subprocess(pila):
    """Even on a clean leader exit, Claude Code's `run_in_background:
    true` Bash tool calls leak — the backgrounded subprocesses are spawned
    in detached POSIX sessions and reparent to PID 1 the moment their
    immediate parent exits.

    A naive post-exit PPID-walk finds nothing (the orphans are no longer
    descendants of the dead leader). `_DescendantTracker` solves this by
    polling `_enumerate_descendants` THROUGHOUT the leader's life and
    accumulating every PID it ever sees. At exit, the accumulated set
    is SIGKILLed — catching the now-orphaned children."""
    import asyncio
    import subprocess

    async def _run():
        # Leader backgrounds a sleep then waits briefly so the tracker has
        # at least one poll cycle to observe the sleep before the leader
        # exits.
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            "sleep 38383 < /dev/null > /dev/null 2>&1 & "
            "echo $! ; "
            "sleep 1",  # keep parent alive 1s so tracker's 0.5s poll catches the sleep
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        tracker = pila._DescendantTracker(proc.pid)
        tracker.start()
        # Read the sleep PID off stdout
        line = await proc.stdout.readline()
        sleep_pid = int(line.strip())
        # Let the leader exit cleanly
        await proc.wait()
        # At this point sleep_pid should be orphaned (PPID=1) but still
        # alive. The tracker should have observed it during its ~0.5s
        # poll while the parent was alive.
        await asyncio.sleep(0.1)  # let kernel reparent
        # stop_and_reap must kill the orphaned sleep
        leaked = await tracker.stop_and_reap()
        assert leaked >= 1, (
            f"tracker reaped {leaked} descendants — expected at least 1 "
            f"(the backgrounded sleep that became an orphan when its "
            f"parent exited). The tracker did not observe the sleep "
            f"during its polling window."
        )
        # Verify the sleep actually died
        await asyncio.sleep(0.2)
        assert not _pid_alive(sleep_pid), (
            f"sleep PID {sleep_pid} survived tracker.stop_and_reap. The "
            f"tracker recorded the PID but SIGKILL didn't deliver — "
            f"likely a permission or signal-delivery bug."
        )

    try:
        asyncio.run(_run())
    finally:
        # Safety net
        subprocess.run(["pkill", "-9", "-f", "sleep 38383"], capture_output=True)


# --- Module-level helper unit tests ----------------------------------------

@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only test.",
)
def test_enumerate_descendants_returns_indirect_children(pila):
    """`_enumerate_descendants(root_pid)` must walk transitively, not just
    list direct children. Spawn a 3-deep chain and assert all 3 are
    found."""
    import subprocess
    # outer bash → (sub-bash backgrounded with &) → sleep
    # The outer bash MUST keep running (its trailing `wait` is what holds it
    # alive) so the PPID chain stays intact while we measure. Without the
    # `& wait` shape, outer bash would exec into the sub-bash via tail-call
    # optimization and the chain would only be 2-deep.
    leader = subprocess.Popen(
        ["bash", "-c", "bash -c 'sleep 28282 & wait' & wait"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    import time
    time.sleep(1.5)
    try:
        descs = pila._enumerate_descendants(leader.pid)
        # Should find: inner bash (level 1), sleep (level 2)
        assert len(descs) >= 2, (
            f"_enumerate_descendants found only {len(descs)} descendants "
            f"(expected at least 2 — inner bash + sleep). Walk is not "
            f"transitive."
        )
    finally:
        leader.kill()
        subprocess.run(["pkill", "-9", "-f", "sleep 28282"], capture_output=True)
        leader.wait()


def test_enumerate_descendants_returns_empty_for_nonexistent_pid(pila):
    """Sanity: a sentinel PID with no children returns empty set."""
    assert pila._enumerate_descendants(999_999_999) == set()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only test.",
)
def test_descendant_tracker_is_safe_on_nonexistent_pid(pila):
    """`_DescendantTracker(sentinel_pid).stop_and_reap()` must not raise —
    used in `_invoke`'s success path and must be idempotent even when the
    leader has no descendants at all."""
    import asyncio

    async def _run():
        tracker = pila._DescendantTracker(999_999_999)
        tracker.start()
        await asyncio.sleep(0.1)  # one poll cycle
        leaked = await tracker.stop_and_reap()
        assert leaked == 0
        # Idempotent
        leaked2 = await tracker.stop_and_reap()
        assert leaked2 == 0

    asyncio.run(_run())


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only test.",
)
def test_descendant_tracker_records_descendants_during_lifetime(pila):
    """Verify the tracker actually accumulates PIDs across multiple poll
    cycles, not just at start or stop. This guards against a regression
    where the poll loop is broken (e.g. caught CancelledError too eagerly)
    and only sees the descendant set at one moment."""
    import asyncio
    import subprocess

    async def _run():
        leader = await asyncio.create_subprocess_exec(
            "bash", "-c", "sleep 18181 & wait",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        tracker = pila._DescendantTracker(leader.pid)
        tracker.start()
        # Wait for at least 2 poll cycles to catch the sleep
        await asyncio.sleep(1.5)
        # Snapshot what tracker has accumulated
        accumulated = set(tracker._seen)
        # Clean up
        leader.kill()
        await leader.wait()
        leaked = await tracker.stop_and_reap()
        subprocess.run(["pkill", "-9", "-f", "sleep 18181"], capture_output=True)

        assert len(accumulated) >= 1, (
            f"tracker accumulated 0 descendants during its polling lifetime "
            f"(expected at least 1 — the sleep was alive for 1.5s and the "
            f"poll interval is 0.5s)"
        )

    asyncio.run(_run())
