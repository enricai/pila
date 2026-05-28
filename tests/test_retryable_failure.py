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

PILA_PY = (Path(__file__).resolve().parent.parent
               / "orchestrator" / "pila.py")


# --- behavior of _retryable_failure ---------------------------------------

@pytest.mark.parametrize("reason", [
    "subtask branch for feat-001 has no commits ahead of the run branch (pila/runs/feat-foo-abc123) — implementer claimed complete without making any changes",
    "worktree has 3 uncommitted change(s) — implementer left it dirty",
    "single uncommitted change found",
])
def test_retryable_strings_return_true(pila, reason):
    assert pila._retryable_failure(reason) is True


@pytest.mark.parametrize("reason", [
    "diff touches protected path(s): ['.pila/state.json']",
    "validate_result cross-field invariant violated",
    "worker reported is_error",
    "schema validation failed twice",
    "",
])
def test_terminal_strings_return_false(pila, reason):
    assert pila._retryable_failure(reason) is False


# --- the checkpoint-missing branch ----------------------------------------

def test_incomplete_handoff_missing_checkpoint_is_retryable(pila):
    """The validate_result line-2314 message for an `incomplete-handoff`
    worker that produced no checkpoint must be retryable. This is the
    Claude Code session-limit / rate-limit safety net: when the
    subscription cap is hit, claude -p returns the session-limit text
    and the worker's envelope claims `incomplete-handoff` while pointing
    at a checkpoint that was never written. The new prefix-match arm in
    `_retryable_failure` catches it so the next attempt (on a fresh
    process after the reset window, or even on the same process if
    detect_session_limit upstream missed) is allowed to retry."""
    reason = ("checkpoint_path '/Users/x/.pila/runs/r/checkpoints/feat-001.md' "
              "does not exist on disk")
    assert pila._retryable_failure(reason) is True


def test_needs_clarification_missing_checkpoint_stays_terminal(pila):
    """The validate_result line-2350 message (needs-clarification with a
    missing checkpoint) shares both substrings 'checkpoint_path' and
    'does not exist on disk' with the retryable line-2314 message, but
    represents a genuinely-broken worker (the prompt requires both a
    question AND a checkpoint; a worker that asks a question with no
    work-in-progress to come back to is lying about its own status).
    Must stay terminal — the prefix-match in `_retryable_failure`
    disambiguates the two by anchoring on the start of the line-2314
    wording (`checkpoint_path '...`)."""
    reason = ("status='needs-clarification' but checkpoint_path "
              "'/Users/x/.pila/runs/r/checkpoints/feat-001.md' "
              "does not exist on disk")
    assert pila._retryable_failure(reason) is False


def test_needs_clarification_null_checkpoint_stays_terminal(pila):
    """The validate_result line-2347 sibling case — checkpoint_path is
    null entirely — was always terminal and stays terminal."""
    reason = ("status='needs-clarification' but checkpoint_path is null "
              "— the work-in-progress must survive the question")
    assert pila._retryable_failure(reason) is False


def test_checkpoint_missing_marker_matches_validate_result_string(pila):
    """Coupling test for the new retryable branch — same spirit as
    test_retryable_markers_match_check_strings above.

    The new `reason.startswith("checkpoint_path '")` arm in
    `_retryable_failure` must match the exact string `validate_result`
    emits for the incomplete-handoff case. If `validate_result`'s
    wording changes, this test fails and forces the retry classifier
    to be updated in the same change."""
    validate_src = inspect.getsource(pila.validate_result)
    # Look for the exact f-string format that produces the line-2314
    # message. The literal characters `checkpoint_path '` (with the
    # trailing single quote) are what `_retryable_failure` keys on.
    assert "checkpoint_path '{cp}' does not exist on disk" in validate_src, (
        "validate_result no longer emits the incomplete-handoff "
        "'checkpoint_path' format that _retryable_failure depends on. "
        "Update both in the same change."
    )


# --- the coupling test ----------------------------------------------------

def test_retryable_markers_match_check_strings(pila):
    """The substring markers in _retryable_failure must appear verbatim
    in the source of the functions that emit the corresponding error
    strings. If a check function's wording changes, this test fails and
    forces the retry classifier to be updated in the same change."""
    source = PILA_PY.read_text()

    # Extract the markers from _retryable_failure's source so the test
    # finds whichever markers the function currently declares.
    retryable_src = inspect.getsource(pila._retryable_failure)
    markers_match = re.search(
        r"retryable_markers\s*=\s*\(([^)]+)\)", retryable_src, re.DOTALL
    )
    assert markers_match, "could not locate retryable_markers tuple in source"
    markers = re.findall(r'"([^"]+)"', markers_match.group(1))
    assert markers, "no markers extracted from retryable_markers tuple"

    # Locate check_branch_has_commits' source and confirm its error
    # string contains one of the markers.
    cbc_src = inspect.getsource(pila.check_branch_has_commits)
    assert any(m in cbc_src for m in markers), (
        f"check_branch_has_commits emits no string matching any of "
        f"{markers!r} — the retry classifier would not treat its failure "
        f"as retryable."
    )

    # The dirty-worktree check is inline in settle_subtask, not its own
    # function. Find it in the pila.py source text and confirm one
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


def test_coupling_specifics(pila):
    """Sanity check: the two known coupled pairs are still in force.
    Looser than test_retryable_markers_match_check_strings but reads
    more clearly when this test alone fails."""
    cbc_src = inspect.getsource(pila.check_branch_has_commits)
    assert "no commits ahead of the run" in cbc_src, (
        "check_branch_has_commits no longer emits the "
        "'no commits ahead of the run' marker"
    )

    source = PILA_PY.read_text()
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
