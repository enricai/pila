"""Tests for absorb_supplied_answers().

This helper closes the load-bearing P5-1 bug: prior to its addition,
`--resume --answers FILE` silently dropped the answers file because
the resume branch of orchestrate() never read `args.answers`. The
documented user flow for a non-interactive deferred-question exit
(Phase-1 classifier OR §11 mid-execution clarification) is:

  1. Initial run hits an underivable question, writes a pending-*.json
     and exits with code 10.
  2. User edits an answers file with the answer.
  3. User re-runs with `--resume --answers <file>`.

Step 3 was broken. These tests pin the fix: the helper merges supplied
answers into state, flushes them to every existing subtask spec, and
validates the input.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ----- fixtures --------------------------------------------------------------

@pytest.fixture
def centella_root(tmp_path):
    """The .centella/ root (parent of per-run dirs)."""
    cr = tmp_path / ".centella"
    cr.mkdir()
    return cr


@pytest.fixture
def centella_dir(centella_root):
    """A per-run dir with subtasks/ ready to be walked. Real runs always
    have this — the helper iterates it to flush answers into existing
    spec files. Under the new layout this is `runs/<run-id>/` under
    centella_root. exist_ok=True so the `state` fixture (which also
    constructs the same run_dir) can be a sibling fixture without
    fighting on directory creation."""
    cd = centella_root / "runs" / "test-run-aaa111"
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "subtasks").mkdir(exist_ok=True)
    return cd


@pytest.fixture
def state(centella, centella_root):
    """A State pointed at a per-run dir matching the centella_dir fixture.
    Helper saves to state.json, so we need a real on-disk State, not a
    mock."""
    run_id = "test-run-aaa111"
    (centella_root / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    (centella_root / "runs" / run_id / "subtasks").mkdir(exist_ok=True)
    st = centella.State(centella_root, run_id)
    st.data = {"task": "test", "answers": {"existing-q1": "kept"}}
    st.save()
    return st


def _args(answers_path: str | None = None) -> SimpleNamespace:
    """Minimal argparse-style namespace — only `.answers` is read."""
    return SimpleNamespace(answers=answers_path)


# ----- core merge behaviour --------------------------------------------------

def test_noop_when_no_answers_arg(centella, state, centella_dir):
    """The helper is safe to call on every run; with --answers unset
    it's a no-op."""
    before = dict(state.data["answers"])
    centella.absorb_supplied_answers(_args(None), state, centella_dir)
    assert state.data["answers"] == before


def test_merges_supplied_into_state(centella, state, centella_dir, tmp_path):
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"new-q2": "fresh value"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)
    assert state.data["answers"]["new-q2"] == "fresh value"
    # Existing keys are preserved — this is a merge, not a replace.
    assert state.data["answers"]["existing-q1"] == "kept"


def test_supplied_overrides_existing(centella, state, centella_dir, tmp_path):
    """A re-run with a corrected answer to a previously-answered
    question must take effect — that's the whole point of supplying
    fresh answers on resume."""
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"existing-q1": "corrected"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)
    assert state.data["answers"]["existing-q1"] == "corrected"


def test_state_is_persisted_to_disk(centella, state, centella_dir, tmp_path):
    """The merge is only useful if it survives this process — the
    re-spawned implementer reads from the persisted state.json."""
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"q": "value"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)
    on_disk = json.loads((centella_dir / "state.json").read_text())
    assert on_disk["answers"]["q"] == "value"


# ----- subtask spec propagation ---------------------------------------------

def test_existing_subtask_specs_get_updated(centella, state, centella_dir,
                                             tmp_path):
    """Every spec file in .centella/subtasks/ has its
    _clarification_answers field overwritten with the new answers,
    so the next implementer invocation sees them. Without this,
    answers would land in state.json but never reach the worker."""
    sub_dir = centella_dir / "subtasks"
    spec_path = sub_dir / "feat-001.json"
    spec_path.write_text(json.dumps({
        "id": "feat-001",
        "_clarification_answers": {"existing-q1": "kept"},
        "_task": "test",
    }))

    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"new-q2": "fresh"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)

    updated = json.loads(spec_path.read_text())
    # Both the old and new answers are visible in the spec.
    assert updated["_clarification_answers"]["new-q2"] == "fresh"
    assert updated["_clarification_answers"]["existing-q1"] == "kept"
    # Other spec fields are untouched.
    assert updated["id"] == "feat-001"
    assert updated["_task"] == "test"


