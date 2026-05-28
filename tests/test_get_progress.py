"""Tests for _get_progress() — the helper that computes the (done, total)
subtask fraction shown in each inline log line once waves are scheduled.

_get_progress returns None before planning (no waves key) and once waves
exist it counts terminal statuses against the total subtask count.
Terminal statuses are complete, failed, and blocked — anything else
(in_progress, pending, etc.) does not count as done.
"""
from __future__ import annotations

from types import SimpleNamespace


def test_no_waves_returns_none(pila):
    """Before planning, st.data has no 'waves' key. Returns None so
    classifier/planner workers emit no progress prefix."""
    st = SimpleNamespace(data={})
    assert pila._get_progress(st) is None


def test_empty_waves_returns_none(pila):
    """waves=[] means planning produced no subtasks. Returns None
    rather than (0, 0) to avoid showing a misleading '[0/0]' prefix."""
    st = SimpleNamespace(data={"waves": [], "subtask_status": {}})
    assert pila._get_progress(st) is None


def test_all_pending_returns_zero_done(pila):
    """When waves exist but no subtask has reached a terminal status,
    done=0 and total equals the sum of subtask counts across waves."""
    st = SimpleNamespace(data={
        "waves": [["a", "b"], ["c"]],
        "subtask_status": {},
    })
    assert pila._get_progress(st) == (0, 3)


def test_some_terminal_counted(pila):
    """Only complete and failed count as terminal; in_progress does not.
    This is the common mid-run state: some done, some still running."""
    st = SimpleNamespace(data={
        "waves": [["a", "b", "c"]],
        "subtask_status": {"a": "complete", "b": "failed", "c": "in_progress"},
    })
    assert pila._get_progress(st) == (2, 3)


def test_all_terminal(pila):
    """All subtasks in a terminal state → done == total."""
    st = SimpleNamespace(data={
        "waves": [["a", "b"]],
        "subtask_status": {"a": "complete", "b": "blocked"},
    })
    assert pila._get_progress(st) == (2, 2)


def test_blocked_counted_as_terminal(pila):
    """'blocked' is a terminal status — a blocked subtask has stopped
    making progress and must be counted as done for the fraction."""
    st = SimpleNamespace(data={
        "waves": [["a"]],
        "subtask_status": {"a": "blocked"},
    })
    assert pila._get_progress(st) == (1, 1)
