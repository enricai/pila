"""Tests for the source-of-truth validation gate in gather_answers().

Covers the validation that rejects invalid pre-supplied answers, plus the
non-TTY defer-to-file path and its hint propagation.
"""
from __future__ import annotations

import io
import json
import sys

import pytest


@pytest.fixture
def state(centella, tmp_path):
    """A fresh State pointing at a tmp_path .centella/, with a feature
    task that needs source_of_truth and pref='ask'."""
    centella_dir = tmp_path / ".centella"
    centella_dir.mkdir()
    st = centella.State(centella_dir)
    st.data = {
        "task": "test task",
        "categories": ["feature-implementation"],
        "classifier_questions": [],
        "needs_source_of_truth": True,
        "source_of_truth_pref": "ask",
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
def test_invalid_value_rejected(centella, state, capsys, bad_value):
    with pytest.raises(SystemExit) as exc:
        centella.gather_answers(state, {"source_of_truth": bad_value})
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert bad_value in err


def test_ask_rejected_as_answer(centella, state, capsys):
    """`ask` is a preference value, never an answer."""
    with pytest.raises(SystemExit) as exc:
        centella.gather_answers(state, {"source_of_truth": "ask"})
    assert exc.value.code != 0


@pytest.mark.parametrize("value", ["codebase", "research", "both"])
def test_valid_values_pass(centella, state, value):
    answers = centella.gather_answers(state, {"source_of_truth": value})
    assert answers["source_of_truth"] == value


# --- non-TTY defer path with hint ------------------------------------------

def test_defer_writes_pending_with_hint(centella, state, non_tty_stdin):
    """With pref='ask' and no supplied answer + non-TTY, gather_answers
    writes pending-questions.json with a non-null hint and exits 10."""
    with pytest.raises(SystemExit) as exc:
        centella.gather_answers(state, None)
    assert exc.value.code == centella.EXIT_NEEDS_ANSWERS

    pq = json.loads((state.path.parent / "pending-questions.json").read_text())
    assert pq["source_of_truth"] is True
    assert pq["source_of_truth_hint"]
    assert "CENTELLA_SOURCE_OF_TRUTH" in pq["source_of_truth_hint"]
    assert "centella.toml" in pq["source_of_truth_hint"]


def test_defer_hint_null_when_not_needed(centella, state, non_tty_stdin):
    """When the task does not need source_of_truth, the hint field is null."""
    state.data["needs_source_of_truth"] = False
    state.data["classifier_questions"] = [
        {"id": "q1", "question": "Is the bug intermittent?",
         "why_underivable": "user-specific"}
    ]
    with pytest.raises(SystemExit) as exc:
        centella.gather_answers(state, None)
    assert exc.value.code == centella.EXIT_NEEDS_ANSWERS

    pq = json.loads((state.path.parent / "pending-questions.json").read_text())
    assert pq["source_of_truth"] is False
    assert pq["source_of_truth_hint"] is None


# --- preference fills in source_of_truth without asking --------------------

@pytest.mark.parametrize("pref", ["codebase", "research", "both"])
def test_preset_preference_skips_question(centella, state, pref):
    """When pref is set (not 'ask'), gather_answers fills in source_of_truth
    without asking and without deferring."""
    state.data["source_of_truth_pref"] = pref
    answers = centella.gather_answers(state, None)
    assert answers["source_of_truth"] == pref


# --- --no-clarify "skips clarification entirely" (DESIGN §11) --------------

def test_no_clarify_with_preference_satisfies_sot(centella, state):
    """--no-clarify + a real preference: SoT comes from the preference, no
    defer file, no warning needed."""
    state.data["source_of_truth_pref"] = "research"
    state.data["no_clarify"] = True
    answers = centella.gather_answers(state, None)
    assert answers["source_of_truth"] == "research"


def test_no_clarify_with_ask_defaults_to_codebase(centella, state, capsys):
    """--no-clarify + pref='ask': defaults to 'codebase' rather than blocking,
    and logs a warning explaining the default."""
    state.data["no_clarify"] = True  # pref already 'ask' via fixture
    answers = centella.gather_answers(state, None)
    assert answers["source_of_truth"] == "codebase"
    out = capsys.readouterr().out
    assert "--no-clarify" in out
    assert "defaulting" in out
    # no pending-questions.json should have been written
    assert not (state.path.parent / "pending-questions.json").exists()
