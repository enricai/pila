"""Tests for _summarize_stream_event, _summarize_tool_use, and
_extract_tool_result_text — the per-event inline-log summarization.

Event shapes here come from real `claude -p --output-format stream-json
--verbose` captures, not invented. This is the pre-implementation
calibration step the plan called out: capture real output, read the
actual shapes, base the table on that. Each test uses the captured
shape verbatim where it matters (key paths like `message.content[]`,
field names like `tool_use.input.file_path`).

Verbosity controls inline output only — the per-worker .log file is
written regardless of level. These tests assert what's surfaced
inline.
"""
from __future__ import annotations

import pytest


# ----- _extract_tool_result_text: content union normalization ---------------

def test_extract_text_from_string_content(pila):
    """When tool_result.content is a plain string."""
    block = {"type": "tool_result", "content": "the file says hello"}
    assert pila._extract_tool_result_text(block) == "the file says hello"


def test_extract_text_from_list_content(pila):
    """When tool_result.content is a list of {type:'text', text:'...'}."""
    block = {"type": "tool_result", "content": [
        {"type": "text", "text": "line 1"},
        {"type": "text", "text": "line 2"},
    ]}
    assert pila._extract_tool_result_text(block) == "line 1 line 2"


def test_extract_text_handles_missing_content(pila):
    assert pila._extract_tool_result_text({}) == ""
    assert pila._extract_tool_result_text({"content": None}) == ""


# ----- _summarize_tool_use: per-tool shape ----------------------------------

def test_read_tool(pila):
    block = {"type": "tool_use", "name": "Read",
             "input": {"file_path": "src/foo.py"}}
    assert pila._summarize_tool_use("x", block, "stream") == "  [x read] src/foo.py"


def test_grep_tool_with_path(pila):
    block = {"type": "tool_use", "name": "Grep",
             "input": {"pattern": "TODO", "path": "src/"}}
    assert pila._summarize_tool_use("x", block, "stream") == "  [x grep] TODO in src/"


def test_grep_tool_without_path(pila):
    block = {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}}
    assert pila._summarize_tool_use("x", block, "stream") == "  [x grep] TODO"


def test_bash_tool_not_truncated_at_stream(pila):
    """Bash commands are not truncated at stream level. Mid-cut commands
    lose the operands at the end of a pipeline — the part a user
    reading the inline log actually needs to see. The per-worker
    .log file has identical content."""
    long_cmd = "echo " + "a" * 200
    block = {"type": "tool_use", "name": "Bash",
             "input": {"command": long_cmd}}
    out = pila._summarize_tool_use("x", block, "stream")
    # Full command preserved; only the leading prefix `  [x bash] ` is added.
    assert out == f"  [x bash] {long_cmd}"


def test_bash_tool_stream_and_debug_identical(pila):
    """Bash truncation is gone at every level — stream and debug
    produce the same output. Pinning so a future change that
    re-introduces a per-level limit is caught."""
    long_cmd = "echo " + "a" * 200
    block = {"type": "tool_use", "name": "Bash",
             "input": {"command": long_cmd}}
    out_stream = pila._summarize_tool_use("x", block, "stream")
    out_debug = pila._summarize_tool_use("x", block, "debug")
    assert out_stream == out_debug


def test_bash_tool_first_line_only(pila):
    block = {"type": "tool_use", "name": "Bash",
             "input": {"command": "first line\nsecond line\nthird"}}
    out = pila._summarize_tool_use("x", block, "stream")
    assert "second line" not in out
    assert "first line" in out


def test_write_edit_tools(pila):
    for name in ("Write", "Edit", "NotebookEdit"):
        block = {"type": "tool_use", "name": name,
                 "input": {"file_path": "src/foo.py"}}
        out = pila._summarize_tool_use("x", block, "stream")
        assert "src/foo.py" in out
        assert name.lower() in out


def test_structured_output_tool_stream_suppressed(pila):
    """The StructuredOutput tool is suppressed at stream — it adds
    noise since `done` follows immediately. At debug it shows the full
    payload with the 'finalizing output' label. The per-worker file
    has the raw event regardless of level."""
    big_payload = {"k": "v" * 2000}
    block = {"type": "tool_use", "name": "StructuredOutput",
             "input": big_payload}
    out_stream = pila._summarize_tool_use("x", block, "stream")
    out_debug = pila._summarize_tool_use("x", block, "debug")
    assert out_stream is None
    assert "finalizing output" in out_debug
    # debug shows the full payload (no truncation).
    assert "v" * 2000 in out_debug


