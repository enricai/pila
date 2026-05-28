"""Tests for _invoke()'s line-by-line streaming behavior.

These tests mock `asyncio.create_subprocess_exec` rather than spawn a
real subprocess (per CLAUDE.md, claude_p is not exercised live in unit
tests — the worker invocation path is end-to-end tier). The mock
yields a pre-recorded stream of events shaped like real
`claude -p --output-format stream-json --verbose` output.

What we pin here:
  - The final `result` event is returned as the envelope (same shape
    consumers already parse).
  - Per-worker log file is always written, regardless of verbosity.
  - Inline summaries are emitted to pila's log() per verbosity.
  - If no `result` event arrives, WorkerError is raised.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ----- minimal mock for asyncio.subprocess.Process --------------------------

class _MockStream:
    """A mock asyncio stream that yields pre-set lines, then EOF."""
    def __init__(self, lines: list[str]):
        self._lines = [(l + "\n").encode() for l in lines]
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line

    async def read(self, n: int = -1) -> bytes:
        # Used by the stderr drain path. Return empty bytes immediately
        # since the mock has no stderr.
        return b""


class _MockProc:
    """A mock asyncio.subprocess.Process."""
    def __init__(self, stdout_lines: list[str], returncode: int = 0):
        self.stdout = _MockStream(stdout_lines)
        self.stderr = _MockStream([])
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode

    async def communicate(self):
        # not used by streaming path, but kept for completeness
        out = b""
        async for chunk in self.stdout:
            out += chunk
        return out, b""


def _make_subprocess_exec_mock(stdout_lines: list[str], returncode: int = 0):
    async def fake(*cmd, **kwargs):
        return _MockProc(stdout_lines, returncode)
    return fake


@pytest.fixture
def pila_dir(tmp_path):
    cd = tmp_path / ".pila"
    cd.mkdir()
    (cd / "logs").mkdir()
    return cd


# ----- envelope return ------------------------------------------------------

def test_invoke_returns_final_result_event(pila, pila_dir, monkeypatch):
    """The final type='result' event is the envelope, returned to the
    caller. Same shape as the pre-streaming json mode."""
    events = [
        json.dumps({"type": "system", "subtype": "init",
                    "model": "claude-opus-4-7"}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "total_cost_usd": 0.01,
                    "structured_output": {"ok": True},
                    "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    result = asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t1", pila_dir=pila_dir,
        verbosity="stream"))
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result["structured_output"] == {"ok": True}


def test_invoke_raises_when_no_result_event(pila, pila_dir, monkeypatch):
    """A worker that exits without emitting any result event (e.g. the
    process died mid-stream) raises WorkerError — same error class
    pila's existing retry path already handles."""
    events = [
        json.dumps({"type": "system", "subtype": "init",
                    "model": "x"}),
        # No result event — stream ends.
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    with pytest.raises(pila.WorkerError):
        asyncio.run(pila._invoke(
            ["claude", "-p", "x"], cwd=str(pila_dir.parent),
            timeout=60, sid="t2", pila_dir=pila_dir,
            verbosity="stream"))


# ----- per-worker log file --------------------------------------------------

def test_log_file_written_at_stream(pila, pila_dir, monkeypatch):
    events = [
        json.dumps({"type": "system", "subtype": "init", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t3", pila_dir=pila_dir,
        verbosity="stream"))
    log_text = (pila_dir / "logs" / "t3.log").read_text()
    # All 3 events appear in the file.
    assert "system/init" in log_text
    assert "assistant" in log_text
    assert "result/success" in log_text


def test_log_file_written_at_quiet(pila, pila_dir, monkeypatch):
    """The per-worker file is written REGARDLESS of verbosity — even
    at quiet, the audit trail is preserved. Verbosity gates only the
    inline output."""
    events = [
        json.dumps({"type": "system", "subtype": "init", "model": "m"}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t4", pila_dir=pila_dir,
        verbosity="quiet"))
    log_text = (pila_dir / "logs" / "t4.log").read_text()
    assert "system/init" in log_text
    assert "result/success" in log_text


def test_log_file_records_non_json_lines(pila, pila_dir, monkeypatch):
    """If a line of stdout isn't valid JSON (rare; defensive), the raw
    line goes to the file with a 'non-json-line' header. Stream
    progresses past it."""
    events = [
        "not valid json at all",
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t5", pila_dir=pila_dir,
        verbosity="stream"))
    log_text = (pila_dir / "logs" / "t5.log").read_text()
    assert "non-json-line" in log_text
    assert "not valid json at all" in log_text


# ----- inline summaries (verbosity-gated) ----------------------------------

def test_inline_summaries_emitted_at_stream(pila, pila_dir,
                                            monkeypatch, capsys):
    events = [
        json.dumps({"type": "system", "subtype": "init",
                    "model": "opus"}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 2, "total_cost_usd": 0.05,
                    "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t6", pila_dir=pila_dir,
        verbosity="stream"))
    out = capsys.readouterr().out
    # Two summary lines: starting + done.
    assert "[t6] starting" in out
    assert "[t6] done" in out


def test_no_inline_summaries_at_quiet_for_success(pila, pila_dir,
                                                   monkeypatch, capsys):
    """At quiet, successful events produce no inline output. Per-worker
    file is still written (see test_log_file_written_at_quiet)."""
    events = [
        json.dumps({"type": "system", "subtype": "init", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"}]}}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t7", pila_dir=pila_dir,
        verbosity="quiet"))
    out = capsys.readouterr().out
    # None of the individual events should produce a [t7] line.
    assert "[t7]" not in out


def test_multi_line_summary_each_line_has_timestamp(pila, pila_dir,
                                                     monkeypatch, capsys):
    """A multi-line summary (multi-line text block, or multiple
    tool_use blocks in one event) must produce one log() call per
    line so each line gets its own [pila HH:MM:SS] prefix.

    Earlier behavior returned a \\n-joined string and called log()
    once, which prepended the timestamp only to the first line —
    lines 2+ visually disconnected from the orchestrator's
    timestamped stream. In a parallel run, untimestamped lines from
    one worker could be misread as belonging to a different worker."""
    events = [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "first paragraph\n"
                                     "second paragraph\n"
                                     "third paragraph"}]}}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t-multi", pila_dir=pila_dir,
        verbosity="stream"))
    out = capsys.readouterr().out
    # Each text line is on its own output line, each prefixed with
    # the [pila HH:MM:SS] timestamp.
    paragraphs = ["first paragraph", "second paragraph", "third paragraph"]
    for para in paragraphs:
        # Find the line containing this paragraph and assert it has
        # the pila prefix.
        matching = [l for l in out.split("\n") if para in l]
        assert matching, f"missing line for {para!r}; got: {out!r}"
        for l in matching:
            assert l.lstrip().startswith("[pila "), (
                f"line {l!r} lacks [pila HH:MM:SS] prefix — the "
                "log() call per line guarantee broke")


