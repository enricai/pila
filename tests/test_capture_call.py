"""Tests for per-call NDJSON telemetry capture written by claude_p().

Covers:
  - Single invocation writes exactly one JSON line to calls.ndjson with
    all required fields per IMPLEMENTATION.md §10.
  - Two sequential invocations append exactly two independently parseable
    lines with distinct call_ids.
  - A failed invocation (is_error=True) still writes a record with
    success=False and parsed_ok=False.
  - The capture file is at <run_dir>/calls.ndjson.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_state(centella, run_dir: Path):
    """Minimal State-alike enough for claude_p to write the capture file."""
    st = centella.State.__new__(centella.State)
    st.run_id = "test-run-001"
    st.run_dir = run_dir
    st.path = run_dir / "state.json"
    st.data = {"telemetry": {"calls": 0, "cost_usd": 0.0,
                             "input_tokens": 0, "output_tokens": 0},
               "verbosity": "quiet"}
    # add_telemetry saves; give it a real save path
    run_dir.mkdir(parents=True, exist_ok=True)
    st.path.write_text("{}")
    return st


_GOOD_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 3,
    "total_cost_usd": 0.012,
    "is_error": False,
    "terminal_reason": "completed",
    "result": '{"answer": 42}',
    "structured_output": {"answer": 42},
    "usage": {"input_tokens": 500, "output_tokens": 100},
}

_ERROR_ENVELOPE = {
    "type": "result",
    "subtype": "error",
    "num_turns": 1,
    "total_cost_usd": 0.001,
    "is_error": True,
    "api_error_status": 500,
    "result": None,
    "structured_output": None,
    "usage": {"input_tokens": 100, "output_tokens": 0},
}

_CAPS = {
    "worker_timeout_sec": 60,
    "max_total_workers": 99,
}

_REQUIRED_FIELDS = {
    "call_id", "run_id", "call_type", "model",
    "system_prompt", "user_content", "response_content",
    "parsed_ok", "input_tokens", "output_tokens",
    "latency_ms", "success", "ts",
}


def _run_claude_p(centella, st, envelope, monkeypatch):
    """Invoke claude_p with a stubbed _invoke returning envelope."""
    async def fake_invoke(*args, **kwargs):
        return envelope

    monkeypatch.setattr(centella, "_invoke", fake_invoke)
    # bump_workers checks max_total_workers; patch it to be trivially passing
    monkeypatch.setattr(centella.State, "bump_workers",
                        lambda self, caps: None)

    asyncio.run(centella.claude_p(
        user_prompt="test user prompt",
        system_prompt="test system prompt",
        schema_key="classifier",
        cwd=str(st.run_dir),
        allowed_tools="Read",
        max_turns=20,
        autonomous=False,
        caps=_CAPS,
        st=st,
        model="sonnet",
        sid="test-sid",
    ))


# ---------------------------------------------------------------------------
# single-call capture
# ---------------------------------------------------------------------------

def test_single_call_writes_one_ndjson_line(centella, tmp_path, monkeypatch):
    """A single claude_p invocation writes exactly one JSON line to
    calls.ndjson with all required fields."""
    run_dir = tmp_path / "runs" / "test-run-001"
    st = _make_state(centella, run_dir)

    _run_claude_p(centella, st, _GOOD_ENVELOPE, monkeypatch)

    capture_path = run_dir / "calls.ndjson"
    assert capture_path.exists(), "calls.ndjson must be created"

    lines = [l for l in capture_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"

    record = json.loads(lines[0])
    missing = _REQUIRED_FIELDS - set(record.keys())
    assert not missing, f"Missing fields: {missing}"


def test_single_call_field_values(centella, tmp_path, monkeypatch):
    """Field values in the NDJSON record match the envelope and call params."""
    run_dir = tmp_path / "runs" / "test-run-001"
    st = _make_state(centella, run_dir)

    _run_claude_p(centella, st, _GOOD_ENVELOPE, monkeypatch)

    record = json.loads((run_dir / "calls.ndjson").read_text().strip())
    assert record["run_id"] == "test-run-001"
    assert record["call_type"] == "classifier"
    assert record["model"] == "sonnet"
    assert record["system_prompt"] == "test system prompt"
    assert "test user prompt" in record["user_content"]
    assert record["parsed_ok"] is True
    assert record["success"] is True
    assert record["input_tokens"] == 500
    assert record["output_tokens"] == 100
    assert record["latency_ms"] >= 0
    # call_id must be a non-empty string (UUID v4 shape)
    assert isinstance(record["call_id"], str) and len(record["call_id"]) > 0
    # ts must end with Z (UTC)
    assert record["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# two-call append — two distinct lines, each parseable
# ---------------------------------------------------------------------------

def test_two_calls_append_two_lines(centella, tmp_path, monkeypatch):
    """Two sequential claude_p calls append exactly two independently
    parseable JSON lines with distinct call_ids."""
    run_dir = tmp_path / "runs" / "test-run-001"
    st = _make_state(centella, run_dir)

    async def fake_invoke(*args, **kwargs):
        return _GOOD_ENVELOPE

    monkeypatch.setattr(centella, "_invoke", fake_invoke)
    monkeypatch.setattr(centella.State, "bump_workers",
                        lambda self, caps: None)

    async def two_calls():
        for _ in range(2):
            await centella.claude_p(
                user_prompt="prompt",
                system_prompt="sys",
                schema_key="planner",
                cwd=str(run_dir),
                allowed_tools="Read",
                max_turns=20,
                autonomous=False,
                caps=_CAPS,
                st=st,
                model="opus",
                sid="sid-x",
            )

    asyncio.run(two_calls())

    lines = [l for l in (run_dir / "calls.ndjson").read_text().splitlines()
             if l.strip()]
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    records = [json.loads(l) for l in lines]
    # Each line is independently parseable (already asserted above via loads)
    # call_ids must be distinct
    ids = [r["call_id"] for r in records]
    assert ids[0] != ids[1], "call_ids must be distinct across invocations"


# ---------------------------------------------------------------------------
# failed-call capture — is_error=True still writes a record
# ---------------------------------------------------------------------------

def test_failed_call_still_writes_record(centella, tmp_path, monkeypatch):
    """A call that returns is_error=True writes a record with success=False
    and parsed_ok=False — the audit trail is complete even for failures."""
    run_dir = tmp_path / "runs" / "test-run-001"
    st = _make_state(centella, run_dir)

    # The error envelope causes both attempts to fail, raising WorkerError.
    async def fake_invoke(*args, **kwargs):
        return _ERROR_ENVELOPE

    monkeypatch.setattr(centella, "_invoke", fake_invoke)
    monkeypatch.setattr(centella.State, "bump_workers",
                        lambda self, caps: None)

    with pytest.raises(centella.WorkerError):
        asyncio.run(centella.claude_p(
            user_prompt="prompt",
            system_prompt="sys",
            schema_key="implementer",
            cwd=str(run_dir),
            allowed_tools="Read",
            max_turns=20,
            autonomous=False,
            caps=_CAPS,
            st=st,
            model="sonnet",
            sid="failing-sid",
        ))

    lines = [l for l in (run_dir / "calls.ndjson").read_text().splitlines()
             if l.strip()]
    # Two attempts, each written
    assert len(lines) == 2, f"Expected 2 lines (2 attempts), got {len(lines)}"
    for line in lines:
        record = json.loads(line)
        assert record["success"] is False
        assert record["parsed_ok"] is False


# ---------------------------------------------------------------------------
# capture file path
# ---------------------------------------------------------------------------

def test_capture_file_at_run_dir_calls_ndjson(centella, tmp_path, monkeypatch):
    """The capture file is at <run_dir>/calls.ndjson, not in a subdirectory."""
    run_dir = tmp_path / "my-run"
    st = _make_state(centella, run_dir)

    _run_claude_p(centella, st, _GOOD_ENVELOPE, monkeypatch)

    assert (run_dir / "calls.ndjson").exists()
    # No subdirectory was created for the capture file itself
    assert not (run_dir / "captures").exists()
