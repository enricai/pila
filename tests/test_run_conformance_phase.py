"""Tests for the orchestrator-level conformance phase loop —
_run_conformance_phase() in pila.py (DESIGN §9 *Post-work
conformance*).

The phase is advisory: it never raises and never returns a status that
fails the subtask. All failure modes (malformed conformer output,
WorkerError, protected-path violations on conformer commits,
criteria-lock mismatch, exhausted rounds) surface as `warnings` entries.

The tests stub `run_conformer` with a queue of canned results and use a
real git worktree on disk so the criteria-lock and check_diff_scope
re-runs against the conformer's commits exercise the real code paths.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest


# --- shared fixtures -------------------------------------------------------

def _run(cmd, cwd, check=True):
    """Run a git command, asserting success unless check=False."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"{cmd} failed in {cwd}: {r.stderr}"
    return r


@pytest.fixture
def env(pila, tmp_path):
    """A real git repo with a .pila run dir, one subtask worktree
    branched off a 'run branch', and the criteria locked.

    Returns a dict of every path / object the phase needs to run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "t@t"], cwd=repo)
    _run(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "README.md").write_text("# repo\n")
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "initial"], cwd=repo)

    # The "run branch" the implementer branched off of.
    run_id = "fix-001-abcdef"
    run_branch = f"pila/runs/{run_id}"
    _run(["git", "checkout", "-q", "-b", run_branch], cwd=repo)

    # Set up .pila coordination state first so the subtask worktree
    # can live under run_dir/worktrees/<sid> — the canonical location
    # settle_subtask uses (`worktree = str(pila_dir / "worktrees" / sid)`).
    sid = "t1"
    subtask_branch = f"pila/subtasks/{run_id}/{sid}"
    pila_root = repo / ".pila"
    run_dir = pila_root / "runs" / run_id
    (run_dir / "subtasks").mkdir(parents=True)
    (run_dir / "criteria").mkdir()
    (run_dir / "logs").mkdir()
    (run_dir / "worktrees").mkdir()
    worktree = run_dir / "worktrees" / sid
    _run(["git", "worktree", "add", "-q", "-b", subtask_branch,
          str(worktree), run_branch], cwd=repo)

    # Simulate an implementer commit on the subtask branch.
    (worktree / "src.py").write_text("def f():\n    pass\n")
    _run(["git", "add", "-A"], cwd=worktree)
    _run(["git", "commit", "-q", "-m", "implementer: add f()"], cwd=worktree)

    (run_dir / "criteria" / f"{sid}.md").write_text(
        "# Criteria\n- f() returns None\n")
    subtask = {"id": sid, "files_likely_touched": ["src.py"]}
    (run_dir / "subtasks" / f"{sid}.json").write_text(json.dumps(subtask))

    State = pila.State
    st = State(pila_root, run_id)
    st.data = {"task": "x", "answers": {"source_of_truth": "codebase"}}
    st.save()

    caps = dict(pila.DEFAULT_CAPS)
    models = {w: "sonnet" for w in pila.WORKER_TYPES}

    return {
        "pila": pila, "repo": repo, "worktree": worktree,
        "sid": sid, "subtask": subtask, "run_dir": run_dir, "st": st,
        "caps": caps, "models": models, "run_branch": run_branch,
    }


def _stub_run_conformer(pila_mod, results_queue, *, commits=None):
    """Patch pila.run_conformer to return queued results in order. If
    `commits` is provided, the matching index's stub also writes a file
    and commits it to the worktree before returning."""
    commits = commits or {}
    state = {"i": 0}

    async def _stub(sid, pila_dir, worktree, caps, st, models,
                    *, rules_files, blt_commands, diff_base):
        i = state["i"]
        state["i"] += 1
        action = commits.get(i)
        if action is not None:
            action(Path(worktree))
        return results_queue[i] if i < len(results_queue) else None

    pila_mod.run_conformer = _stub
    return state


def _clean_result(sid="t1", **overrides):
    """A conformer result that is well-formed and clean (no residuals,
    no failing build/lint/tests)."""
    base = {
        "subtask_id": sid,
        "rules_files_read": [],
        "rule_violations_fixed": [],
        "rule_violations_residual": [],
        "docs_updates": [],
        "tests_updates": [],
        "build": {"ran": False, "passed": False, "command": "", "summary": ""},
        "lint": {"ran": False, "passed": False, "command": "", "summary": ""},
        "tests": {"ran": False, "passed": False, "command": "", "summary": ""},
        "summary": "nothing to do",
    }
    base.update(overrides)
    return base


# --- happy path: clean result, no warnings, single round -------------------

def test_clean_result_exits_after_one_round(env, monkeypatch):
    c = env["pila"]
    state = _stub_run_conformer(c, [_clean_result()])

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    assert state["i"] == 1
    assert res is not None
    assert warnings == []


# --- malformed output: surfaced as warning, loop breaks --------------------

def test_malformed_result_breaks_loop_with_warning(env):
    c = env["pila"]
    # residual without files_read — cross-field invariant violation.
    bad = _clean_result(rule_violations_residual=[{"rule": "x",
                                                   "why_not_fixed": "y"}])
    state = _stub_run_conformer(c, [bad])

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    assert state["i"] == 1  # loop did not retry on malformed output
    assert res == bad
    assert any("malformed" in w for w in warnings)


# --- crash (None): surfaced as warning, loop breaks -----------------------

def test_worker_crash_surfaces_as_warning(env):
    c = env["pila"]
    state = _stub_run_conformer(c, [None])

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    assert state["i"] == 1
    assert res is None
    assert any("crashed" in w for w in warnings)


# --- protected path: conformer commits get rolled back --------------------

def test_protected_path_commit_is_rolled_back(env):
    c = env["pila"]

    def _bad_commit(wt: Path):
        """Simulate a conformer that wrote to .claude/ — a protected path."""
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "x").write_text("bad\n")
        _run(["git", "add", "-A"], cwd=wt)
        _run(["git", "commit", "-q", "-m",
              "conformer: BAD touched protected path"], cwd=wt)

    state = _stub_run_conformer(c, [_clean_result()], commits={0: _bad_commit})
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env["worktree"],
        capture_output=True, text=True).stdout.strip()

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    # The protected-path commit must be gone from HEAD after rollback.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env["worktree"],
        capture_output=True, text=True).stdout.strip()
    assert head_after == head_before, "rollback didn't reset HEAD"
    assert any("protected-path" in w for w in warnings)
    assert state["i"] == 1  # loop broke after rollback


# --- rounds cap is respected ----------------------------------------------

def test_rounds_cap_respected_with_residuals(env):
    """If the conformer keeps returning a clean-looking result that the
    orchestrator considers non-clean (e.g. build failed), the loop runs
    up to `caps[conformance_rounds]` times. Residuals are surfaced as
    warnings; nothing escalates to failed/blocked."""
    c = env["pila"]
    failing = _clean_result(
        rules_files_read=["README.md"],
        rule_violations_residual=[{"rule": "r", "why_not_fixed": "still bad"}],
        build={"ran": True, "passed": False, "command": "make",
               "summary": "oops"},
    )
    state = _stub_run_conformer(c, [failing, failing, failing, failing])

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    assert state["i"] == env["caps"]["conformance_rounds"]
    assert any("rule-residual" in w for w in warnings)
    assert any("build-failed" in w for w in warnings)


# --- the phase never returns failure --------------------------------------

def test_phase_never_returns_failed_status(env):
    """No matter what the conformer does, _run_conformance_phase returns
    (result_or_none, warnings_list) — never a status that could escalate
    the subtask to failed/blocked."""
    c = env["pila"]
    # Mix of crash, malformed, bad commits, residuals — none of these are
    # supposed to fail the subtask.
    state = _stub_run_conformer(c, [None])
    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))
    # Returned shape: 2-tuple, second element is a list.
    assert isinstance(warnings, list)
    # res may be None on crash; that's the intended advisory signal.
    assert res is None or isinstance(res, dict)


# --- commit-prefix observability ------------------------------------------

def test_unprefixed_conformer_commits_surface_as_warnings(env):
    """A conformer that commits with a subject NOT prefixed `conformer:`
    must surface a warning, but must NOT trigger rollback. The commit
    content is still valid; only the discipline is lapsed."""
    c = env["pila"]

    def _unprefixed_commit(wt: Path):
        (wt / "docs.txt").write_text("doc\n")
        _run(["git", "add", "-A"], cwd=wt)
        _run(["git", "commit", "-q", "-m", "docs: update without prefix"],
             cwd=wt)

    state = _stub_run_conformer(c, [_clean_result()],
                                commits={0: _unprefixed_commit})
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env["worktree"],
        capture_output=True, text=True).stdout.strip()

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env["worktree"],
        capture_output=True, text=True).stdout.strip()
    assert head_after != head_before, \
        "unprefixed commits must NOT be rolled back — they're still valid work"
    assert any("missing `conformer:` prefix" in w for w in warnings)
    # The commit subject should appear in the warning text for traceability.
    assert any("docs: update without prefix" in w for w in warnings)


def test_prefixed_conformer_commits_do_not_warn(env):
    """A conformer that follows the discipline produces no prefix
    warning."""
    c = env["pila"]

    def _good_commit(wt: Path):
        (wt / "notes.txt").write_text("notes\n")
        _run(["git", "add", "-A"], cwd=wt)
        _run(["git", "commit", "-q", "-m", "conformer: add release notes"],
             cwd=wt)

    state = _stub_run_conformer(c, [_clean_result()],
                                commits={0: _good_commit})

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    assert not any("missing `conformer:` prefix" in w for w in warnings)


# --- worker budget exhaustion is advisory, not fatal ----------------------

def test_bump_workers_exhaustion_surfaces_as_warning(env, monkeypatch):
    """When `max_total_workers` is exhausted at conformer-spawn time,
    `bump_workers` raises WorkerError. The conformance phase must catch
    that and surface it as a `conformance_warnings` entry — it must NOT
    propagate up and crash the subtask. This pins the fix for the
    third-pass audit bug: bump_workers placement inside run_conformer's
    try block."""
    c = env["pila"]
    # Force the cap to a value already exceeded by st.data["worker_count"].
    env["st"].data["worker_count"] = 100
    env["st"].save()
    env["caps"]["max_total_workers"] = 1  # any positive value < worker_count

    # Make claude_p obviously detectable in case we incorrectly fall
    # through to it (we shouldn't — bump_workers should raise first).
    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("claude_p was called despite budget exhaustion")
    monkeypatch.setattr(c, "claude_p", _should_not_be_called)

    # Call run_conformer directly. It should catch the budget WorkerError
    # raised by bump_workers and return None.
    result = asyncio.run(c.run_conformer(
        env["sid"], env["run_dir"], str(env["worktree"]), env["caps"],
        env["st"], env["models"],
        rules_files=[], blt_commands={"build": "", "lint": "", "test": ""},
        diff_base="dummy"))
    assert result is None, "budget-exhausted conformer must return None"

    # Now exercise the full phase loop to confirm the warning surfaces.
    env["st"].data["worker_count"] = 100
    env["st"].save()
    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))
    assert res is None
    assert any("crashed" in w for w in warnings), \
        "budget exhaustion should surface as a 'crashed' advisory warning"


# --- outer contract: settle_subtask never escalates conformance failures --

def test_settle_subtask_never_escalates_on_conformer_crash(env, monkeypatch):
    """The outer contract — settle_subtask must NEVER return a result
    with status `failed` or `blocked` due to a conformance failure. This
    tightens the contract verification beyond the inner-helper tests:
    those verify _run_conformance_phase returns advisory warnings; this
    verifies the caller actually honors that and doesn't re-escalate."""
    c = env["pila"]

    # Stub run_implementer to return a clean `complete` result without
    # actually spawning a worker. The worktree already has the implementer's
    # commit from the env fixture, so the per-subtask gates will pass.
    async def _stub_implementer(sid, pila_dir, caps, st, models,
                                continuation=False, note=""):
        return {
            "subtask_id": sid,
            "status": "complete",
            "criteria_results": [
                {"criterion": "f() exists", "met": True, "evidence": "src.py"},
            ],
        }
    monkeypatch.setattr(c, "run_implementer", _stub_implementer)

    # Conformer crashes (returns None). _run_conformance_phase surfaces
    # this as a warning; settle_subtask must still return `complete`.
    _stub_run_conformer(c, [None])

    res = asyncio.run(c.settle_subtask(
        env["sid"], env["run_dir"], env["caps"], env["st"], env["models"]))

    assert res["status"] == "complete", \
        f"conformer crash escalated subtask to {res['status']!r}"
    assert res["status"] not in ("failed", "blocked")
    # The conformance failure should still be surfaced — just not fatally.
    assert res.get("conformance_warnings"), \
        "conformer crash must produce conformance_warnings on the result"