def test_worker_failure_surfaces_even_at_quiet(pila, pila_dir,
                                                monkeypatch, capsys):
    """Errors emit at every level (clig.dev). A result event with
    is_error=true must produce a summary even at quiet."""
    events = [
        json.dumps({"type": "result", "subtype": "error_max_turns",
                    "num_turns": 5, "is_error": True}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t8", pila_dir=pila_dir,
        verbosity="quiet"))
    out = capsys.readouterr().out
    assert "[t8] worker failed" in out


# ----- P10-1: live-flush property (per-worker log file is line-buffered) ---

def test_log_file_opened_line_buffered(pila, pila_dir, monkeypatch):
    """The per-worker log file MUST be opened line-buffered (buffering=1)
    so a user running `tail -f .pila/logs/<sid>.log` sees events as
    they happen, not when the file closes at worker end. Default Python
    text-mode buffering would batch writes into ~8KB chunks and only
    flush on close — silently defeating the live-progress property the
    streaming feature exists to provide.

    Tested by spying on Path.open to capture the keyword arguments. Set
    up before `_invoke` runs; assert `buffering=1` was passed."""
    open_calls: list[dict] = []
    real_open = type(pila_dir).open  # pathlib.Path.open

    def spy_open(self, *args, **kwargs):
        # Spy on every Path.open and capture the path along with the
        # kwargs so we can filter to the per-worker log open after.
        open_calls.append({"path": str(self), "args": args,
                           "kwargs": kwargs})
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(pila_dir), "open", spy_open)
    events = [json.dumps({"type": "result", "subtype": "success",
                          "num_turns": 1, "is_error": False})]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t-flush", pila_dir=pila_dir,
        verbosity="quiet"))
    # The log file open call must have buffering=1.
    log_opens = [c for c in open_calls if c["path"].endswith("t-flush.log")]
    assert log_opens, ("expected at least one t-flush.log open "
                       f"intercepted; got: {open_calls!r}")
    for call in log_opens:
        assert call["kwargs"].get("buffering") == 1, (
            "per-worker log file MUST be opened with buffering=1 (line-"
            "buffered) so `tail -f` shows live progress. Without this, "
            "Python's default text-mode buffering batches writes until "
            f"the file closes. Got: {call!r}")


# ----- P10-2: StreamReader limit raised to handle large JSON events --------

class _OverlimitStream:
    """A mock stream that yields one valid event, then raises
    ValueError on the next iteration — simulating asyncio's
    StreamReader hitting the line limit mid-stream (which is what
    happens when a worker emits a line >10 MiB)."""
    def __init__(self, first_line: str):
        self._yielded = False
        self._line = (first_line + "\n").encode()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._yielded:
            self._yielded = True
            return self._line
        raise ValueError(
            "Separator is not found, and chunk exceed the limit")

    async def read(self, n: int = -1) -> bytes:
        return b""


