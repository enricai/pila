"""Tests for HealState, heal_baseline, heal_apply_patch, heal_replay_patched.

Covers:
  (a) heal_baseline against 2 fake failing captures and n=3 writes state.json
      with baseline arm-results for both captures, plus 6 judge verdicts
  (b) heal_apply_patch writes one patched prompt per failing capture under
      iter-1/patched-prompts/
  (c) heal_replay_patched updates state.json with an iteration record
  (d) HealState round-trips via save/load
"""
from __future__ import annotations

import asyncio
import json
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

_CAPS = {
    "worker_timeout_sec": 60,
    "max_total_workers": 99,
    "max_parallel": 4,
}

_MODELS = {
    "judge": "opus",
}

_CALL_TYPES = ["classifier", "planner"]


def _make_failing_records(n: int = 2) -> list[dict]:
    """Create n synthetic failing capture records."""
    records = []
    for i in range(n):
        records.append({
            "call_id": f"fail-aaaa-{i:012d}",
            "run_id": "test-heal-run",
            "call_type": _CALL_TYPES[i % len(_CALL_TYPES)],
            "model": "opus",
            "system_prompt": f"Original system prompt for record {i}. ANCHOR_POINT_HERE.",
            "user_content": f"User input for record {i}.",
            "response_content": json.dumps({"categories": ["bug-fixing"]}),
            "parsed_ok": False,
            "input_tokens": 200,
            "output_tokens": 50,
            "latency_ms": 1000,
            "success": False,
            "ts": "2026-01-01T00:00:00.000Z",
        })
    return records


def _make_state(pila, run_dir: Path):
    """Minimal State-alike for heal tests."""
    st = pila.State.__new__(pila.State)
    st.run_id = "test-heal-run"
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


def _patch_invoke_for_judge(pila, monkeypatch, judge_envelope=_JUDGE_ENVELOPE):
    """Patch pila._invoke to return the judge envelope."""
    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return judge_envelope

    monkeypatch.setattr(pila, "_invoke", fake_invoke)


def _patch_replay_and_judge(pila, monkeypatch):
    """Patch both replay_capture and _invoke (for judge) without network I/O."""
    async def fake_replay(record, *, override_system_prompt=None, cwd=None):
        return (_REPLAY_ENVELOPE, {"categories": ["bug-fixing"]})

    monkeypatch.setattr(pila, "replay_capture", fake_replay)

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return _JUDGE_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)


# ---------------------------------------------------------------------------
# Criterion (d): HealState round-trips via save/load
# ---------------------------------------------------------------------------

def test_heal_state_save_load_roundtrip(pila, tmp_path):
    """HealState.save() + load() preserves all fields."""
    heal_dir = tmp_path / "heal"
    hs = pila.HealState(heal_dir, "classifier")
    hs.failing_samples = [{"call_id": "abc", "call_type": "classifier"}]
    hs.baseline = {"abc": {"pass_rate": 0.33, "verdicts": []}}
    hs.history = [{"iter_n": 1, "pass_rate": 0.5, "scores": {}}]
    hs.best_so_far = {"pass_rate": 0.5, "iter_n": 1}

    hs.save()

    state_path = heal_dir / "classifier" / "state.json"
    assert state_path.exists(), "state.json not written after save()"

    hs2 = pila.HealState(heal_dir, "classifier")
    loaded = hs2.load()
    assert loaded, "load() returned False despite state.json existing"
    assert hs2.failing_samples == hs.failing_samples
    assert hs2.baseline == hs.baseline
    assert hs2.history == hs.history
    assert hs2.best_so_far == hs.best_so_far


def test_heal_state_save_is_atomic(pila, tmp_path):
    """save() writes via a temp file then replaces (atomic)."""
    heal_dir = tmp_path / "heal"
    hs = pila.HealState(heal_dir, "planner")
    hs.save()
    state_path = heal_dir / "planner" / "state.json"
    assert state_path.exists()
    # Temp file must be gone after save.
    tmp_path_candidate = state_path.with_suffix(".tmp")
    assert not tmp_path_candidate.exists(), ".tmp file leaked after save()"


def test_heal_state_load_missing(pila, tmp_path):
    """load() returns False when no state.json exists."""
    heal_dir = tmp_path / "heal"
    hs = pila.HealState(heal_dir, "classifier")
    assert not hs.load()


# ---------------------------------------------------------------------------
# Criterion (a): heal_baseline writes state.json + 6 judge verdicts
# ---------------------------------------------------------------------------