def test_settle_subtask_never_escalates_on_conformer_residuals(env, monkeypatch):
    """Same outer contract under a different failure mode: the conformer
    reports residuals and failing build/lint/tests round after round
    until the cap is hit. The subtask still returns `complete`."""
    c = env["pila"]

    async def _stub_implementer(sid, pila_dir, caps, st, models,
                                continuation=False, note=""):
        return {
            "subtask_id": sid,
            "status": "complete",
            "criteria_results": [
                {"criterion": "f() exists", "met": True, "evidence": "src.py"},
            ],
        }
    monkeypatch.setattr(c, "run_implementer", _stub_implementer)

    failing = _clean_result(
        rules_files_read=["README.md"],
        rule_violations_residual=[{"rule": "r", "why_not_fixed": "still bad"}],
        tests={"ran": True, "passed": False, "command": "pytest",
               "summary": "1 failed"},
    )
    _stub_run_conformer(c, [failing] * 10)

    res = asyncio.run(c.settle_subtask(
        env["sid"], env["run_dir"], env["caps"], env["st"], env["models"]))

    assert res["status"] == "complete"
    assert res.get("conformance_warnings"), \
        "residuals must surface as warnings on the subtask result"


# --- the phase survives unexpected exceptions (fourth-pass audit follow-up) -

