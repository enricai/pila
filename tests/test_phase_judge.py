"""Tests for phase_judge() and judge_capture() — the LLM judge phase.

Covers:
  - phase_judge() with a stubbed _invoke returning a fixed judge envelope:
    3-record NDJSON → 3 verdict files written under judge_dir/
  - INDEX.json is written listing all 3 call_ids with passed status
  - Each verdict validates against SCHEMAS["judge"] (required fields present)
  - judge invocations honour max_parallel (concurrency never exceeds cap)
  - Filtering by judge_call_types skips non-matching records
  - Empty calls.ndjson (or missing file) produces 0 verdicts
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# shared helpers / fixtures
# ---------------------------------------------------------------------------

_JUDGE_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 2,
    "total_cost_usd": 0.003,
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
        "rationale": "The response is well-formed and grounded.",
        "suggested_fixes": [],
    },
    "usage": {"input_tokens": 300, "output_tokens": 80},
}

_CAPS = {
    "worker_timeout_sec": 60,
    "max_total_workers": 99,
    "max_parallel": 4,
}

_MODELS = {
    "judge": "opus",
}

_CALL_TYPES = ["classifier", "planner", "implementer"]


def _make_records(n: int = 3) -> list[dict]:
    """Create n synthetic capture records with distinct call_ids."""
    records = []
    for i in range(n):
        records.append({
            "call_id": f"aaaa-bbbb-cccc-dddd-{i:012d}",
            "run_id": "test-run-judge",
            "call_type": _CALL_TYPES[i % len(_CALL_TYPES)],
            "model": "opus",
            "system_prompt": f"System prompt for record {i}.",
            "user_content": f"User content for record {i}.",
            "response_content": json.dumps({"categories": ["bug-fixing"]}),
            "parsed_ok": True,
            "input_tokens": 200,
            "output_tokens": 50,
            "latency_ms": 1000,
            "success": True,
            "ts": "2026-01-01T00:00:00.000Z",
        })
    return records


def _write_ndjson(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_state(pila, run_dir: Path):
    """Minimal State-alike for judge tests."""
    st = pila.State.__new__(pila.State)
    st.run_id = "test-run-judge"
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


def _patch_invoke(pila, monkeypatch, envelope=_JUDGE_ENVELOPE):
    """Patch pila._invoke to return envelope without network I/O."""
    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return envelope

    monkeypatch.setattr(pila, "_invoke", fake_invoke)


# ---------------------------------------------------------------------------
# Criterion 5: 3 verdicts written for 3-record NDJSON
# ---------------------------------------------------------------------------

def test_phase_judge_writes_three_verdict_files(pila, tmp_path, monkeypatch):
    """phase_judge() with 3-record NDJSON writes 3 verdict files and an INDEX."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    records = _make_records(3)
    _write_ndjson(run_dir / "calls.ndjson", records)
    _patch_invoke(pila, monkeypatch)

    result = asyncio.run(
        pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
    )

    assert result["judged"] == 3, f"Expected 3, got {result['judged']}"
    for rec in records:
        vf = judge_dir / f"{rec['call_id']}.json"
        assert vf.exists(), f"Verdict file missing: {vf}"


def test_phase_judge_index_lists_all_call_ids(pila, tmp_path, monkeypatch):
    """INDEX.json exists and lists all 3 judged call_ids."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    records = _make_records(3)
    _write_ndjson(run_dir / "calls.ndjson", records)
    _patch_invoke(pila, monkeypatch)

    asyncio.run(pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS))

    index_path = judge_dir / "INDEX.json"
    assert index_path.exists(), "INDEX.json not written"
    index = json.loads(index_path.read_text())
    assert isinstance(index, list), "INDEX.json must be a JSON array"
    assert len(index) == 3, f"Expected 3 index entries, got {len(index)}"
    judged_ids = {e["call_id"] for e in index}
    expected_ids = {r["call_id"] for r in records}
    assert judged_ids == expected_ids, (
        f"INDEX call_ids mismatch: {judged_ids} vs {expected_ids}")


def test_phase_judge_verdicts_validate_against_judge_schema(
        pila, tmp_path, monkeypatch):
    """Each verdict file has all required SCHEMAS['judge'] fields."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    records = _make_records(3)
    _write_ndjson(run_dir / "calls.ndjson", records)
    _patch_invoke(pila, monkeypatch)

    asyncio.run(pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS))

    required_fields = {"passed", "dimensions", "rationale", "suggested_fixes"}
    dim_fields = {"schema_ok", "factual_ok", "hallucination_ok"}

    for rec in records:
        vf = judge_dir / f"{rec['call_id']}.json"
        verdict = json.loads(vf.read_text())
        missing = required_fields - set(verdict.keys())
        assert not missing, f"Verdict {rec['call_id']} missing fields: {missing}"
        assert isinstance(verdict["passed"], bool), (
            f"passed must be bool, got {type(verdict['passed'])}")
        dims = verdict["dimensions"]
        assert isinstance(dims, dict), "dimensions must be a dict"
        dim_missing = dim_fields - set(dims.keys())
        assert not dim_missing, (
            f"Verdict {rec['call_id']} missing dimension fields: {dim_missing}")
        assert isinstance(verdict["rationale"], str)
        assert isinstance(verdict["suggested_fixes"], list)


