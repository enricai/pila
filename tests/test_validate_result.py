"""Tests for validate_result() — cross-field invariant checks on
worker results.

Returns an error string when the result is missing a required
mechanical-precondition field for its status branch, None otherwise.
Per DESIGN §8 the criteria file is informational and the
`complete` branch no longer gates on `criteria_results` shape or
content — those tests cover that loosening.
"""
from __future__ import annotations


# --- complete status -------------------------------------------------------
# Per DESIGN §8 the §8 confidence gate is the only load-bearing signal;
# the criteria file is informational (DESIGN §9). `complete` is accepted
# regardless of what `criteria_results` carries.

def test_complete_with_empty_criteria_results_returns_none(pila):
    """Empty criteria_results no longer rejects `complete`."""
    assert pila.validate_result(
        {"status": "complete", "criteria_results": []}) is None


def test_complete_with_missing_criteria_results_returns_none(pila):
    """Missing criteria_results no longer rejects `complete`."""
    assert pila.validate_result({"status": "complete"}) is None


def test_complete_with_all_met_criteria_returns_none(pila):
    assert pila.validate_result({
        "status": "complete",
        "criteria_results": [
            {"criterion": "tests pass", "met": True, "evidence": "ran them"},
            {"criterion": "no regressions", "met": True, "evidence": "verified"},
        ],
    }) is None


def test_complete_with_failing_criteria_returns_none(pila):
    """`met:false` entries are recorded as warnings but do not reject
    `complete` (DESIGN §8 — confidence gate is the only load-bearing
    signal)."""
    assert pila.validate_result({
        "status": "complete",
        "criteria_results": [
            {"criterion": "tests pass", "met": True, "evidence": "ok"},
            {"criterion": "no regressions", "met": False, "evidence": "broke X"},
        ],
    }) is None


def test_complete_missing_criteria_file_returns_none(pila, tmp_path):
    """`pila_dir` is accepted for backwards compatibility but no
    longer consulted — a missing criteria file does not reject
    `complete`."""
    (tmp_path / "criteria").mkdir()
    assert pila.validate_result({
        "status": "complete",
        "subtask_id": "feat-001",
        "criteria_results": [{"criterion": "x", "met": True}],
    }, pila_dir=tmp_path) is None


# --- incomplete-handoff status ---------------------------------------------

def test_incomplete_handoff_without_checkpoint_path_returns_error(pila):
    err = pila.validate_result({"status": "incomplete-handoff"})
    assert err is not None
    assert "checkpoint_path" in err


def test_incomplete_handoff_with_null_checkpoint_path_returns_error(pila):
    err = pila.validate_result(
        {"status": "incomplete-handoff", "checkpoint_path": None}
    )
    assert err is not None
    assert "checkpoint_path" in err


def test_incomplete_handoff_with_nonexistent_checkpoint_returns_error(pila, tmp_path):
    err = pila.validate_result(
        {"status": "incomplete-handoff",
         "checkpoint_path": str(tmp_path / "nonexistent.md")}
    )
    assert err is not None
    assert "does not exist" in err


def test_incomplete_handoff_with_existing_checkpoint_returns_none(pila, tmp_path):
    cp = tmp_path / "checkpoint.md"
    cp.write_text("# checkpoint\n")
    assert pila.validate_result(
        {"status": "incomplete-handoff", "checkpoint_path": str(cp)}
    ) is None


# --- blocked status --------------------------------------------------------

def test_blocked_without_blocker_returns_error(pila):
    err = pila.validate_result({"status": "blocked"})
    assert err is not None
    assert "blocker" in err


def test_blocked_with_empty_blocker_returns_error(pila):
    err = pila.validate_result({"status": "blocked", "blocker": "   "})
    assert err is not None
    assert "blocker" in err


def test_blocked_with_blocker_returns_none(pila):
    assert pila.validate_result(
        {"status": "blocked", "blocker": "missing API key XYZ"}
    ) is None


# --- failed status ---------------------------------------------------------
# A `failed` result must carry a non-empty summary (the worker's diagnosis).
# The prompt requires it; the code enforces it per DESIGN §12.

def test_failed_with_empty_summary_returns_error(pila):
    assert pila.validate_result({"status": "failed"}) is not None
    assert pila.validate_result({"status": "failed", "summary": ""}) is not None
    assert pila.validate_result({"status": "failed", "summary": "   "}) is not None


def test_failed_with_summary_returns_none(pila):
    assert pila.validate_result(
        {"status": "failed", "summary": "tests still red after 5 iterations"}
    ) is None
