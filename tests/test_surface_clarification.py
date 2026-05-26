"""Direct unit tests for surface_clarification().

The helper carries a mid-execution clarification question across a
worker boundary: TTY path captures the answer with input() and
returns True; non-TTY path writes .centella/pending-clarifications.json
and exits with EXIT_NEEDS_ANSWERS. The Pass-7 coupling test verifies
the *call site* in settle_subtask invokes this helper; these tests
verify the helper's own behavior so a refactor that breaks the JSON
shape, the answer-key naming, or the exit code is caught.

The P5-1 bug taught us that load-bearing helpers without direct
coverage are how regressions ship — coupling tests pin the call
sites, function-level tests pin what the call site actually invokes.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


def _question(qid: str = "feat-001-q1") -> dict:
    return {
        "id": qid,
        "question": "Should the new API preserve compat with v1 clients?",
        "why_underivable": "both patterns exist in src/api/v1.py and v2.py; "
                           "task description does not specify",
    }


@pytest.fixture
def state(centella, tmp_path):
    """A State pointed at a tmp per-run directory. surface_clarification
    derives the per-run dir from st.path.parent, so we need a real
    on-disk State."""
    centella_root = tmp_path / ".centella"
    run_id = "test-run-aaa111"
    (centella_root / "runs" / run_id).mkdir(parents=True)
    st = centella.State(centella_root, run_id)
    st.data = {"task": "test", "answers": {}}
    st.save()
    return st


# ----- TTY path -------------------------------------------------------------

def test_tty_path_stores_answer_under_question_id(centella, state, monkeypatch):
    """The TTY branch captures the user's input via input() and stores
    it in st.data['answers'][question['id']]. This key choice is
    load-bearing: the re-spawned implementer reads its
    _clarification_answers by the same id (see settle_subtask which
    rewrites the spec from st.data['answers'])."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "preserve compat")
    q = _question("auth-001-q1")
    result = centella.surface_clarification(
        "auth-001", q, "/tmp/checkpoint.md", state)
    assert result is True
    assert state.data["answers"]["auth-001-q1"] == "preserve compat"


def test_tty_path_strips_whitespace(centella, state, monkeypatch):
    """The .strip() in surface_clarification is the existing
    gather_answers convention. Trailing newlines from input() shouldn't
    pollute the answer downstream — pin the strip so a future
    refactor that removes it is caught."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "  break compat  \n")
    q = _question()
    centella.surface_clarification("feat-001", q, "/tmp/cp.md", state)
    assert state.data["answers"][q["id"]] == "break compat"


def test_tty_path_empty_input_stores_empty_string(centella, state, monkeypatch):
    """If the user hits enter without typing, the answer is "" after
    strip. Documented behavior: the re-spawned implementer will see
    an empty answer and re-ask (consuming one more
    subtask_continuations slot), bounded by the cap. Pin this so a
    future change that adds prompt-loop logic is a deliberate decision
    rather than a quiet drift."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    q = _question()
    centella.surface_clarification("feat-001", q, "/tmp/cp.md", state)
    assert state.data["answers"][q["id"]] == ""


def test_tty_path_persists_state_to_disk(centella, state, monkeypatch):
    """Same persistence-survives-process property as
    absorb_supplied_answers: the answer must reach state.json on disk
    so the re-spawned worker (in a fresh process) can read it."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "answer text")
    q = _question("test-q1")
    centella.surface_clarification("test-sid", q, "/tmp/cp.md", state)
    on_disk = json.loads(state.path.read_text())
    assert on_disk["answers"]["test-q1"] == "answer text"


# ----- non-TTY path ---------------------------------------------------------

def test_non_tty_writes_pending_clarifications_json(centella, state,
                                                    monkeypatch):
    """The non-TTY path's contract is the user-facing surface: the
    file must contain subtask_id, question (the full question object),
    and checkpoint_path. The wrapper layer (plugin skill / CI driver)
    reads this file to know what to ask the user. A schema change
    here without coordinated wrapper updates breaks the resume flow."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    q = _question("feat-001-q1")
    with pytest.raises(SystemExit) as exc:
        centella.surface_clarification(
            "feat-001", q, "/path/to/checkpoint.md", state)
    assert exc.value.code == centella.EXIT_NEEDS_ANSWERS

    pending_path = state.path.parent / "pending-clarifications.json"
    assert pending_path.exists()
    data = json.loads(pending_path.read_text())
    assert data["subtask_id"] == "feat-001"
    assert data["question"] == q
    assert data["checkpoint_path"] == "/path/to/checkpoint.md"


def test_non_tty_saves_state_before_exiting(centella, state, monkeypatch):
    """state.save() must run before sys.exit(). The user's resume
    re-reads state.json; if state.json reflects pre-exit state, the
    resumed run picks up correctly. If save() were skipped or moved
    after exit, mid-flight subtask progress would be lost."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    state.data["answers"]["earlier-answer"] = "from before"
    q = _question()
    with pytest.raises(SystemExit):
        centella.surface_clarification("sid", q, "/cp.md", state)
    # state.json on disk reflects the in-memory state at exit time.
    on_disk = json.loads(state.path.read_text())
    assert on_disk["answers"]["earlier-answer"] == "from before"


def test_non_tty_exit_code_is_exit_needs_answers(centella, state, monkeypatch):
    """Pin the exact exit code. The wrapper layer (plugin skill / CI)
    distinguishes "needs-clarification deferred" from other failures
    by checking exit code == 10. A future change that returned a
    different code would silently break the wrapper integration."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    q = _question()
    with pytest.raises(SystemExit) as exc:
        centella.surface_clarification("sid", q, "/cp.md", state)
    assert exc.value.code == 10
    # Constant matches the literal — guard against the constant being
    # changed without the wrapper-facing contract being reviewed.
    assert centella.EXIT_NEEDS_ANSWERS == 10


def test_non_tty_does_not_modify_answers(centella, state, monkeypatch):
    """The non-TTY path defers the answer — it shouldn't pre-populate
    anything in st.data['answers']. (The answer arrives later via
    --resume --answers FILE, handled by absorb_supplied_answers.)
    Pin this so a future change that, say, writes a stub answer to
    avoid the re-ask doesn't accidentally land."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    before = dict(state.data["answers"])
    q = _question("never-asked-q1")
    with pytest.raises(SystemExit):
        centella.surface_clarification("sid", q, "/cp.md", state)
    # answers dict on disk is the SAME as before (modulo state.save
    # rewriting the file, but with the same content)
    on_disk = json.loads(state.path.read_text())
    assert on_disk["answers"] == before
    assert q["id"] not in on_disk["answers"]
