"""Tests for _retryable_failure() — the retry policy classifier.

Includes the coupling test promised in IMPLEMENTATION.md §10: the
retryable markers in `_retryable_failure` must match the error strings
actually emitted by `check_branch_has_commits` and the inline
dirty-worktree check in `settle_subtask`. If either side drifts without
the other being updated, the coupling test fails.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

CENTELLA_PY = (Path(__file__).resolve().parent.parent
               / "orchestrator" / "centella.py")


# --- behavior of _retryable_failure ---------------------------------------

@pytest.mark.parametrize("reason", [
    "subtask branch for feat-001 has no commits ahead of the run branch (centella/feat-foo-abc123) — implementer claimed complete without making any changes",
    "worktree has 3 uncommitted change(s) — implementer left it dirty",
    "single uncommitted change found",
])
def test_retryable_strings_return_true(centella, reason):
    assert centella._retryable_failure(reason) is True


@pytest.mark.parametrize("reason", [
    "diff touches protected path(s): ['.centella/state.json']",
    "validate_result cross-field invariant violated",
    "worker reported is_error",
    "schema validation failed twice",
    "",
])
def test_terminal_strings_return_false(centella, reason):
    assert centella._retryable_failure(reason) is False


# --- the coupling test ----------------------------------------------------

def test_retryable_markers_match_check_strings(centella):
    """The substring markers in _retryable_failure must appear verbatim
    in the source of the functions that emit the corresponding error
    strings. If a check function's wording changes, this test fails and
    forces the retry classifier to be updated in the same change."""
    source = CENTELLA_PY.read_text()

    # Extract the markers from _retryable_failure's source so the test
    # finds whichever markers the function currently declares.
    retryable_src = inspect.getsource(centella._retryable_failure)
    markers_match = re.search(
        r"retryable_markers\s*=\s*\(([^)]+)\)", retryable_src, re.DOTALL
    )
    assert markers_match, "could not locate retryable_markers tuple in source"
    markers = re.findall(r'"([^"]+)"', markers_match.group(1))
    assert markers, "no markers extracted from retryable_markers tuple"

    # Locate check_branch_has_commits' source and confirm its error
    # string contains one of the markers.
    cbc_src = inspect.getsource(centella.check_branch_has_commits)
    assert any(m in cbc_src for m in markers), (
        f"check_branch_has_commits emits no string matching any of "
        f"{markers!r} — the retry classifier would not treat its failure "
        f"as retryable."
    )

    # The dirty-worktree check is inline in settle_subtask, not its own
    # function. Find it in the centella.py source text and confirm one
    # of the markers appears.
    settle_match = re.search(
        r"^(?:async )?def settle_subtask\b.*?"
        r"(?=^(?:async )?(?:def |class ))",
        source, re.DOTALL | re.MULTILINE,
    )
    assert settle_match, "could not locate settle_subtask in source"
    settle_src = settle_match.group(0)
    assert any(m in settle_src for m in markers), (
        f"settle_subtask emits no string matching any of {markers!r} "
        f"in its dirty-worktree check — the retry classifier would not "
        f"treat that failure as retryable."
    )


def test_coupling_specifics(centella):
    """Sanity check: the two known coupled pairs are still in force.
    Looser than test_retryable_markers_match_check_strings but reads
    more clearly when this test alone fails."""
    cbc_src = inspect.getsource(centella.check_branch_has_commits)
    assert "no commits ahead of the run" in cbc_src, (
        "check_branch_has_commits no longer emits the "
        "'no commits ahead of the run' marker"
    )

    source = CENTELLA_PY.read_text()
    settle_match = re.search(
        r"^(?:async )?def settle_subtask\b.*?"
        r"(?=^(?:async )?(?:def |class ))",
        source, re.DOTALL | re.MULTILINE,
    )
    assert settle_match
    assert "uncommitted change" in settle_match.group(0), (
        "settle_subtask's dirty-worktree check no longer emits the "
        "'uncommitted change' marker"
    )
