"""Tests for the central property of the per-run refactor: two
pila runs in the same repository have completely disjoint state.

These tests exercise the per-run State and directory layout without
spawning real workers. They confirm that:

- Two runs' State instances write to disjoint paths.
- Mutations on one State don't bleed into the other's saved state.json.
- Per-run subdirectories (worktrees/, criteria/, logs/, etc.) are
  scoped under the run dir, so two runs can use the same `sid` without
  colliding.

The single-clone parallel-safety property is the stated goal of the
refactor (DESIGN §6). If this test fails, the refactor's central
guarantee is broken.
"""
from __future__ import annotations

import json


def test_disjoint_state_paths(pila, tmp_path):
    """The minimum invariant: two State instances write to different
    state.json paths."""
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    assert sa.path != sb.path


def test_disjoint_run_dirs(pila, tmp_path):
    """Run dirs are siblings under pila_root/runs/; nothing inside
    one's dir is shared with the other."""
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    assert sa.run_dir != sb.run_dir
    # No path-prefix relationship in either direction.
    assert not str(sa.run_dir).startswith(str(sb.run_dir))
    assert not str(sb.run_dir).startswith(str(sa.run_dir))


def test_disjoint_subpaths(pila, tmp_path):
    """The per-run subdirectories that pila code uses
    (pila_dir / 'worktrees', '/criteria', '/logs', '/subtasks',
    '/checkpoints') are all under the run_dir — and therefore disjoint
    between runs."""
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    for sub in ("worktrees", "criteria", "logs", "subtasks", "checkpoints"):
        assert sa.run_dir / sub != sb.run_dir / sub


def test_concurrent_saves_do_not_clobber(pila, tmp_path):
    """The critical case the old single-state.json model couldn't handle:
    two runs writing different `task` strings, both saved. Each State's
    save must land in its own file with its own data — neither clobbers
    the other."""
    (tmp_path / "runs" / "feat-a-aaaaaa").mkdir(parents=True)
    (tmp_path / "runs" / "fix-b-bbbbbb").mkdir(parents=True)
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    sa.data = {"task": "task A"}
    sb.data = {"task": "task B"}
    sa.save()
    sb.save()

    # Each file holds its own data.
    a_loaded = json.loads(sa.path.read_text())
    b_loaded = json.loads(sb.path.read_text())
    assert a_loaded["task"] == "task A"
    assert b_loaded["task"] == "task B"


def test_interleaved_saves_do_not_clobber(pila, tmp_path):
    """Simulate the worst case: two runs interleave their save() calls.
    Atomic write-via-temp-rename within each State, and the use of
    distinct paths between States, means neither clobbers the other."""
    (tmp_path / "runs" / "feat-a-aaaaaa").mkdir(parents=True)
    (tmp_path / "runs" / "fix-b-bbbbbb").mkdir(parents=True)
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    sa.data = {"task": "A"}
    sa.save()
    sb.data = {"task": "B"}
    sb.save()
    sa.data["task"] = "A2"
    sa.save()
    sb.data["task"] = "B2"
    sb.save()

    assert json.loads(sa.path.read_text())["task"] == "A2"
    assert json.loads(sb.path.read_text())["task"] == "B2"


def test_same_subtask_id_in_different_runs(pila, tmp_path):
    """The planner's deterministic subtask IDs (`feat-001`, `bugfix-002`,
    etc.) would collide between runs under the old layout. Under
    per-run, each run scopes its worktrees under its own dir, so the
    `feat-001` worktree of run A is at a different path than the
    `feat-001` worktree of run B."""
    sa = pila.State(tmp_path, "feat-a-aaaaaa")
    sb = pila.State(tmp_path, "fix-b-bbbbbb")
    wt_a = sa.run_dir / "worktrees" / "feat-001"
    wt_b = sb.run_dir / "worktrees" / "feat-001"
    assert wt_a != wt_b
