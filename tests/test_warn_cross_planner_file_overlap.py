"""Tests for warn_cross_planner_file_overlap() — the plan-validation
warning that surfaces when multiple planners claim the same file in
`files_likely_touched`.

The warning is a warning, never a hard fail. Empirical grounding from
historical pila runs: 0 false positives in the n=3 runs available
when this check was added (a successful run had 0 overlaps; two
failures had 9 and 10 respectively).
"""
from __future__ import annotations

import re

import pytest


def _capture_logs(pila, monkeypatch):
    """Capture every `log()` call so the test can assert on emitted lines."""
    lines: list[str] = []
    monkeypatch.setattr(pila, "log", lambda msg: lines.append(msg))
    return lines


def test_no_overlap_is_silent(pila, monkeypatch):
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001", "files_likely_touched": ["src/a.py"]},
            {"id": "feat-002", "files_likely_touched": ["src/b.py"]},
        ]},
        {"domain": "refactor", "subtasks": [
            {"id": "refactor-001", "files_likely_touched": ["src/c.py"]},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    assert lines == []


def test_overlap_within_same_planner_does_not_warn(pila, monkeypatch):
    """Two subtasks in the SAME planner touching the same file is the
    planner's own business (intra-domain ordering); only cross-planner
    overlap is suspicious."""
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001", "files_likely_touched": ["src/shared.py"]},
            {"id": "feat-002", "files_likely_touched": ["src/shared.py"]},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    assert lines == []


def test_cross_planner_overlap_warns(pila, monkeypatch):
    """The stackpulse failure case: feat-001 and refactor-001 both
    claim src/app/globals.css with contradictory criteria."""
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001",
             "files_likely_touched": ["src/app/globals.css"]},
        ]},
        {"domain": "refactor", "subtasks": [
            {"id": "refactor-001",
             "files_likely_touched": ["src/app/globals.css"]},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    assert any("cross-planner file overlap" in l for l in lines)
    assert any("globals.css" in l for l in lines)
    assert any("feat(feat-001)" in l for l in lines)
    assert any("refactor(refactor-001)" in l for l in lines)


def test_warn_reports_count(pila, monkeypatch):
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001",
             "files_likely_touched": ["a.css", "b.css", "c.css"]},
        ]},
        {"domain": "refactor", "subtasks": [
            {"id": "refactor-001",
             "files_likely_touched": ["a.css", "b.css", "c.css"]},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    # The leading log line should mention the count "3 file(s)".
    summary = [l for l in lines if "cross-planner file overlap" in l]
    assert summary, "expected a summary warning line"
    assert re.search(r"3 file\(s\)", summary[0])


def test_multiple_subtasks_per_planner_listed(pila, monkeypatch):
    """When one planner has multiple subtasks claiming the same file,
    all sids must show up in the per-file detail line so the user can
    see the full picture."""
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001", "files_likely_touched": ["x.tsx"]},
            {"id": "feat-002", "files_likely_touched": ["x.tsx"]},
        ]},
        {"domain": "refactor", "subtasks": [
            {"id": "refactor-001", "files_likely_touched": ["x.tsx"]},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    detail = [l for l in lines if "x.tsx" in l]
    assert detail, "expected a per-file detail line"
    assert "feat-001" in detail[0]
    assert "feat-002" in detail[0]
    assert "refactor-001" in detail[0]


def test_empty_plans_list(pila, monkeypatch):
    lines = _capture_logs(pila, monkeypatch)
    pila.warn_cross_planner_file_overlap([])
    assert lines == []


def test_plan_with_no_subtasks(pila, monkeypatch):
    """A planner that returned an empty plan (e.g., 'nothing in this
    domain needs doing') should not produce a warning by itself."""
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001", "files_likely_touched": ["a.py"]},
        ]},
        {"domain": "docs", "subtasks": []},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    assert lines == []


def test_missing_files_likely_touched_is_safe(pila, monkeypatch):
    """A subtask without `files_likely_touched` shouldn't crash the
    warning function."""
    lines = _capture_logs(pila, monkeypatch)
    plans = [
        {"domain": "feat", "subtasks": [
            {"id": "feat-001"},  # no files_likely_touched
        ]},
        {"domain": "refactor", "subtasks": [
            {"id": "refactor-001"},
        ]},
    ]
    pila.warn_cross_planner_file_overlap(plans)
    assert lines == []
