"""Tests for replay_capture() — the primitive for judge and heal-loop replays.

Covers:
  - Arguments passed to claude_p match the captured record's fields.
  - override_system_prompt replaces system_prompt end-to-end.
  - A replay call does not write to any calls.ndjson (no capture pollution).
  - Return value is (envelope, structured_output) 2-tuple.
  - replay_capture is importable from the pila module.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_GOOD_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "num_turns": 2,
    "total_cost_usd": 0.005,
    "is_error": False,
    "terminal_reason": "completed",
    "result": '{"categories": ["bug-fixing"]}',
    "structured_output": {"categories": ["bug-fixing"]},
    "usage": {"input_tokens": 200, "output_tokens": 50},
}

_CAPTURE_RECORD = {
    "call_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
    "run_id": "fix-some-bug-abc123",
    "call_type": "classifier",
    "model": "opus",
    "system_prompt": "You are the original classifier system prompt.",
    "user_content": "TASK:\nFix the login bug.\n\nClassify it.",
    "response_content": '{"categories": ["bug-fixing"]}',
    "parsed_ok": True,
    "input_tokens": 200,
    "output_tokens": 50,
    "latency_ms": 1234,
    "success": True,
    "ts": "2026-01-01T00:00:00.000Z",
}


def _stub_invoke(pila, monkeypatch, envelope=_GOOD_ENVELOPE):
    """Patch pila._invoke to return envelope; return captured call_args list."""
    captured = []

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        captured.append({"cmd": cmd, "cwd": cwd})
        return envelope

    monkeypatch.setattr(pila, "_invoke", fake_invoke)
    return captured


# ---------------------------------------------------------------------------
# Criterion 1: args match capture fields
# ---------------------------------------------------------------------------

def test_args_match_capture_fields(pila, tmp_path, monkeypatch):
    """replay_capture passes system_prompt, user_content, call_type→schema_key,
    and model from the capture record through to claude_p / _invoke."""
    collected_cmd: list[list[str]] = []

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        collected_cmd.append(list(cmd))
        return _GOOD_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    asyncio.run(pila.replay_capture(_CAPTURE_RECORD))

    assert collected_cmd, "fake_invoke was never called"
    cmd = collected_cmd[0]

    # user_content is the -p argument (second element after 'claude -p')
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    user_arg = cmd[2]
    assert "Fix the login bug" in user_arg, (
        f"user_content not in -p arg: {user_arg!r}")

    # system_prompt is passed via --append-system-prompt
    assert "--append-system-prompt" in cmd
    sys_idx = cmd.index("--append-system-prompt")
    assert cmd[sys_idx + 1] == "You are the original classifier system prompt."

    # model is passed via --model
    assert "--model" in cmd
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "opus"

    # schema_key → --json-schema must embed the classifier schema
    assert "--json-schema" in cmd
    schema_idx = cmd.index("--json-schema")
    schema_str = cmd[schema_idx + 1]
    schema = json.loads(schema_str)
    # classifier schema has "categories" property
    assert "categories" in schema.get("properties", {}), (
        f"schema_key 'classifier' not reflected in --json-schema: {schema_str}")


# ---------------------------------------------------------------------------
# Criterion 2: override_system_prompt is plumbed through
# ---------------------------------------------------------------------------

def test_override_system_prompt(pila, tmp_path, monkeypatch):
    """When override_system_prompt is supplied, it replaces the captured
    system_prompt in the invocation."""
    collected_cmd: list[list[str]] = []

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        collected_cmd.append(list(cmd))
        return _GOOD_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    override = "PATCHED: use a different classifier strategy."
    asyncio.run(pila.replay_capture(
        _CAPTURE_RECORD,
        override_system_prompt=override,
    ))

    assert collected_cmd, "fake_invoke was never called"
    cmd = collected_cmd[0]

    assert "--append-system-prompt" in cmd
    sys_idx = cmd.index("--append-system-prompt")
    actual_sys = cmd[sys_idx + 1]
    assert actual_sys == override, (
        f"override_system_prompt not plumbed through; got: {actual_sys!r}")
    # Original prompt must NOT appear
    assert "You are the original classifier system prompt." not in actual_sys


# ---------------------------------------------------------------------------
# Criterion 3: no calls.ndjson written (no capture pollution)
# ---------------------------------------------------------------------------

def test_replay_does_not_pollute_captures(pila, tmp_path, monkeypatch):
    """replay_capture must not write to any calls.ndjson file — replays must
    not pollute the captures stream."""

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return _GOOD_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    # Run replay with cwd set to tmp_path so if any files are written they
    # land there where we can detect them.
    asyncio.run(pila.replay_capture(
        _CAPTURE_RECORD,
        cwd=str(tmp_path),
    ))

    # No calls.ndjson anywhere under tmp_path
    ndjson_files = list(tmp_path.rglob("calls.ndjson"))
    assert not ndjson_files, (
        f"calls.ndjson was written during replay: {ndjson_files}")


def test_replay_does_not_modify_existing_capture_file(pila, tmp_path,
                                                       monkeypatch):
    """If a calls.ndjson already exists (from a prior live run), replay must
    leave it unmodified."""
    existing = tmp_path / "calls.ndjson"
    original_content = '{"call_id":"existing"}\n'
    existing.write_text(original_content)

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return _GOOD_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    asyncio.run(pila.replay_capture(
        _CAPTURE_RECORD,
        cwd=str(tmp_path),
    ))

    assert existing.read_text() == original_content, (
        "replay_capture modified an existing calls.ndjson")


# ---------------------------------------------------------------------------
# Criterion 4: return value shape
# ---------------------------------------------------------------------------

def test_return_value_shape(pila, tmp_path, monkeypatch):
    """replay_capture returns a 2-tuple (envelope, structured_output)."""

    async def fake_invoke(cmd, cwd, timeout, sid, pila_dir, verbosity,
                          progress=None):
        return _GOOD_ENVELOPE

    monkeypatch.setattr(pila, "_invoke", fake_invoke)

    result = asyncio.run(pila.replay_capture(_CAPTURE_RECORD))

    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    assert len(result) == 2, f"Expected 2-tuple, got {len(result)}-tuple"

    envelope, structured_output = result
    assert isinstance(envelope, dict), "First element (envelope) must be a dict"
    assert isinstance(structured_output, dict), (
        "Second element (structured_output) must be a dict")

    # structured_output matches the envelope's structured_output field
    assert structured_output == _GOOD_ENVELOPE["structured_output"]

    # envelope has the expected keys from the fake invocation
    assert envelope.get("type") == "result"
    assert envelope.get("is_error") is False


# ---------------------------------------------------------------------------
# Criterion 5: importable from pila module
# ---------------------------------------------------------------------------

def test_replay_capture_importable(pila):
    """replay_capture must be a top-level name in the pila module."""
    assert hasattr(pila, "replay_capture"), (
        "replay_capture is not defined in orchestrator/pila.py")
    assert callable(pila.replay_capture), (
        "replay_capture is not callable")
