"""Tests for the source-of-truth validation gate in gather_answers().

Covers the validation that rejects invalid pre-supplied answers, and
the non-interactive flow where the resolved preference satisfies
source_of_truth without asking the user.
"""
from __future__ import annotations

import io
import json
import sys

import pytest


@pytest.fixture
def state(pila, tmp_path):
    """A fresh State at a tmp_path/.pila/runs/<run-id>/, with a feature
    task that needs source_of_truth and pref='both' (the new default)."""
    pila_root = tmp_path / ".pila"
    run_id = "test-run-aaa111"
    (pila_root / "runs" / run_id).mkdir(parents=True)
    st = pila.State(pila_root, run_id)
    st.data = {
        "task": "test task",
        "categories": ["feature-implementation"],
        "classifier_questions": [],
        "needs_source_of_truth": True,
        "source_of_truth_pref": "both",
    }
    return st


@pytest.fixture
def non_tty_stdin(monkeypatch):
    """Force sys.stdin to a non-TTY so gather_answers takes the defer path."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


# --- validation gate -------------------------------------------------------

@pytest.mark.parametrize("bad_value", [
    "codbase",                 # typo
    "existing-patterns",       # not in the enum
    "researched-standards",    # not in the enum
])
def test_invalid_value_rejected(pila, state, capsys, bad_value):
    with pytest.raises(SystemExit) as exc:
        pila.gather_answers(state, {"source_of_truth": bad_value})
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert bad_value in err


@pytest.mark.parametrize("value", ["codebase", "research", "both"])
def test_valid_values_pass(pila, state, value):
    answers = pila.gather_answers(state, {"source_of_truth": value})
    assert answers["source_of_truth"] == value


# --- preference fills in source_of_truth without asking --------------------

@pytest.mark.parametrize("pref", ["codebase", "research", "both"])
def test_preference_satisfies_source_of_truth(pila, state, pref):
    """gather_answers fills source_of_truth from the resolved preference,
    without prompting the user or deferring to pending-questions.json."""
    state.data["source_of_truth_pref"] = pref
    answers = pila.gather_answers(state, None)
    assert answers["source_of_truth"] == pref
    assert not (state.path.parent / "pending-questions.json").exists()


def test_default_preference_is_both(pila, state):
    """With the new default, source_of_truth is filled with 'both' when
    nothing else is specified."""
    answers = pila.gather_answers(state, None)
    assert answers["source_of_truth"] == "both"


# --- non-TTY defer path: only fires for classifier intent questions --------

def test_defer_writes_pending_for_classifier_questions(
        pila, state, non_tty_stdin):
    """When the classifier surfaced intent questions and stdin is non-TTY,
    gather_answers writes pending-questions.json and exits 10. The file
    contains only the questions; source-of-truth was already satisfied
    from the preference."""
    state.data["classifier_questions"] = [
        {"id": "q1", "question": "Is the bug intermittent?",
         "why_underivable": "user-specific"}
    ]
    with pytest.raises(SystemExit) as exc:
        pila.gather_answers(state, None)
    assert exc.value.code == pila.EXIT_NEEDS_ANSWERS

    pq = json.loads((state.path.parent / "pending-questions.json").read_text())
    assert pq == {"questions": [
        {"id": "q1", "question": "Is the bug intermittent?",
         "why_underivable": "user-specific"}
    ]}


def test_no_defer_when_no_classifier_questions(pila, state, non_tty_stdin):
    """No classifier questions + source-of-truth satisfied from preference
    → no defer file, no exit."""
    answers = pila.gather_answers(state, None)
    assert answers["source_of_truth"] == "both"
    assert not (state.path.parent / "pending-questions.json").exists()


# --- clarify default (asking is opt-in via --clarify) ---------------------

def test_default_mode_satisfies_sot_from_preference(pila, state):
    """In the default mode (no --clarify), source_of_truth still comes
    from the resolved preference. Asking is opt-in, but the preference
    still fills the answer non-interactively."""
    state.data["source_of_truth_pref"] = "research"
    state.data["clarify"] = False
    answers = pila.gather_answers(state, None)
    assert answers["source_of_truth"] == "research"
