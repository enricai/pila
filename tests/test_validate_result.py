"""Tests for validate_result() — cross-field invariant checks on
worker results.

Returns an error string when the result is self-contradictory, None
when the result is consistent. Tests cover each status enum branch
plus the criteria-file existence side-check.
"""
from __future__ import annotations


# --- complete status -------------------------------------------------------

def test_complete_with_empty_criteria_results_returns_error(centella):
    err = centella.validate_result({"status": "complete", "criteria_results": []})
    assert err is not None
    assert "criteria_results is empty" in err


def test_complete_with_missing_criteria_results_returns_error(centella):
    err = centella.validate_result({"status": "complete"})
    assert err is not None
    assert "criteria_results is empty" in err


def test_complete_with_all_met_criteria_returns_none(centella, tmp_path):
    """When criteria_results show every criterion met, and no centella_dir
    is passed, validate_result returns None."""
    result = {
        "status": "complete",
        "criteria_results": [
            {"criterion": "tests pass", "met": True, "evidence": "ran them"},
            {"criterion": "no regressions", "met": True, "evidence": "verified"},
        ],
    }
    assert centella.validate_result(result) is None


def test_complete_with_one_failing_criterion_returns_error(centella):
    result = {
        "status": "complete",
        "criteria_results": [
            {"criterion": "tests pass", "met": True, "evidence": "ok"},
            {"criterion": "no regressions", "met": False, "evidence": "broke X"},
        ],
    }
    err = centella.validate_result(result)
    assert err is not None
    assert "1 criterion/criteria unmet" in err
    assert "no regressions" in err


def test_complete_with_many_failing_truncates_sample(centella):
    """When more than 3 criteria fail, the error names only the first 3."""
    result = {
        "status": "complete",
        "criteria_results": [
            {"criterion": f"crit-{i}", "met": False} for i in range(5)
        ],
    }
    err = centella.validate_result(result)
    assert err is not None
    assert "5 criterion/criteria unmet" in err
    assert "…" in err  # truncation indicator


def test_complete_missing_met_field_treated_as_failing(centella):
    """A criteria_results entry without an explicit met field is treated
    as not-met (criteria must affirmatively report success)."""
    result = {
        "status": "complete",
        "criteria_results": [{"criterion": "vague"}],  # no 'met' key
    }
    err = centella.validate_result(result)
    assert err is not None
    assert "criterion/criteria unmet" in err


def test_complete_missing_criteria_file_returns_error(centella, tmp_path):
    """When centella_dir is passed but the criteria file is absent,
    validate_result rejects (catches fabricated criteria_results)."""
    (tmp_path / "criteria").mkdir()
    result = {
        "status": "complete",
        "subtask_id": "feat-001",
        "criteria_results": [{"criterion": "x", "met": True}],
    }
    err = centella.validate_result(result, centella_dir=tmp_path)
    assert err is not None
    assert "criteria file does not exist" in err


def test_complete_with_criteria_file_present_returns_none(centella, tmp_path):
    (tmp_path / "criteria").mkdir()
    (tmp_path / "criteria" / "feat-001.md").write_text("the criteria\n")
    result = {
        "status": "complete",
        "subtask_id": "feat-001",
        "criteria_results": [{"criterion": "x", "met": True}],
    }
    assert centella.validate_result(result, centella_dir=tmp_path) is None


# --- incomplete-handoff status ---------------------------------------------

def test_incomplete_handoff_without_checkpoint_path_returns_error(centella):
    err = centella.validate_result({"status": "incomplete-handoff"})
    assert err is not None
    assert "checkpoint_path" in err


def test_incomplete_handoff_with_null_checkpoint_path_returns_error(centella):
    err = centella.validate_result(
        {"status": "incomplete-handoff", "checkpoint_path": None}
    )
    assert err is not None
    assert "checkpoint_path" in err


def test_incomplete_handoff_with_nonexistent_checkpoint_returns_error(centella, tmp_path):
    err = centella.validate_result(
        {"status": "incomplete-handoff",
         "checkpoint_path": str(tmp_path / "nonexistent.md")}
    )
    assert err is not None
    assert "does not exist" in err


def test_incomplete_handoff_with_existing_checkpoint_returns_none(centella, tmp_path):
    cp = tmp_path / "checkpoint.md"
    cp.write_text("# checkpoint\n")
    assert centella.validate_result(
        {"status": "incomplete-handoff", "checkpoint_path": str(cp)}
    ) is None


# --- blocked status --------------------------------------------------------

def test_blocked_without_blocker_returns_error(centella):
    err = centella.validate_result({"status": "blocked"})
    assert err is not None
    assert "blocker" in err


def test_blocked_with_empty_blocker_returns_error(centella):
    err = centella.validate_result({"status": "blocked", "blocker": "   "})
    assert err is not None
    assert "blocker" in err


def test_blocked_with_blocker_returns_none(centella):
    assert centella.validate_result(
        {"status": "blocked", "blocker": "missing API key XYZ"}
    ) is None


# --- failed status ---------------------------------------------------------
# A `failed` result must carry a non-empty summary (the worker's diagnosis).
# The prompt requires it; the code enforces it per DESIGN §12.

def test_failed_with_empty_summary_returns_error(centella):
    assert centella.validate_result({"status": "failed"}) is not None
    assert centella.validate_result({"status": "failed", "summary": ""}) is not None
    assert centella.validate_result({"status": "failed", "summary": "   "}) is not None


def test_failed_with_summary_returns_none(centella):
    assert centella.validate_result(
        {"status": "failed", "summary": "tests still red after 5 iterations"}
    ) is None
