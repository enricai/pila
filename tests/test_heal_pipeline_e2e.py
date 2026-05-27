"""End-to-end smoke test: telemetry → judge → heal pipeline.

Drives the full pipeline against a fixture captures NDJSON file:
  (a) Prepare 3 synthetic capture records in calls.ndjson.
  (b) Run phase_judge with a stubbed _invoke: 2 records fail, 1 passes.
  (c) Run phase_heal with a stubbed request_patch_fn that returns a patch
      flipping one failing sample to pass (pass_rate=0.5), while the other
      remains failing; replay + judge are also stubbed.
  (d) Assert the heal state.json shows pass_rate=0.5 in best_so_far,
      history has ≥1 iteration, and healing-<call_type>.md is rendered
      with the best patch text.

Exercises feat-003..feat-010 together. Catches regressions when any one
subtask drifts.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# shared constants
# ---------------------------------------------------------------------------

_CALL_TYPE = "classifier"
_ANCHOR = "ORIGINAL_ANCHOR_TEXT_HERE"

# Call IDs must differ in their first 8 characters because judge_capture()
# builds sid as `judge-{call_type}-{call_id[:8]}` — the fake _invoke stubs
# key on that sid to determine pass/fail per record.
_ID_PASS = "passrec0-0000-0000-0000-000000000001"
_ID_FAIL_A = "failreca-0000-0000-0000-000000000002"   # flips to pass after patch
_ID_FAIL_B = "failrecb-0000-0000-0000-000000000003"   # stays failing

_CAPS = {
    "worker_timeout_sec": 60,
    "max_total_workers": 99,
    "max_parallel": 4,
}

_MODELS = {
    "judge": "opus",
    "validator": "opus",
}

# Judge envelopes: one passes, two fail.
_JUDGE_PASS_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 1,
    "total_cost_usd": 0.002,
    "is_error": False,
    "terminal_reason": "completed",
    "result": "{}",
    "structured_output": {
        "passed": True,
        "dimensions": {
            "schema_ok": True,
            "factual_ok": True,
            "hallucination_ok": True,
        },
        "rationale": "Well-formed response.",
        "suggested_fixes": [],
    },
    "usage": {"input_tokens": 200, "output_tokens": 50},
}

_JUDGE_FAIL_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 1,
    "total_cost_usd": 0.002,
    "is_error": False,
    "terminal_reason": "completed",
    "result": "{}",
    "structured_output": {
        "passed": False,
        "dimensions": {
            "schema_ok": False,
            "factual_ok": True,
            "hallucination_ok": True,
        },
        "rationale": "Schema validation failed.",
        "suggested_fixes": ["Fix the schema."],
    },
    "usage": {"input_tokens": 200, "output_tokens": 50},
}

_REPLAY_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 1,
    "total_cost_usd": 0.001,
    "is_error": False,
    "terminal_reason": "completed",
    "result": json.dumps({"categories": ["bug-fixing"]}),
    "structured_output": {"categories": ["bug-fixing"]},
    "usage": {"input_tokens": 100, "output_tokens": 20},
}

# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _make_capture_records() -> list[dict]:
    """3 synthetic capture records: 2 failing (call_type=classifier), 1 passing.

    Call IDs are chosen so their first 8 characters are all distinct — this
    matters because judge_capture() builds `sid = judge-{call_type}-{call_id[:8]}`
    and our fake _invoke stubs key on that sid to produce per-record verdicts.
    """
    return [
        {
            "call_id": _ID_PASS,
            "run_id": "e2e-test-run",
            "call_type": _CALL_TYPE,
            "model": "opus",
            "system_prompt": f"System prompt for passing record. {_ANCHOR}",
            "user_content": "User input for passing record.",
            "response_content": json.dumps({"categories": ["bug-fixing"]}),
            "parsed_ok": True,
            "input_tokens": 200,
            "output_tokens": 50,
            "latency_ms": 800,
            "success": True,
            "ts": "2026-01-01T00:00:00.000Z",
        },
        {
            "call_id": _ID_FAIL_A,
            "run_id": "e2e-test-run",
            "call_type": _CALL_TYPE,
            "model": "opus",
            "system_prompt": f"System prompt for failing record A. {_ANCHOR}",
            "user_content": "User input for failing record A.",
            "response_content": "invalid json that broke the parser",
            "parsed_ok": False,
            "input_tokens": 200,
            "output_tokens": 50,
            "latency_ms": 900,
            "success": False,
            "ts": "2026-01-01T00:00:01.000Z",
        },
        {
            "call_id": _ID_FAIL_B,
            "run_id": "e2e-test-run",
            "call_type": _CALL_TYPE,
            "model": "opus",
            "system_prompt": f"System prompt for failing record B. {_ANCHOR}",
            "user_content": "User input for failing record B.",
            "response_content": "another broken response",
            "parsed_ok": False,
            "input_tokens": 200,
            "output_tokens": 50,
            "latency_ms": 1100,
            "success": False,
            "ts": "2026-01-01T00:00:02.000Z",
        },
    ]


def _write_ndjson(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_state(centella, run_dir: Path):
    """Minimal State-like object for tests (no live I/O)."""
    st = centella.State.__new__(centella.State)
    st.run_id = "e2e-test-run"
    st.run_dir = run_dir
    st.path = run_dir / "state.json"
    st.data = {
        "telemetry": {"calls": 0, "cost_usd": 0.0,
                      "input_tokens": 0, "output_tokens": 0},
        "verbosity": "quiet",
        "worker_count": 0,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    st.path.write_text("{}")
    return st


# ---------------------------------------------------------------------------
# (b) phase_judge stub: call_id determines pass/fail outcome
# ---------------------------------------------------------------------------

def _make_judge_invoke():
    """Return a fake _invoke that yields pass for _ID_PASS, fail for the others.

    judge_capture() builds sid = `judge-{call_type}-{call_id[:8]}`.
    _ID_PASS[:8] == "passrec0", _ID_FAIL_A[:8] == "failreca", _ID_FAIL_B[:8]
    == "failrecb" — all distinct, so the sid uniquely identifies the record.
    """
    async def fake_invoke(cmd, cwd, timeout, sid, centella_dir, verbosity,
                          progress=None):
        if _ID_PASS[:8] in sid:
            return _JUDGE_PASS_ENVELOPE
        return _JUDGE_FAIL_ENVELOPE

    return fake_invoke


# ---------------------------------------------------------------------------
# (c) heal-phase stubs
# ---------------------------------------------------------------------------

def _make_heal_judge_invoke(n_baseline_calls: int = 2):
    """For heal replays: baseline calls all fail (first n_baseline_calls * 2
    invocations). After that, _ID_FAIL_A passes and _ID_FAIL_B stays failing
    → pass_rate = 0.5 overall in each patched iteration.

    n_baseline_calls should equal n (the replays-per-sample count) * 2 records.
    Default 2 = n=1 replays × 2 failing records.

    judge_capture() builds sid = `judge-{call_type}-{call_id[:8]}`.
    _ID_FAIL_A[:8] == "failreca", _ID_FAIL_B[:8] == "failrecb".
    """
    call_counter = [0]

    async def fake_invoke(cmd, cwd, timeout, sid, centella_dir, verbosity,
                          progress=None):
        call_counter[0] += 1
        # First n_baseline_calls are the unpatched baseline — all fail.
        if call_counter[0] <= n_baseline_calls:
            return _JUDGE_FAIL_ENVELOPE
        # Subsequent calls are patched iterations: _FAIL_A passes, _FAIL_B fails.
        if _ID_FAIL_B[:8] in sid:
            return _JUDGE_FAIL_ENVELOPE
        return _JUDGE_PASS_ENVELOPE

    return fake_invoke


def _request_patch_stub(hs, iter_n: int):
    """Stub request_patch: returns a fixed anchor+replacement patch."""
    return (_ANCHOR, f"PATCHED_CONTENT_iter{iter_n}")


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------

class TestHealPipelineE2E:
    """Full pipeline: calls.ndjson → phase_judge → phase_heal → artefacts."""

    def _run(self, centella, tmp_path: Path):
        """Execute the pipeline and return (run_dir, judge_dir, heal_dir, records)."""
        run_dir = tmp_path / "run"
        judge_dir = tmp_path / "judge-results"
        heal_dir = tmp_path / "healing"

        records = _make_capture_records()
        _write_ndjson(run_dir / "calls.ndjson", records)
        st = _make_state(centella, run_dir)
        return run_dir, judge_dir, heal_dir, records, st

    def test_calls_ndjson_written_with_3_records(self, centella, tmp_path,
                                                  monkeypatch):
        """Fixture NDJSON has 3 records, each valid JSON with required fields."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)
        capture_path = run_dir / "calls.ndjson"
        assert capture_path.exists(), "calls.ndjson not written"
        lines = [l for l in capture_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 records, got {len(lines)}"
        for line in lines:
            rec = json.loads(line)
            for field in ("call_id", "call_type", "system_prompt",
                          "response_content", "success"):
                assert field in rec, f"Record missing field: {field}"

    def test_phase_judge_produces_index_with_3_entries(self, centella, tmp_path,
                                                        monkeypatch):
        """phase_judge writes INDEX.json listing all 3 call_ids."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)
        monkeypatch.setattr(centella, "_invoke",
                            _make_judge_invoke())

        result = asyncio.run(
            centella.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
        )

        assert result["judged"] == 3, (
            f"Expected 3 judged records, got {result['judged']}")
        index_path = judge_dir / "INDEX.json"
        assert index_path.exists(), "INDEX.json not written"
        index = json.loads(index_path.read_text())
        assert len(index) == 3, f"Expected 3 index entries, got {len(index)}"
        judged_ids = {e["call_id"] for e in index}
        expected_ids = {r["call_id"] for r in records}
        assert judged_ids == expected_ids

    def test_phase_judge_marks_correct_pass_fail(self, centella, tmp_path,
                                                  monkeypatch):
        """INDEX.json has passed=True for the first record, False for the others."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)
        monkeypatch.setattr(centella, "_invoke",
                            _make_judge_invoke())

        asyncio.run(
            centella.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
        )

        index = json.loads((judge_dir / "INDEX.json").read_text())
        by_id = {e["call_id"]: e for e in index}
        assert by_id[records[0]["call_id"]]["passed"] is True, (
            "First record (pass_id) should be marked passed")
        assert by_id[records[1]["call_id"]]["passed"] is False, (
            "Second record should be marked failed")
        assert by_id[records[2]["call_id"]]["passed"] is False, (
            "Third record should be marked failed")

    def test_phase_judge_verdict_files_exist(self, centella, tmp_path,
                                              monkeypatch):
        """One <call_id>.json verdict file per record is written."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)
        monkeypatch.setattr(centella, "_invoke",
                            _make_judge_invoke())

        asyncio.run(
            centella.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
        )

        for rec in records:
            vf = judge_dir / f"{rec['call_id']}.json"
            assert vf.exists(), f"Verdict file missing: {vf}"
            verdict = json.loads(vf.read_text())
            assert "passed" in verdict
            assert "dimensions" in verdict
            assert "rationale" in verdict

    def test_phase_heal_state_json_pass_rate_05(self, centella, tmp_path,
                                                  monkeypatch):
        """After phase_heal, state.json best_so_far.pass_rate == 0.5.

        Setup: 2 failing records (_ID_FAIL_A, _ID_FAIL_B). The patch flips
        _ID_FAIL_A to pass while _ID_FAIL_B stays failing → pass_rate = 0.5.
        """
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)

        # Only the 2 failing records go to phase_heal.
        failing = [r for r in records if not r["success"]]
        assert len(failing) == 2, "Test setup error: expected 2 failing records"

        async def fake_replay(record, *, override_system_prompt=None, cwd=None):
            return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

        monkeypatch.setattr(centella, "replay_capture", fake_replay)
        monkeypatch.setattr(centella, "_invoke",
                            _make_heal_judge_invoke())

        # Config: enough iterations to record ≥1 history entry but
        # PLATEAUED quickly (plateau_window=2 entries, delta<0.03).
        config = {
            "success_threshold": 0.99,   # very high so SUCCESS won't fire
            "max_iterations": 5,
            "plateau_window": 2,
            "plateau_delta": 0.03,
        }

        verdict = asyncio.run(
            centella.phase_heal(
                _CALL_TYPE, failing, heal_dir, _CAPS, st, _MODELS,
                _request_patch_stub, n=1, config=config,
            )
        )

        state_path = heal_dir / _CALL_TYPE / "state.json"
        assert state_path.exists(), f"state.json not written at {state_path}"

        state = json.loads(state_path.read_text())
        best = state.get("best_so_far", {})
        # pass_rate must be 0.5: one of two samples passes.
        assert abs(best.get("pass_rate", -1) - 0.5) < 1e-9, (
            f"Expected best_so_far.pass_rate=0.5, got {best.get('pass_rate')}")

    def test_phase_heal_history_has_at_least_one_iteration(self, centella,
                                                             tmp_path,
                                                             monkeypatch):
        """After phase_heal, state.json history has ≥1 iteration record."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)

        failing = [r for r in records if not r["success"]]

        async def fake_replay(record, *, override_system_prompt=None, cwd=None):
            return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

        monkeypatch.setattr(centella, "replay_capture", fake_replay)
        monkeypatch.setattr(centella, "_invoke",
                            _make_heal_judge_invoke())

        config = {
            "success_threshold": 0.99,
            "max_iterations": 5,
            "plateau_window": 2,
            "plateau_delta": 0.03,
        }

        asyncio.run(
            centella.phase_heal(
                _CALL_TYPE, failing, heal_dir, _CAPS, st, _MODELS,
                _request_patch_stub, n=1, config=config,
            )
        )

        state_path = heal_dir / _CALL_TYPE / "state.json"
        state = json.loads(state_path.read_text())
        assert len(state.get("history", [])) >= 1, (
            f"Expected ≥1 history entry, got: {state.get('history')}")

    def test_phase_heal_no_regressions(self, centella, tmp_path, monkeypatch):
        """best_so_far.pass_rate must be >= baseline average pass_rate.

        Since the patch flips one record to pass (0.5 overall) and the baseline
        all fail (0.0 average), there are no regressions.
        """
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)

        failing = [r for r in records if not r["success"]]

        async def fake_replay(record, *, override_system_prompt=None, cwd=None):
            return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

        monkeypatch.setattr(centella, "replay_capture", fake_replay)
        monkeypatch.setattr(centella, "_invoke",
                            _make_heal_judge_invoke())

        config = {
            "success_threshold": 0.99,
            "max_iterations": 5,
            "plateau_window": 2,
            "plateau_delta": 0.03,
        }

        asyncio.run(
            centella.phase_heal(
                _CALL_TYPE, failing, heal_dir, _CAPS, st, _MODELS,
                _request_patch_stub, n=1, config=config,
            )
        )

        state_path = heal_dir / _CALL_TYPE / "state.json"
        state = json.loads(state_path.read_text())

        baseline = state.get("baseline", {})
        baseline_avg = (
            sum(v["pass_rate"] for v in baseline.values()) / len(baseline)
            if baseline else 0.0
        )
        best_rate = state.get("best_so_far", {}).get("pass_rate", 0.0)
        assert best_rate >= baseline_avg, (
            f"Regression detected: best_so_far.pass_rate={best_rate} "
            f"< baseline_avg={baseline_avg}")

    def test_phase_heal_report_written_with_patch_text(self, centella, tmp_path,
                                                         monkeypatch):
        """healing-<call_type>.md is written and contains the best patch text."""
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)

        failing = [r for r in records if not r["success"]]

        async def fake_replay(record, *, override_system_prompt=None, cwd=None):
            return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

        monkeypatch.setattr(centella, "replay_capture", fake_replay)
        monkeypatch.setattr(centella, "_invoke",
                            _make_heal_judge_invoke())

        config = {
            "success_threshold": 0.99,
            "max_iterations": 5,
            "plateau_window": 2,
            "plateau_delta": 0.03,
        }

        asyncio.run(
            centella.phase_heal(
                _CALL_TYPE, failing, heal_dir, _CAPS, st, _MODELS,
                _request_patch_stub, n=1, config=config,
            )
        )

        report_path = heal_dir / _CALL_TYPE / f"healing-{_CALL_TYPE}.md"
        assert report_path.exists(), f"Heal report not written at {report_path}"

        content = report_path.read_text()
        assert "# Heal report" in content, "Report missing header"
        # The stub patch text contains "PATCHED_CONTENT_iter" — verify it's present.
        assert "PATCHED_CONTENT_iter" in content, (
            f"Best patch text not in report:\n{content}")

    def test_all_four_artefacts_exist(self, centella, tmp_path, monkeypatch):
        """Full pipeline check: all 4 artefact types exist with expected fields.

        Artefacts:
          1. calls.ndjson  — fixture NDJSON with 3 records
          2. judge-results/INDEX.json — 3 entries with call_id, call_type, passed
          3. healing/<call_type>/state.json — has failing_samples, baseline, history,
             best_so_far
          4. healing/<call_type>/healing-<call_type>.md — markdown report
        """
        run_dir, judge_dir, heal_dir, records, st = self._run(
            centella, tmp_path)

        # Phase judge
        monkeypatch.setattr(centella, "_invoke",
                            _make_judge_invoke())
        asyncio.run(
            centella.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
        )
        monkeypatch.undo()

        # Extract failing records (simulate what the CLI does after judging)
        index = json.loads((judge_dir / "INDEX.json").read_text())
        failing_ids = {e["call_id"] for e in index if not e["passed"]}
        failing = [r for r in records if r["call_id"] in failing_ids]

        # Phase heal
        async def fake_replay(record, *, override_system_prompt=None, cwd=None):
            return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

        monkeypatch.setattr(centella, "replay_capture", fake_replay)
        monkeypatch.setattr(centella, "_invoke",
                            _make_heal_judge_invoke())

        config = {
            "success_threshold": 0.99,
            "max_iterations": 5,
            "plateau_window": 2,
            "plateau_delta": 0.03,
        }

        asyncio.run(
            centella.phase_heal(
                _CALL_TYPE, failing, heal_dir, _CAPS, st, _MODELS,
                _request_patch_stub, n=1, config=config,
            )
        )

        # --- Artefact 1: calls.ndjson ---
        assert (run_dir / "calls.ndjson").exists(), "calls.ndjson missing"

        # --- Artefact 2: INDEX.json ---
        index_path = judge_dir / "INDEX.json"
        assert index_path.exists(), "INDEX.json missing"
        index_data = json.loads(index_path.read_text())
        for entry in index_data:
            assert "call_id" in entry and "call_type" in entry and "passed" in entry

        # --- Artefact 3: state.json ---
        state_path = heal_dir / _CALL_TYPE / "state.json"
        assert state_path.exists(), f"state.json missing at {state_path}"
        state_data = json.loads(state_path.read_text())
        for field in ("failing_samples", "baseline", "history", "best_so_far"):
            assert field in state_data, f"state.json missing field: {field}"

        # --- Artefact 4: heal report ---
        report_path = heal_dir / _CALL_TYPE / f"healing-{_CALL_TYPE}.md"
        assert report_path.exists(), f"heal report missing at {report_path}"
        content = report_path.read_text()
        assert "# Heal report" in content