def test_value_error_from_line_limit_becomes_worker_error(pila,
                                                          pila_dir,
                                                          monkeypatch):
    """When a worker emits a line larger than the StreamReader limit,
    `async for proc.stdout` raises `ValueError("Separator is not
    found...")`. Without explicit handling this propagates through
    claude_p's retry loop and surfaces as a Python traceback. The
    Pass-12 fix wraps the `async for` in a try/except that converts
    the ValueError into a WorkerError — same exception class
    pila's retry / blocked-subtask paths already handle.

    Mock the stream so the second iteration raises ValueError; assert
    _invoke raises WorkerError (not ValueError) and the message names
    the buffer limit so a user can recognize the failure mode."""
    class _OverlimitProc:
        def __init__(self):
            self.stdout = _OverlimitStream(json.dumps({
                "type": "system", "subtype": "init", "model": "m"}))
            self.stderr = _MockStream([])
            self.returncode = 1

        def kill(self):
            pass

        async def wait(self):
            return self.returncode

    async def fake(*cmd, **kwargs):
        return _OverlimitProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    with pytest.raises(pila.WorkerError) as exc_info:
        asyncio.run(pila._invoke(
            ["claude", "-p", "x"], cwd=str(pila_dir.parent),
            timeout=60, sid="t-overlimit", pila_dir=pila_dir,
            verbosity="quiet"))
    msg = str(exc_info.value)
    # The error message must name the limit so a user diagnosing
    # the failure can act on it (not a generic "worker failed").
    assert "10 MiB" in msg or "buffer limit" in msg, msg
    # ValueError must NOT leak through — explicitly check that the
    # caller doesn't see a ValueError. (pytest.raises only catches
    # the exception type asked for; if the code actually raises
    # ValueError, this test would fail BEFORE getting here. The
    # explicit assert is redundant but documents the contract.)
    assert not isinstance(exc_info.value, ValueError), (
        "ValueError leaked through — should have been converted to "
        "WorkerError. See Pass-12 audit.")


def test_progress_prefix_shown_when_progress_passed(pila, pila_dir,
                                                      monkeypatch, capsys):
    """When progress=(done, total) is passed to _invoke, every inline
    summary line is prefixed with [done/total] before the worker tag.
    This is the implementer/integrator/conformer path once waves exist."""
    events = [
        json.dumps({"type": "system", "subtype": "init", "model": "opus"}),
        json.dumps({"type": "result", "subtype": "success",
                    "num_turns": 1, "is_error": False}),
    ]
    monkeypatch.setattr("asyncio.create_subprocess_exec",
                        _make_subprocess_exec_mock(events))
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t-prog", pila_dir=pila_dir,
        verbosity="stream", progress=(3, 12)))
    out = capsys.readouterr().out
    assert "[3/12]" in out
    assert "[t-prog] starting" in out


def test_create_subprocess_exec_uses_high_limit(pila, pila_dir,
                                                  monkeypatch):
    """asyncio's StreamReader defaults to 64KB per line. A single JSON
    event from `claude -p --output-format stream-json` can plausibly
    exceed that — the implementer's `structured_output` tool_use carries
    the full worker payload (criteria results with multi-KB evidence
    strings, falsifier arrays, etc.). Without a higher limit, a large
    event raises LimitOverrunError mid-stream and the worker run dies
    with no useful diagnostic.

    Pin that create_subprocess_exec is called with a limit well above
    the default."""
    captured_kwargs: dict = {}

    async def spy(*cmd, **kwargs):
        captured_kwargs.update(kwargs)
        # Return a minimal valid stream so _invoke completes normally.
        events = [json.dumps({"type": "result", "subtype": "success",
                              "num_turns": 1, "is_error": False})]
        return _MockProc(events)

    monkeypatch.setattr("asyncio.create_subprocess_exec", spy)
    asyncio.run(pila._invoke(
        ["claude", "-p", "x"], cwd=str(pila_dir.parent),
        timeout=60, sid="t-limit", pila_dir=pila_dir,
        verbosity="quiet"))

    limit = captured_kwargs.get("limit")
    assert limit is not None, (
        "create_subprocess_exec must be called with an explicit `limit` "
        "kwarg to override asyncio's 64KB default — a single JSON event "
        "can exceed 64KB. See Pass-10 audit P10-2.")
    # 1 MB is the floor for "well above the default"; we shipped 10 MB.
    assert limit >= 1_000_000, (
        f"limit={limit} is too close to asyncio's 64KB default. A "
        "large structured_output event would still crash _read_stream "
        "with LimitOverrunError. The shipped value is 10 MB.")