def test_heal_baseline_writes_state_json(pila, tmp_path, monkeypatch):
    """heal_baseline with 2 failing captures, n=3 writes state.json."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    hs = asyncio.run(
        pila.heal_baseline("classifier", records, 3, heal_dir, _CAPS, st, _MODELS)
    )

    state_path = heal_dir / "classifier" / "state.json"
    assert state_path.exists(), "state.json not written by heal_baseline"

    loaded = json.loads(state_path.read_text())
    assert "failing_samples" in loaded
    assert "baseline" in loaded
    assert len(loaded["failing_samples"]) == 2


def test_heal_baseline_baseline_covers_both_samples(pila, tmp_path, monkeypatch):
    """heal_baseline baseline dict contains entries for both call_ids."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    hs = asyncio.run(
        pila.heal_baseline("classifier", records, 3, heal_dir, _CAPS, st, _MODELS)
    )

    assert len(hs.baseline) == 2, f"Expected 2 baseline entries, got {len(hs.baseline)}"
    for rec in records:
        call_id = rec["call_id"]
        assert call_id in hs.baseline, f"Baseline missing entry for {call_id}"
        entry = hs.baseline[call_id]
        assert "pass_rate" in entry
        assert "verdicts" in entry
        assert len(entry["verdicts"]) == 3, (
            f"Expected 3 verdicts for {call_id}, got {len(entry['verdicts'])}")


def test_heal_baseline_writes_6_verdict_files(pila, tmp_path, monkeypatch):
    """heal_baseline with 2 records and n=3 writes exactly 6 verdict files."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    asyncio.run(
        pila.heal_baseline("classifier", records, 3, heal_dir, _CAPS, st, _MODELS)
    )

    verdicts_dir = heal_dir / "classifier" / "baseline" / "verdicts"
    verdict_files = list(verdicts_dir.glob("*.json"))
    assert len(verdict_files) == 6, (
        f"Expected 6 verdict files, got {len(verdict_files)}: {verdict_files}")


def test_heal_baseline_sets_best_so_far(pila, tmp_path, monkeypatch):
    """heal_baseline sets best_so_far from the baseline pass_rate."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    hs = asyncio.run(
        pila.heal_baseline("classifier", records, 3, heal_dir, _CAPS, st, _MODELS)
    )

    assert "pass_rate" in hs.best_so_far, "best_so_far missing pass_rate"
    assert hs.best_so_far.get("iter_n") == 0, "baseline best_so_far should have iter_n=0"