def test_spec_propagation_iterates_all_specs(centella, state, centella_dir,
                                              tmp_path):
    sub_dir = centella_dir / "subtasks"
    for sid in ("a-1", "b-2", "c-3"):
        (sub_dir / f"{sid}.json").write_text(json.dumps({
            "id": sid, "_clarification_answers": {}}))

    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"q": "v"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)

    for sid in ("a-1", "b-2", "c-3"):
        spec = json.loads((sub_dir / f"{sid}.json").read_text())
        assert spec["_clarification_answers"]["q"] == "v"


def test_corrupted_spec_does_not_abort(centella, state, centella_dir,
                                        tmp_path):
    """A corrupted spec file is the implementer's problem to surface —
    a fresh-answer flush should not crash the orchestrator before the
    other specs are updated. Conservative skip-and-continue behavior."""
    sub_dir = centella_dir / "subtasks"
    (sub_dir / "good.json").write_text(json.dumps({"_clarification_answers": {}}))
    (sub_dir / "broken.json").write_text("not valid json {{{")

    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"q": "v"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)

    good = json.loads((sub_dir / "good.json").read_text())
    assert good["_clarification_answers"]["q"] == "v"
    # broken.json is left as-is, not corrupted further.
    assert (sub_dir / "broken.json").read_text() == "not valid json {{{"


def test_handles_missing_subtasks_directory(centella, tmp_path):
    """An early-phase resume (before phase_plan ran) has no subtasks/
    directory yet. Helper must not crash. Uses a fresh tmp_path
    structure (not the centella_dir fixture) because that fixture
    creates subtasks/ by default — the whole point here is to test
    the case where subtasks/ is absent."""
    centella_root = tmp_path / "fresh-centella"
    run_id = "test-run-no-subtasks"
    (centella_root / "runs" / run_id).mkdir(parents=True)
    # NOTE: no subtasks/ subdirectory created.
    st = centella.State(centella_root, run_id)
    st.data = {"task": "test", "answers": {}}
    st.save()
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"q": "v"}))
    centella.absorb_supplied_answers(_args(str(answers_file)), st, st.run_dir)
    assert st.data["answers"]["q"] == "v"


# ----- input validation ------------------------------------------------------

def test_missing_file_dies(centella, state, centella_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        centella.absorb_supplied_answers(
            _args("/nonexistent/path.json"), state, centella_dir)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_invalid_json_dies(centella, state, centella_dir, tmp_path, capsys):
    answers_file = tmp_path / "bad.json"
    answers_file.write_text("not valid {{{")
    with pytest.raises(SystemExit) as exc:
        centella.absorb_supplied_answers(
            _args(str(answers_file)), state, centella_dir)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not valid JSON" in err


def test_non_object_json_dies(centella, state, centella_dir, tmp_path, capsys):
    """The file must contain a JSON object, not an array or a primitive
    — otherwise the merge into st.data['answers'] would silently
    corrupt state."""
    answers_file = tmp_path / "array.json"
    answers_file.write_text(json.dumps(["a", "b"]))
    with pytest.raises(SystemExit) as exc:
        centella.absorb_supplied_answers(
            _args(str(answers_file)), state, centella_dir)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "JSON object" in err


def test_bad_source_of_truth_value_dies(centella, state, centella_dir,
                                         tmp_path, capsys):
    """source_of_truth, when supplied, must be in the validated set.
    Same gate gather_answers uses; the helper catches a typo at resume
    time rather than mid-planner."""
    answers_file = tmp_path / "bad-sot.json"
    answers_file.write_text(json.dumps({"source_of_truth": "neither"}))
    with pytest.raises(SystemExit) as exc:
        centella.absorb_supplied_answers(
            _args(str(answers_file)), state, centella_dir)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "source_of_truth" in err


def test_valid_source_of_truth_accepted(centella, state, centella_dir,
                                         tmp_path):
    answers_file = tmp_path / "sot.json"
    answers_file.write_text(json.dumps({"source_of_truth": "research",
                                         "q1": "a"}))
    centella.absorb_supplied_answers(
        _args(str(answers_file)), state, centella_dir)
    assert state.data["answers"]["source_of_truth"] == "research"
    assert state.data["answers"]["q1"] == "a"