def test_structured_output_not_truncated_at_debug(pila):
    """At debug level the StructuredOutput payload appears in full.
    Earlier behavior truncated to 200 chars — an awkward middle
    ground that lost the tail of any non-trivial payload. The user
    opting into debug is explicitly asking for raw worker output;
    truncating defeats the level."""
    big_payload = {"k": "v" * 2000}
    block = {"type": "tool_use", "name": "StructuredOutput",
             "input": big_payload}
    out_debug = pila._summarize_tool_use("x", block, "debug")
    # The full stringified payload appears in the output, including
    # the tail of the value (which a [:200] slice would have lost).
    assert "v" * 2000 in out_debug


def test_unknown_tool_falls_through(pila):
    """An unfamiliar tool name (e.g. an MCP tool) should produce a
    summary rather than a crash or KeyError. The full stringified
    input appears — earlier behavior truncated to 80 chars at stream
    / 200 at debug, which mid-cut MCP-tool operands like SQL queries
    or API payloads."""
    block = {"type": "tool_use", "name": "mcp__supabase__list_tables",
             "input": {"project_id": "abc"}}
    out = pila._summarize_tool_use("x", block, "stream")
    assert "mcp__supabase__list_tables" in out


def test_unknown_tool_not_truncated(pila):
    """An MCP-tool input with a long operand (e.g. a SQL query)
    appears in full at both stream and debug. Earlier behavior
    truncated to 80 chars at stream level, mid-cutting the part of
    the input most useful to read."""
    long_sql = "SELECT id, name, email FROM users WHERE " + (
        "tenant_id = 1 AND " * 30) + "active = true"
    block = {"type": "tool_use", "name": "mcp__supabase__execute_sql",
             "input": {"sql": long_sql}}
    out_stream = pila._summarize_tool_use("x", block, "stream")
    out_debug = pila._summarize_tool_use("x", block, "debug")
    # The full SQL appears in both — the tail of the query (the
    # "active = true" predicate) survives where [:80] would have
    # dropped it.
    assert "active = true" in out_stream
    assert "active = true" in out_debug
    # Stream and debug now produce the same output (no per-level
    # limit difference). Pin this so a future change that
    # re-introduces a debug-only formatting can't quietly happen.
    assert out_stream == out_debug


def test_tool_use_handles_missing_input(pila):
    """A tool_use block with no input dict (unusual but possible per
    schema) should not crash."""
    block = {"type": "tool_use", "name": "Read"}
    out = pila._summarize_tool_use("x", block, "stream")
    assert "?" in out  # the file_path fallback


# ----- _summarize_stream_event: quiet/normal drop most events --------------

def test_quiet_drops_assistant_events(pila):
    event = {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "hi"}]}}
    assert pila._summarize_stream_event("x", event, "quiet") is None


def test_normal_drops_assistant_events(pila):
    event = {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "hi"}]}}
    assert pila._summarize_stream_event("x", event, "normal") is None


def test_quiet_surfaces_worker_errors(pila):
    """Errors emit at every level (clig.dev anti-pattern guard). A
    result event with is_error=true must produce a summary even at
    quiet, otherwise the user sees a silent failure."""
    event = {"type": "result", "subtype": "error_max_turns",
             "is_error": True, "num_turns": 5}
    out = pila._summarize_stream_event("x", event, "quiet")
    assert out is not None
    assert "worker failed" in out
    assert "error_max_turns" in out


def test_normal_surfaces_worker_errors(pila):
    event = {"type": "result", "subtype": "error_max_turns",
             "is_error": True, "num_turns": 5}
    assert pila._summarize_stream_event("x", event, "normal") is not None


# ----- system/init at stream and above --------------------------------------

def test_system_init_at_stream(pila):
    """Captured shape: {type:'system', subtype:'init', model:'...', ...}.
    Stream level shows model name; full payload goes to the file."""
    event = {"type": "system", "subtype": "init",
             "model": "claude-opus-4-7[1m]"}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "starting" in out
    assert "claude-opus-4-7[1m]" in out


def test_hook_events_dropped_at_stream(pila):
    """SessionStart hooks fire on every claude -p invocation and are
    noise at the stream level. Surfaced only at debug."""
    event = {"type": "system", "subtype": "hook_started",
             "hook_name": "SessionStart:startup"}
    assert pila._summarize_stream_event("x", event, "stream") is None


def test_hook_events_visible_at_debug(pila):
    event = {"type": "system", "subtype": "hook_started",
             "hook_name": "SessionStart:startup"}
    out = pila._summarize_stream_event("x", event, "debug")
    assert out is not None
    assert "SessionStart:startup" in out