def test_phase_judge_index_contains_passed_field(pila, tmp_path, monkeypatch):
    """Each INDEX.json entry has call_id, call_type, and passed fields."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    records = _make_records(3)
    _write_ndjson(run_dir / "calls.ndjson", records)
    _patch_invoke(pila, monkeypatch)

    asyncio.run(pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS))

    index = json.loads((judge_dir / "INDEX.json").read_text())
    for entry in index:
        assert "call_id" in entry, "INDEX entry missing call_id"
        assert "call_type" in entry, "INDEX entry missing call_type"
        assert "passed" in entry, "INDEX entry missing passed"
        assert isinstance(entry["passed"], bool)


# ---------------------------------------------------------------------------
# Criterion 6: max_parallel semaphore is honoured
# ---------------------------------------------------------------------------

def test_phase_judge_honours_max_parallel(pila, tmp_path, monkeypatch):
    """Concurrent judge invocations never exceed caps['max_parallel']."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    # Use a small cap so the test is meaningful
    caps = dict(_CAPS, max_parallel=2)
    st = _make_state(pila, run_dir)

    # Create more records than the cap
    records = _make_records(6)
    _write_ndjson(run_dir / "calls.ndjson", records)

    concurrent_count = [0]
    peak_concurrent = [0]

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        concurrent_count[0] += 1
        if concurrent_count[0] > peak_concurrent[0]:
            peak_concurrent[0] = concurrent_count[0]
        # Yield to let other coroutines start, simulating real concurrency.
        await asyncio.sleep(0)
        concurrent_count[0] -= 1
        return _JUDGE_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    asyncio.run(pila.phase_judge(run_dir, judge_dir, caps, st, _MODELS))

    assert peak_concurrent[0] <= caps["max_parallel"], (
        f"Peak concurrent ({peak_concurrent[0]}) exceeded max_parallel "
        f"({caps['max_parallel']})")


# ---------------------------------------------------------------------------
# Filtering by judge_call_types
# ---------------------------------------------------------------------------

def test_phase_judge_filters_by_call_types(pila, tmp_path, monkeypatch):
    """When judge_call_types is given, only matching records are judged."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)

    # 2 classifier + 1 planner records
    records = [
        {**_make_records(1)[0], "call_id": "id-cls-1", "call_type": "classifier"},
        {**_make_records(1)[0], "call_id": "id-cls-2", "call_type": "classifier"},
        {**_make_records(1)[0], "call_id": "id-pln-1", "call_type": "planner"},
    ]
    _write_ndjson(run_dir / "calls.ndjson", records)
    _patch_invoke(pila, monkeypatch)

    result = asyncio.run(
        pila.phase_judge(
            run_dir, judge_dir, _CAPS, st, _MODELS,
            judge_call_types=["classifier"],
        )
    )

    assert result["judged"] == 2, (
        f"Expected 2 classifier verdicts, got {result['judged']}")
    assert (judge_dir / "id-cls-1.json").exists()
    assert (judge_dir / "id-cls-2.json").exists()
    assert not (judge_dir / "id-pln-1.json").exists()


# ---------------------------------------------------------------------------
# Edge cases: missing / empty calls.ndjson
# ---------------------------------------------------------------------------

def test_phase_judge_missing_ndjson(pila, tmp_path, monkeypatch):
    """phase_judge() with no calls.ndjson returns 0 judged and empty index."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    _patch_invoke(pila, monkeypatch)

    result = asyncio.run(
        pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
    )

    assert result["judged"] == 0
    assert result["index"] == []
    # INDEX.json should not be created for an empty run
    assert not (judge_dir / "INDEX.json").exists()


def test_phase_judge_empty_ndjson(pila, tmp_path, monkeypatch):
    """phase_judge() with an empty calls.ndjson (no records) returns 0 judged."""
    run_dir = tmp_path / "run"
    judge_dir = tmp_path / "judge-out"
    st = _make_state(pila, run_dir)
    _write_ndjson(run_dir / "calls.ndjson", [])
    _patch_invoke(pila, monkeypatch)

    result = asyncio.run(
        pila.phase_judge(run_dir, judge_dir, _CAPS, st, _MODELS)
    )

    assert result["judged"] == 0


# ---------------------------------------------------------------------------
# Importability check
# ---------------------------------------------------------------------------

def test_phase_judge_importable(pila):
    """phase_judge and judge_capture must be importable from pila."""
    assert hasattr(pila, "phase_judge"), (
        "phase_judge is not defined in orchestrator/pila.py")
    assert callable(pila.phase_judge)
    assert hasattr(pila, "judge_capture"), (
        "judge_capture is not defined in orchestrator/pila.py")
    assert callable(pila.judge_capture)


def test_judge_schema_in_schemas(pila):
    """SCHEMAS['judge'] must exist with required fields."""
    assert "judge" in pila.SCHEMAS, "SCHEMAS['judge'] not found"
    schema = pila.SCHEMAS["judge"]
    required = set(schema.get("required", []))
    expected = {"passed", "dimensions", "rationale", "suggested_fixes"}
    assert expected <= required, (
        f"SCHEMAS['judge'] missing required fields: {expected - required}")
    props = schema.get("properties", {})
    assert "passed" in props
    assert "dimensions" in props
    dims = props["dimensions"].get("properties", {})
    assert {"schema_ok", "factual_ok", "hallucination_ok"} <= set(dims.keys()), (
        f"dimensions missing fields, got: {set(dims.keys())}")
