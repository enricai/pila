"""Tests for validate_plan() — the structural validation of merged plans.

Mirrors the IMPLEMENTATION.md §5 plan-validation table. validate_plan
accumulates every issue and dies once with a multi-bullet message, so
each test checks the substring of the relevant error.
"""
from __future__ import annotations

import pytest


def _good_subtask(sid="feat-001", **overrides):
    """A baseline well-formed subtask, overridable per-test."""
    base = {
        "id": sid,
        "title": "a good subtask",
        "intent": "do the thing",
        "scope_note": "one verifiable change",
        "files_likely_touched": ["src/foo.py"],
        "depends_on": [],
        "requires": [],
        "provides": [],
        "success_criteria_seed": "the thing is done",
        "size": "small",
        "investigation_notes": "",
    }
    base.update(overrides)
    return base


def test_well_formed_plan_passes(centella):
    """A clean plan with one subtask per domain-prefixed id passes silently."""
    plan = {
        "feat-001": _good_subtask("feat-001"),
        "test-001": _good_subtask("test-001"),
    }
    # No SystemExit raised → pass.
    centella.validate_plan(plan)


def test_id_without_domain_prefix_dies(centella, capsys):
    plan = {"random-001": _good_subtask("random-001")}
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "must start with one of" in err
    assert "random-001" in err


def test_size_large_dies(centella, capsys):
    plan = {"feat-001": _good_subtask("feat-001", size="large")}
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "size='large'" in err
    assert "split" in err


def test_empty_success_criteria_seed_dies(centella, capsys):
    plan = {"feat-001": _good_subtask("feat-001", success_criteria_seed="")}
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "success_criteria_seed is empty" in err


def test_whitespace_only_success_criteria_seed_dies(centella, capsys):
    plan = {"feat-001": _good_subtask("feat-001", success_criteria_seed="   \n  ")}
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "success_criteria_seed is empty" in err


def test_dangling_depends_on_dies(centella, capsys):
    plan = {
        "feat-001": _good_subtask("feat-001", depends_on=["feat-999"]),
    }
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "depends_on 'feat-999'" in err
    assert "does not exist" in err


def test_unresolvable_requires_dies(centella, capsys):
    plan = {
        "feat-001": _good_subtask("feat-001", requires=["nonexistent-cap"]),
    }
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    assert "requires 'nonexistent-cap'" in err
    assert "nothing provides it" in err


def test_resolvable_requires_passes(centella):
    """When provides on one subtask matches requires on another, it passes."""
    plan = {
        "feat-001": _good_subtask("feat-001", provides=["feature-x-live"]),
        "test-001": _good_subtask("test-001", requires=["feature-x-live"]),
    }
    centella.validate_plan(plan)


def test_multiple_errors_accumulated(centella, capsys):
    """validate_plan reports every error in one die() call, not the first."""
    plan = {
        "random-001": _good_subtask(
            "random-001", size="large", success_criteria_seed=""
        ),
    }
    with pytest.raises(SystemExit):
        centella.validate_plan(plan)
    err = capsys.readouterr().err
    # Three issues from this one subtask: bad prefix, large size, empty seed.
    assert "must start with one of" in err
    assert "size='large'" in err
    assert "success_criteria_seed is empty" in err
    assert "3 issue" in err


@pytest.mark.parametrize("prefix", [
    "bugfix-", "feat-", "refactor-", "perf-",
    "test-", "deps-", "config-", "docs-",
])
def test_all_documented_prefixes_accepted(centella, prefix):
    sid = f"{prefix}001"
    plan = {sid: _good_subtask(sid)}
    centella.validate_plan(plan)
