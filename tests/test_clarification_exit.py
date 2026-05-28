"""Tests for the DESIGN §11 mid-execution clarification exit
(`status: "needs-clarification"`).

Covers:
  - the cap rename: `handoff_continuations` → `subtask_continuations`
  - the schema additions (new status enum value, new
    `clarification_question` field)
  - the cross-field invariant in `validate_result`: the new status
    requires both `clarification_question` (with three non-empty
    sub-fields) AND `checkpoint_path` (existing and on disk)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ----- cap rename ------------------------------------------------------------

def test_subtask_continuations_default(pila):
    """The unified per-subtask re-spawn budget — consumed by BOTH
    context-exhaustion handoffs and DESIGN §11 clarifications — is 3."""
    assert pila.DEFAULT_CAPS["subtask_continuations"] == 3


def test_handoff_continuations_removed(pila):
    """The old separate cap name must not survive the rename — a
    consumer that reads it would silently get a KeyError at runtime."""
    assert "handoff_continuations" not in pila.DEFAULT_CAPS


# ----- schema: status enum + clarification_question field --------------------

def test_status_enum_includes_needs_clarification(pila):
    impl = pila.SCHEMAS["implementer"]
    enum = impl["properties"]["status"]["enum"]
    assert "needs-clarification" in enum
    # Existing enum values must still be present — the rename was
    # additive, not a replacement.
    for expected in ("complete", "incomplete-handoff", "blocked", "failed"):
        assert expected in enum


def test_clarification_question_field_shape(pila):
    """The clarification_question field, when present, must require all
    three sub-fields. The schema requirement is the structural defense
    against a worker shipping a half-formed question."""
    impl = pila.SCHEMAS["implementer"]
    cq = impl["properties"]["clarification_question"]
    # nullable
    assert "null" in cq["type"]
    # required sub-fields
    assert set(cq["required"]) == {"id", "question", "why_underivable"}
    # all sub-fields are strings
    for f in ("id", "question", "why_underivable"):
        assert cq["properties"][f]["type"] == "string"


# ----- cross-field invariant in validate_result ------------------------------

def _good_clarification_result(checkpoint_path: str) -> dict:
    return {
        "subtask_id": "feat-001",
        "status": "needs-clarification",
        "checkpoint_path": checkpoint_path,
        "clarification_question": {
            "id": "feat-001-q1",
            "question": "Preserve backward compat for v1 clients, or break?",
            "why_underivable": ("both patterns exist in the codebase "
                                "(src/api/v1.py and src/api/v2.py); "
                                "task description says only 'modernize'"),
        },
        "confidence": {
            "root_cause": 8.5, "solution": 7.0, "basis": "—",
            "falsifiers_tested": [], "contradictions_reconciled": [],
            "gap_to_close": {},
        },
    }


def _write_valid_checkpoint(tmp_path: Path) -> Path:
    """Write a checkpoint file that passes the structural check; the
    cross-field invariant only needs the file to exist and be a string
    path, but a realistic test asserts no other check trips."""
    p = tmp_path / "feat-001.md"
    p.write_text(
        "# Checkpoint: feat-001\n"
        "## Frozen success criteria\n- pending\n"
        "## Current status\nStarted; need user input.\n"
        "## Files touched\n- src/api/v2.py — drafted new shape\n"
        "## Decisions made\nnone\n"
        "## Evidence gate status\nroot_cause=8.5\n"
        "## Next action\nAwait clarification on compat policy\n"
        "## Open unknowns\nbackward-compat policy\n"
    )
    return p


def test_validate_result_passes_well_formed_clarification(pila, tmp_path):
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    assert pila.validate_result(res) is None


def test_validate_result_rejects_missing_clarification_question(pila, tmp_path):
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    res["clarification_question"] = None
    err = pila.validate_result(res)
    assert err is not None
    assert "clarification_question" in err
    assert "DESIGN §11" in err


def test_validate_result_rejects_empty_question_field(pila, tmp_path):
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    res["clarification_question"]["question"] = ""
    err = pila.validate_result(res)
    assert err is not None
    assert "question" in err


def test_validate_result_rejects_empty_why_underivable(pila, tmp_path):
    """`why_underivable` is the gate against the worker drifting toward
    'ask instead of research' (DESIGN §11). An empty value means the
    worker didn't justify the question — terminal."""
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    res["clarification_question"]["why_underivable"] = "   "
    err = pila.validate_result(res)
    assert err is not None
    assert "why_underivable" in err


def test_validate_result_rejects_missing_checkpoint_path(pila, tmp_path):
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    res["checkpoint_path"] = None
    err = pila.validate_result(res)
    assert err is not None
    assert "checkpoint_path" in err
    assert "work-in-progress must survive" in err


def test_validate_result_rejects_nonexistent_checkpoint_file(pila, tmp_path):
    res = _good_clarification_result(str(tmp_path / "ghost.md"))
    err = pila.validate_result(res)
    assert err is not None
    assert "does not exist" in err


def test_validate_result_rejects_empty_question_id(pila, tmp_path):
    """Question id is the key for the answer in state.json. Empty id
    means the answer cannot be routed back — terminal."""
    cp = _write_valid_checkpoint(tmp_path)
    res = _good_clarification_result(str(cp))
    res["clarification_question"]["id"] = ""
    err = pila.validate_result(res)
    assert err is not None
    assert "id" in err


# ----- existing invariants still hold (regression guard) ---------------------

def test_incomplete_handoff_still_requires_checkpoint(pila):
    """The cap rename did not affect this invariant; pin it."""
    err = pila.validate_result({
        "subtask_id": "x", "status": "incomplete-handoff",
        "checkpoint_path": None,
    })
    assert err is not None
    assert "incomplete-handoff" in err


def test_blocked_still_requires_blocker(pila):
    err = pila.validate_result({
        "subtask_id": "x", "status": "blocked", "blocker": "",
    })
    assert err is not None
    assert "blocker" in err
