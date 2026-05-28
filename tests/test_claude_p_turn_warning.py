"""Source-text coupling test for the context-decay warning in claude_p.

claude_p shells out to the `claude` CLI and the test suite does not exercise
the live binary (see CLAUDE.md "Testing"). We pin the warning's presence
in source the same way `test_inspect_tools.py` pins the tool-bucket
contract: by checking the function body contains the expected check and
that it lives in the place where the envelope has been parsed.

The risk this guards against is that a future refactor removes the
80%-of-max_turns warning without leaving a structural trace. The schema
only validates the final output's *shape*, not whether the reasoning that
produced it was anchored in a healthy context — this warning is the only
proxy the orchestrator has.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


def _claude_p_body() -> str:
    """Return the source text of the claude_p function."""
    src = PILA_PY.read_text()
    start = src.index("async def claude_p(")
    # claude_p ends at the next top-level `def` or `async def` / `class`
    next_async = src.index("\nasync def ", start + 1)
    next_sync = src.index("\ndef ", start + 1)
    end = min(next_async, next_sync)
    return src[start:end]


def test_warning_check_present():
    """The 80%-of-max-turns warning fires at the claude_p return path,
    after envelope parsing and before structured_output is returned."""
    body = _claude_p_body()
    # The threshold itself (the actual proportional check).
    assert "0.8 * max_turns" in body, (
        "claude_p must compare num_turns against 80% of max_turns as the "
        "context-decay proxy — see DESIGN §8 / Pass-4 audit P4-1")
    # The log message must call out the proxy nature so a user reading the
    # output understands why it fires.
    assert "degraded context" in body, (
        "the warning text must name 'degraded context' so a 9.x confidence "
        "score from a near-cap worker is read with the right scepticism")


def test_warning_sits_with_existing_terminal_reason_check():
    """The new warning should live next to the existing terminal_reason
    warning, not somewhere else — both are post-envelope checks that
    surface non-clean exits. Splitting them would make the relationship
    less obvious to future maintainers."""
    body = _claude_p_body()
    term_idx = body.index('terminal_reason')
    turn_warn_idx = body.index('0.8 * max_turns')
    # The new check appears AFTER the terminal_reason block (the existing
    # block runs first; the new one is its `elif`-style sibling).
    assert turn_warn_idx > term_idx
    # And the two are close together — sibling branches in one
    # conditional, not separated by other logic. Threshold accommodates
    # the explanatory comment block between them without being so loose
    # that an unrelated chunk could slip in.
    assert turn_warn_idx - term_idx < 1500, (
        "the proportional-turn warning should sit immediately after the "
        "terminal_reason warning so the two non-clean-exit signals stay "
        "co-located")


def test_warning_only_runs_when_terminal_reason_not_set():
    """Avoid double-warning the same condition: if the worker exited
    mid-work, terminal_reason will be non-empty and the existing line
    already names num_turns. The new proportional check should only
    fire when terminal_reason is absent/completed."""
    body = _claude_p_body()
    # The new check is in an `elif` branch under the terminal_reason
    # conditional — that's the structural defense against double-firing.
    # Locate the new warning and confirm `elif` precedes it.
    turn_warn_idx = body.index('0.8 * max_turns')
    # Search backwards for the nearest control-flow keyword.
    prefix = body[:turn_warn_idx]
    last_elif = prefix.rfind("elif")
    last_if = prefix.rfind("\n        if ")
    # elif must be the more recent of the two for the structural defense
    # to hold.
    assert last_elif > last_if, (
        "the proportional warning must be in an `elif` branch under the "
        "terminal_reason check, otherwise it will double-fire when a "
        "worker also tripped terminal_reason mid-work")