# ----- assistant message content[] — key path is event.message.content[] ----

def test_assistant_text_block_at_stream(pila):
    """The key path is event.message.content[], not event.content[].
    This was the corrected path after live capture — pinning so a
    future refactor doesn't regress to the wrong path."""
    event = {"type": "assistant", "message": {
        "content": [{"type": "text", "text": "starting investigation"}],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "starting investigation" in out


def test_assistant_text_block_all_lines_emitted(pila):
    """A multi-line text block emits every non-empty line as its own
    [<sid> text] entry. Earlier behavior took only the first line,
    which dropped useful content for paragraph-style replies and
    code-fenced messages. Empty lines (e.g. blank separators) are
    skipped to avoid cluttering the inline log."""
    event = {"type": "assistant", "message": {
        "content": [{"type": "text",
                     "text": "first\nsecond\n\nthird"}],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    assert "[x text] first" in out
    assert "[x text] second" in out
    assert "[x text] third" in out
    # The blank line between "second" and "third" must not produce
    # an empty `[x text]` entry.
    assert "[x text] \n" not in out and not out.endswith("[x text] ")


def test_assistant_text_block_not_truncated(pila):
    """Long text lines are emitted in full at stream level. The
    100-char limit in earlier versions cut sentences mid-thought."""
    long_text = "a long sentence " * 50  # ~800 chars
    event = {"type": "assistant", "message": {
        "content": [{"type": "text", "text": long_text}],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    # The full text appears in the output (stripped of leading/trailing
    # whitespace by the per-line .strip()).
    assert long_text.strip() in out


def test_assistant_tool_use_block(pila):
    event = {"type": "assistant", "message": {
        "content": [{"type": "tool_use", "name": "Read",
                     "input": {"file_path": "x.py"}}],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "x.py" in out


def test_assistant_multiple_blocks(pila):
    """An assistant message can carry multiple blocks (e.g. some text
    plus a tool call). Both should appear in the summary."""
    event = {"type": "assistant", "message": {
        "content": [
            {"type": "text", "text": "reading file"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
        ],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    assert "reading file" in out
    assert "x.py" in out


def test_assistant_structured_output_with_text_no_none_literal(pila):
    """An assistant event that carries both a text block and a
    StructuredOutput tool_use at stream verbosity must not inject the
    literal string 'None' into the summary. _summarize_tool_use returns
    None for StructuredOutput at stream; if that None is appended to
    the lines list and joined, '\\n'.join([..., None]) raises TypeError
    in Python 3 — or produces 'None' if the list is filtered lazily.
    The fix: skip None values at the append site."""
    event = {"type": "assistant", "message": {
        "content": [
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "name": "StructuredOutput",
             "input": {"result": "value"}},
        ],
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    # The text line must appear.
    assert out is not None
    assert "thinking..." in out
    # The literal string "None" must NOT appear anywhere in the output.
    assert "None" not in out


def test_assistant_empty_content_returns_none(pila):
    """A defensive empty-content path. Stream / debug should not
    crash."""
    event = {"type": "assistant", "message": {"content": []}}
    assert pila._summarize_stream_event("x", event, "stream") is None


# ----- user tool_result events ---------------------------------------------

def test_tool_failure_surfaces_at_stream(pila):
    """A failing tool_result (e.g. schema validation failure) is a
    visible event at stream — the user wants to know."""
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True,
         "content": "Output does not match required schema: root: must have required property 'ok'",
         "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "tool-fail" in out
    assert "Output does not match" in out


def test_tool_failure_not_truncated(pila):
    """A long tool-failure message (e.g. a schema validation listing
    multiple missing fields) must surface in full. Earlier behavior
    truncated to 120 chars, dropping the useful detail (`root: must
    have required property 'X', root: must have required property
    'Y'` cut mid-second-field). The per-worker .log file is
    unaffected; this just ensures the inline view matches it."""
    long_err = ("Output does not match required schema: "
                "root: must have required property 'domain', "
                "root: must have required property 'subtasks', "
                "root: must have required property 'status', "
                "root: must have required property 'confidence'")
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True,
         "content": long_err, "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "stream")
    # Full error appears, including the LAST missing field at the end.
    assert "'confidence'" in out
    assert long_err in out


def test_tool_success_dropped_at_stream(pila):
    """Successful tool_result is the noise floor — drop from inline
    at stream level. File has it."""
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": "file contents here",
         "tool_use_id": "tu_1"},
    ]}}
    assert pila._summarize_stream_event("x", event, "stream") is None


def test_tool_success_visible_at_debug(pila):
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": "file contents here",
         "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "debug")
    assert out is not None
    assert "tool-ok" in out


def test_tool_failure_multiline_each_line_tagged(pila):
    """A multi-line tool-fail content (rare but possible — e.g. a
    schema validator with a multi-line error format) must tag every
    line with `[<sid> tool-fail]`. Otherwise lines 2+ would appear
    as bare text in the orchestrator log, indistinguishable from
    other workers' output in a parallel run."""
    multi_err = ("line one of the error\n"
                 "line two with more detail\n"
                 "line three with a trailing fact")
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True,
         "content": multi_err, "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    # Every line carries the tag.
    assert "[x tool-fail] line one of the error" in out
    assert "[x tool-fail] line two with more detail" in out
    assert "[x tool-fail] line three with a trailing fact" in out


def test_tool_success_multiline_each_line_tagged_at_debug(pila):
    """A Read of a multi-line file at debug verbosity tags every
    non-empty line with `[<sid> tool-ok]`. Earlier behavior
    returned the content with a single leading tag, leaving lines
    2+ as bare text. Per the same per-line-attribution principle
    as multi-line assistant text and the per-line timestamp fix
    in Pass-15."""
    file_content = "def foo():\n    return 1\n\ndef bar():\n    return 2"
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": file_content, "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "debug")
    assert out is not None
    # Every non-empty line is tagged.
    assert "[x tool-ok] def foo():" in out
    assert "[x tool-ok]     return 1" in out
    assert "[x tool-ok] def bar():" in out
    assert "[x tool-ok]     return 2" in out
    # The blank line between functions must NOT produce a bare
    # `[x tool-ok] ` entry.
    assert "[x tool-ok] \n" not in out and not out.endswith("[x tool-ok] ")


def test_tool_success_not_truncated_at_debug(pila):
    """At debug verbosity a successful tool_result appears in full —
    earlier behavior truncated to 120 chars. The trade-off: a
    worker reading a large file will flood the orchestrator log at
    debug. That's the user's accepted cost when they opt into
    debug-level streaming. Per-worker .log file always has full
    content regardless."""
    long_result = "line of file content " * 100  # ~2100 chars
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": long_result, "tool_use_id": "tu_1"},
    ]}}
    out = pila._summarize_stream_event("x", event, "debug")
    assert out is not None
    # The tail of the content survives — [:120] would have cut it.
    assert long_result.rstrip() in out


# ----- rate_limit_event: live-captured shape -------------------------------

def test_rate_limit_threshold_crossing_surfaces(pila):
    """Captured shape: {type:'rate_limit_event', rate_limit_info:{...,
    surpassedThreshold:0.75, status:'allowed_warning', utilization:0.89}}.
    A threshold-crossing surfaces at stream — the user wants to know."""
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning",
        "utilization": 0.89,
        "surpassedThreshold": 0.75,
    }}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "rate-limit" in out
    assert "89" in out  # utilization as percent


def test_rate_limit_quiet_event_dropped_at_stream(pila):
    """A rate_limit_event without surpassedThreshold (routine
    accounting) is dropped at stream."""
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "utilization": 0.10,
    }}
    assert pila._summarize_stream_event("x", event, "stream") is None