def test_settle_subtask_survives_unexpected_exception_in_conformance(env, monkeypatch):
    """The conformance phase is documented as 'never raises a workflow
    error.' But underlying `run_proc` calls `asyncio.create_subprocess_exec`,
    which raises FileNotFoundError when cwd is missing. The settle_subtask
    splice has a broad try/except specifically to honor the advisory
    contract for any unexpected exception — including this one. Verify the
    subtask still returns `complete` with a warning."""
    c = env["pila"]

    # Stub run_implementer to short-circuit to a clean complete result.
    async def _stub_implementer(sid, pila_dir, caps, st, models,
                                continuation=False, note=""):
        return {
            "subtask_id": sid,
            "status": "complete",
            "criteria_results": [
                {"criterion": "f() exists", "met": True, "evidence": "src.py"},
            ],
        }
    monkeypatch.setattr(c, "run_implementer", _stub_implementer)

    # Stub _run_conformance_phase to raise a synthetic unexpected exception
    # that mimics the realistic FileNotFoundError-from-missing-worktree case.
    async def _explode(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory",
                                "/nonexistent/worktree")
    monkeypatch.setattr(c, "_run_conformance_phase", _explode)

    res = asyncio.run(c.settle_subtask(
        env["sid"], env["run_dir"], env["caps"], env["st"], env["models"]))

    assert res["status"] == "complete", \
        f"unexpected exception in conformance escalated subtask to {res['status']!r}"
    warnings = res.get("conformance_warnings") or []
    assert warnings, "the exception must surface as a conformance_warnings entry"
    assert any("FileNotFoundError" in w for w in warnings)


