"""Coupling tests for INSPECT_TOOLS — the tool bucket for classifier,
planner, reconciler, and provision.

These workers run in the real repo cwd (no worktree isolation), so they
cannot use --dangerously-skip-permissions. INSPECT_TOOLS preserves the
DESIGN §12 "read-only worker" contract mechanically: read tools plus
allowlisted Bash(<verb>:*) patterns for cross-cwd inspection, no
Write/Edit. Anything outside the allowlist falls through and is rejected
in non-interactive mode.

These tests pin both halves so a future edit that adds Write/Edit or
swaps in a bare Bash wildcard (which would defeat the allowlist) fails
loudly.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


def _entries(bucket: str) -> list[str]:
    """Split the bucket string on commas — but not commas inside Bash(...)
    parens. The current INSPECT_TOOLS has no comma inside a Bash pattern
    (commas are between entries, spaces or colons inside patterns), so a
    plain split is correct today. Guarded with a sanity assertion below."""
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in bucket:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [e.strip() for e in out if e.strip()]


def test_inspect_tools_has_bash_patterns(pila):
    """At least one Bash(<verb>:*) pattern must be present — that's the
    whole point of the bucket. Without it, classifier/planner/reconciler/provision
    can't run ls/find/cat without per-call permission prompts (which are
    never granted in -p mode)."""
    entries = _entries(pila.INSPECT_TOOLS)
    bash_patterns = [e for e in entries if e.startswith("Bash(")]
    assert bash_patterns, (
        "INSPECT_TOOLS must contain at least one Bash(...) pattern so the "
        "inspect-bucket workers can run read-only shell commands without "
        "per-call permission prompts"
    )


def test_inspect_tools_excludes_write_and_edit(pila):
    """No Write/Edit — the §12 read-only-worker contract."""
    entries = set(_entries(pila.INSPECT_TOOLS))
    assert "Write" not in entries, (
        "INSPECT_TOOLS must not grant Write — DESIGN §12 read-only contract"
    )
    assert "Edit" not in entries, (
        "INSPECT_TOOLS must not grant Edit — DESIGN §12 read-only contract"
    )


def test_inspect_tools_excludes_bare_bash(pila):
    """A bare `Bash` entry would auto-approve ANY shell command, defeating
    the allowlist. Patterns only — Bash(<verb>:*) form."""
    entries = set(_entries(pila.INSPECT_TOOLS))
    assert "Bash" not in entries, (
        "INSPECT_TOOLS must use Bash(<verb>:*) patterns, not bare Bash — "
        "a wildcard would defeat the read-only-shell allowlist"
    )


def test_inspect_tools_includes_read_tools(pila):
    """Read/Grep/Glob still need to be in the bucket — they're the
    primary tools and the Bash patterns are a fallback for cross-cwd
    inspection."""
    entries = set(_entries(pila.INSPECT_TOOLS))
    for name in ("Read", "Grep", "Glob"):
        assert name in entries, f"INSPECT_TOOLS must include {name}"


def test_classifier_call_site_uses_inspect_tools():
    """Source-text check: the phase_classify worker invocation must pass
    allowed_tools=INSPECT_TOOLS, not READ_TOOLS (removed) or ACT_TOOLS
    (would grant Write/Edit)."""
    src = PILA_PY.read_text()
    start = src.index("async def phase_classify(")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "allowed_tools=INSPECT_TOOLS" in body, (
        "phase_classify must pass allowed_tools=INSPECT_TOOLS to claude_p"
    )
    assert "allowed_tools=ACT_TOOLS" not in body
    assert "allowed_tools=RUN_TOOLS" not in body


def test_planner_call_site_uses_inspect_tools():
    """plan_one is a closure inside phase_plan; check the enclosing
    function's body for the call site."""
    src = PILA_PY.read_text()
    start = src.index("async def phase_plan(")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "allowed_tools=INSPECT_TOOLS" in body, (
        "phase_plan's plan_one must pass allowed_tools=INSPECT_TOOLS"
    )
    assert "allowed_tools=ACT_TOOLS" not in body
    assert "allowed_tools=RUN_TOOLS" not in body


def test_reconciler_call_site_uses_inspect_tools():
    src = PILA_PY.read_text()
    start = src.index("async def phase_reconcile(")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "allowed_tools=INSPECT_TOOLS" in body, (
        "phase_reconcile must pass allowed_tools=INSPECT_TOOLS to claude_p"
    )
    assert "allowed_tools=ACT_TOOLS" not in body
    assert "allowed_tools=RUN_TOOLS" not in body
