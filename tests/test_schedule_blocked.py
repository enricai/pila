"""Tests for schedule()'s handling of blocked planner outputs.

DESIGN §8 planner gate: a planner can exit `status: "blocked"` with an
empty subtasks list. schedule() must:
  - die with an informative message when ALL planners block (and thus
    no subtasks exist)
  - emit a WARNING and proceed when SOME planners block but at least
    one produced subtasks

The warning path is the P2-1 audit finding: silent loss of a domain is
a footgun, so the partial-block case surfaces a second log line at
scheduling time even though phase_plan already logged each blocked
domain.
"""
from __future__ import annotations

import pytest


def _good_subtask(sid="feat-001", **overrides):
    """A baseline well-formed subtask, overridable per-test."""
    base = {
        "id": sid,
        "title": "a good subtask",
        "depends_on": [],
        "requires": [],
        "provides": [],
        "success_criteria_seed": "the thing is done",
        "size": "small",
    }
    base.update(overrides)
    return base


def _ready_plan(domain: str, *subtasks: dict) -> dict:
    """A planner output with status ready and the given subtasks."""
    return {
        "domain": domain,
        "status": "ready",
        "subtasks": list(subtasks),
    }


def _blocked_plan(domain: str, gap: dict | None = None) -> dict:
    """A planner output with status blocked, empty subtasks, gap analysis."""
    return {
        "domain": domain,
        "status": "blocked",
        "subtasks": [],
        "confidence": {
            "task_understanding": 7.5,
            "decomposition_quality": 6.0,
            "basis": "could not pin the scope",
            "falsifiers_tested": [],
            "contradictions_reconciled": [],
            "gap_to_close": gap or {
                "task_understanding": "need clarification on X",
            },
        },
    }


def test_all_blocked_dies_with_informative_message(pila, capsys):
    """When every planner blocked, schedule() dies citing each blocked
    domain and pointing the user at the configurable knob and the gap
    field — not the generic 'no subtasks' message."""
    plans = [
        _blocked_plan("feature-implementation"),
        _blocked_plan("bug-fixing"),
    ]
    with pytest.raises(SystemExit) as exc:
        pila.schedule(plans)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    # Specific blocked domains are named so the user knows which planners
    # could not clear the gate.
    assert "feature-implementation" in err
    assert "bug-fixing" in err
    # The hint points at the knob and the gap field, not just "failed."
    assert "confidence_rounds" in err.lower() or "confidence-rounds" in err
    assert "gap_to_close" in err


def test_partial_block_emits_warning_and_proceeds(pila, capsys):
    """When some planners block but at least one produced subtasks,
    schedule() must succeed AND log a WARNING naming the blocked
    domain(s). Silent loss of a domain is the footgun this test guards
    against."""
    plans = [
        _ready_plan("feature-implementation", _good_subtask("feat-001")),
        _blocked_plan("bug-fixing"),
    ]
    subtasks, waves = pila.schedule(plans)
    # Scheduling proceeded with the ready domain's subtasks.
    assert "feat-001" in subtasks
    assert any("feat-001" in wave for wave in waves)
    # The blocked domain is named in a WARNING that schedule() emits
    # to stdout via log() (capsys captures both streams).
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "bug-fixing" in out


def test_all_ready_no_warning(pila, capsys):
    """No blocked planners → no WARNING line. Sanity check that the
    warning fires only on the partial-block path, not unconditionally."""
    plans = [
        _ready_plan("feature-implementation", _good_subtask("feat-001")),
        _ready_plan("testing", _good_subtask("test-001")),
    ]
    subtasks, _waves = pila.schedule(plans)
    assert set(subtasks.keys()) == {"feat-001", "test-001"}
    out = capsys.readouterr().out
    assert "WARNING" not in out


def test_blocked_domain_without_subtasks_does_not_contribute_provides(pila):
    """Sanity: a blocked planner has subtasks=[], so it provides nothing.
    A ready sibling that requires a capability the blocked planner would
    have provided will fail validate_plan (tested separately); schedule()
    itself just won't see that capability in the providers map.

    This test pins the upstream fact: schedule() merges only the
    subtasks of ready (or empty-but-ready) plans, never fabricates
    'provides' on behalf of a blocked planner."""
    plans = [
        _ready_plan("feature-implementation", _good_subtask("feat-001")),
        _blocked_plan("refactoring"),
    ]
    subtasks, _waves = pila.schedule(plans)
    # Only the ready domain's subtask is in the merged map.
    assert list(subtasks.keys()) == ["feat-001"]