# --- dirty-state warning before rollback (fourth-pass audit follow-up) -----

def test_protected_path_rollback_warns_about_discarded_uncommitted(env):
    """When the conformer commits to a protected path AND leaves
    uncommitted changes to tracked files, the rollback (git reset --hard)
    will silently erase those uncommitted scribbles. The phase must surface
    a warning naming the discarded files BEFORE rolling back."""
    c = env["pila"]

    def _bad_with_uncommitted(wt: Path):
        # Commit a protected-path change (will trigger rollback).
        (wt / ".claude").mkdir(exist_ok=True)
        (wt / ".claude" / "x").write_text("bad\n")
        _run(["git", "add", "-A"], cwd=wt)
        _run(["git", "commit", "-q", "-m",
              "conformer: BAD touched protected path"], cwd=wt)
        # Now leave an uncommitted modification to a TRACKED file.
        # (`src.py` was committed by the env fixture.)
        (wt / "src.py").write_text(
            "def f():\n    pass\n\n# uncommitted scribble\n")

    _stub_run_conformer(c, [_clean_result()],
                        commits={0: _bad_with_uncommitted})

    res, warnings = asyncio.run(c._run_conformance_phase(
        env["sid"], env["run_dir"], str(env["worktree"]), env["subtask"],
        env["caps"], env["st"], env["models"]))

    # The protected-path rollback warning should be present (as before).
    assert any("protected-path" in w for w in warnings)
    # AND a new warning should call out the discarded uncommitted file.
    assert any("discarding" in w and "src.py" in w for w in warnings), \
        f"expected a 'discarding' warning mentioning src.py; got {warnings!r}"