def test_rate_limit_always_visible_at_debug(pila):
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "utilization": 0.10,
    }}
    assert pila._summarize_stream_event("x", event, "debug") is not None


# ----- result envelope -----------------------------------------------------

def test_result_success_summary(pila):
    """Live-captured shape: result.subtype='success', total_cost_usd,
    num_turns. Show all three at stream."""
    event = {"type": "result", "subtype": "success",
             "num_turns": 3, "total_cost_usd": 0.1290}
    out = pila._summarize_stream_event("x", event, "stream")
    assert "done" in out
    assert "3" in out
    assert "0.1290" in out


def test_result_error_summary_at_stream(pila):
    """A failing result at stream (NOT quiet — quiet has its own
    error path tested above)."""
    event = {"type": "result", "subtype": "error_max_turns",
             "num_turns": 1, "is_error": True}
    out = pila._summarize_stream_event("x", event, "stream")
    assert out is not None
    assert "failed" in out or "error" in out


# ----- unknown event type --------------------------------------------------

def test_unknown_event_type_at_debug(pila):
    event = {"type": "future_event_type", "subtype": "novel"}
    out = pila._summarize_stream_event("x", event, "debug")
    assert out is not None
    assert "future_event_type" in out


def test_unknown_event_type_dropped_at_stream(pila):
    event = {"type": "future_event_type", "subtype": "novel"}
    assert pila._summarize_stream_event("x", event, "stream") is None