def test_heal_baseline_history_is_empty(pila, tmp_path, monkeypatch):
    """heal_baseline does not write any iteration history (that's for replay)."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    hs = asyncio.run(
        pila.heal_baseline("classifier", records, 3, heal_dir, _CAPS, st, _MODELS)
    )

    assert hs.history == [], f"Expected empty history, got {hs.history}"


# ---------------------------------------------------------------------------
# Criterion (b): heal_apply_patch writes patched prompts under iter-1/
# ---------------------------------------------------------------------------

def test_heal_apply_patch_writes_two_prompt_files(pila, tmp_path):
    """heal_apply_patch writes one .txt file per failing record."""
    heal_dir = tmp_path / "heal"
    records = _make_failing_records(2)

    written = pila.heal_apply_patch(
        "classifier", 1, "REPLACEMENT_TEXT", "ANCHOR_POINT_HERE",
        heal_dir, records
    )

    assert len(written) == 2, f"Expected 2 paths, got {len(written)}"
    for rec in records:
        call_id = rec["call_id"]
        dest = heal_dir / "classifier" / "iter-1" / "patched-prompts" / f"{call_id}.txt"
        assert dest.exists(), f"Patched prompt missing: {dest}"


def test_heal_apply_patch_anchor_replacement(pila, tmp_path):
    """Patched prompt contains replacement_text where anchor was."""
    heal_dir = tmp_path / "heal"
    records = _make_failing_records(2)
    anchor = "ANCHOR_POINT_HERE"
    replacement = "REPLACED_CONTENT"

    pila.heal_apply_patch("classifier", 1, replacement, anchor, heal_dir, records)

    for rec in records:
        call_id = rec["call_id"]
        dest = heal_dir / "classifier" / "iter-1" / "patched-prompts" / f"{call_id}.txt"
        content = dest.read_text()
        assert replacement in content, f"Replacement not found in {call_id}.txt"
        assert anchor not in content, f"Anchor still present in {call_id}.txt after patch"


def test_heal_apply_patch_creates_dir(pila, tmp_path):
    """heal_apply_patch creates the patched-prompts directory if absent."""
    heal_dir = tmp_path / "heal"
    records = _make_failing_records(1)
    out_dir = heal_dir / "classifier" / "iter-1" / "patched-prompts"
    assert not out_dir.exists()

    pila.heal_apply_patch("classifier", 1, "new", "ANCHOR_POINT_HERE",
                               heal_dir, records)

    assert out_dir.exists()


# ---------------------------------------------------------------------------
# Criterion (c): heal_replay_patched updates state.json with iteration record
# ---------------------------------------------------------------------------

def _setup_for_replay(pila, tmp_path, monkeypatch, n_replays: int = 2):
    """Shared setup: run baseline then apply_patch, ready for replay."""
    run_dir = tmp_path / "run"
    heal_dir = tmp_path / "heal"
    st = _make_state(pila, run_dir)
    records = _make_failing_records(2)
    _patch_replay_and_judge(pila, monkeypatch)

    asyncio.run(
        pila.heal_baseline("classifier", records, 2, heal_dir, _CAPS, st, _MODELS)
    )
    pila.heal_apply_patch(
        "classifier", 1, "REPLACEMENT_TEXT", "ANCHOR_POINT_HERE",
        heal_dir, records
    )
    return run_dir, heal_dir, st, records


def test_heal_replay_patched_updates_history(pila, tmp_path, monkeypatch):
    """heal_replay_patched appends one iteration record to state.json history."""
    run_dir, heal_dir, st, records = _setup_for_replay(
        pila, tmp_path, monkeypatch
    )

    hs = asyncio.run(
        pila.heal_replay_patched("classifier", 1, 2, heal_dir, _CAPS, st, _MODELS)
    )

    assert len(hs.history) == 1, f"Expected 1 history entry, got {len(hs.history)}"
    entry = hs.history[0]
    assert entry["iter_n"] == 1, f"Expected iter_n=1, got {entry['iter_n']}"
    assert "pass_rate" in entry


def test_heal_replay_patched_state_persisted(pila, tmp_path, monkeypatch):
    """After heal_replay_patched, state.json on disk contains the history entry."""
    run_dir, heal_dir, st, records = _setup_for_replay(
        pila, tmp_path, monkeypatch
    )

    asyncio.run(
        pila.heal_replay_patched("classifier", 1, 2, heal_dir, _CAPS, st, _MODELS)
    )

    state_path = heal_dir / "classifier" / "state.json"
    loaded = json.loads(state_path.read_text())
    assert len(loaded["history"]) == 1
    assert loaded["history"][0]["iter_n"] == 1


def test_heal_replay_patched_best_so_far_updated(pila, tmp_path, monkeypatch):
    """best_so_far iter_n is updated when iteration improves on baseline."""
    run_dir, heal_dir, st, records = _setup_for_replay(
        pila, tmp_path, monkeypatch
    )

    # Force baseline pass_rate to 0 so iteration always improves (judge returns passed=True).
    hs_pre = pila.HealState(heal_dir, "classifier")
    hs_pre.load()
    hs_pre.best_so_far = {"pass_rate": 0.0, "iter_n": 0}
    for cid in hs_pre.baseline:
        hs_pre.baseline[cid]["pass_rate"] = 0.0
    hs_pre.save()

    hs = asyncio.run(
        pila.heal_replay_patched("classifier", 1, 2, heal_dir, _CAPS, st, _MODELS)
    )

    # Judge always returns passed=True, so pass_rate=1.0 > 0.0 → best updated.
    assert hs.best_so_far["iter_n"] == 1, (
        f"best_so_far not updated: {hs.best_so_far}")


# ---------------------------------------------------------------------------
# Importability checks
# ---------------------------------------------------------------------------

def test_heal_symbols_importable(pila):
    """HealState, heal_baseline, heal_apply_patch, heal_replay_patched must exist."""
    assert hasattr(pila, "HealState"), "HealState not in pila"
    assert hasattr(pila, "heal_baseline"), "heal_baseline not in pila"
    assert hasattr(pila, "heal_apply_patch"), "heal_apply_patch not in pila"
    assert hasattr(pila, "heal_replay_patched"), "heal_replay_patched not in pila"
    assert callable(pila.HealState)
    assert callable(pila.heal_baseline)
    assert callable(pila.heal_apply_patch)
    assert callable(pila.heal_replay_patched)
