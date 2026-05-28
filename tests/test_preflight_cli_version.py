"""Preflight CLI version-check coupling tests.

Pila's worker invocation requires `--json-schema`, which is only
present in the `claude` CLI from v2.1.22 onward. A too-old CLI used to
surface as a cryptic "unknown option '--json-schema'" inside the smoke-
test error path. `_check_claude_cli_version()` replaces that with a
specific actionable error.

Tests are split in two:

- Behavioral tests on `_parse_claude_version()` (a pure function) confirm
  the regex correctly extracts versions and falls through on garbage.
- Source-text pins on `pila.py` enforce the placement contract: the
  constant exists at the documented value, `preflight` calls the check,
  and the call sits *before* the `if not skip_smoke:` block so the
  version check runs even when `--skip-smoke` is passed.

Same mockless / source-pinning style as `test_inspect_tools.py`.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


# --- behavioral tests on _parse_claude_version ----------------------------

def test_parse_version_typical_native_output(pila):
    assert pila._parse_claude_version("2.1.150 (Claude Code)\n") == (2, 1, 150)


def test_parse_version_at_minimum(pila):
    assert pila._parse_claude_version("2.1.22 (Claude Code)") == (2, 1, 22)


def test_parse_version_old_v1(pila):
    assert pila._parse_claude_version("1.0.72 (Claude Code)") == (1, 0, 72)


def test_parse_version_empty_string(pila):
    assert pila._parse_claude_version("") is None


def test_parse_version_none(pila):
    assert pila._parse_claude_version(None) is None


def test_parse_version_unrecognized_text(pila):
    """Falls through (not closed) so the live smoke test can handle exotic
    CLI builds. Failing closed on a regex is the worse failure mode."""
    assert pila._parse_claude_version("hello world") is None


def test_parse_version_extra_whitespace(pila):
    assert pila._parse_claude_version("   2.1.143  ") == (2, 1, 143)


# --- ordering comparison sanity (the actual gate logic) -------------------

def test_min_claude_cli_constant_value(pila):
    """The floor is documented at v2.1.22 in DESIGN/IMPLEMENTATION and in
    the source comment. Bumping it is intentional, not incidental — this
    test catches accidental edits."""
    assert pila.MIN_CLAUDE_CLI == (2, 1, 22)


def test_old_version_below_floor(pila):
    assert pila._parse_claude_version("1.0.72") < pila.MIN_CLAUDE_CLI


def test_boundary_version_at_floor(pila):
    assert pila._parse_claude_version("2.1.22") == pila.MIN_CLAUDE_CLI


def test_new_version_above_floor(pila):
    assert pila._parse_claude_version("2.1.150") > pila.MIN_CLAUDE_CLI


# --- source-text pins on placement ----------------------------------------

def _preflight_body() -> str:
    """Return the source text of the preflight() coroutine, up to the next
    top-level def/async def/class."""
    src = PILA_PY.read_text()
    start = src.index("async def preflight(")
    next_async = src.index("\nasync def ", start + 1)
    next_sync = src.index("\ndef ", start + 1)
    end = min(next_async, next_sync)
    return src[start:end]


def test_preflight_calls_version_check():
    """preflight() must invoke the CLI version check."""
    body = _preflight_body()
    assert "_check_claude_cli_version()" in body, (
        "preflight must call _check_claude_cli_version() — without it, "
        "a stale CLI surfaces as 'unknown option --json-schema'."
    )


def test_version_check_runs_before_skip_smoke_gate():
    """The version check must precede the `if not skip_smoke:` block so it
    runs even when --skip-smoke is passed. --skip-smoke is for skipping the
    live model call (auth + a turn), not local CLI sanity. A future
    refactor reordering them would silently re-introduce the cryptic
    failure mode for users of --skip-smoke."""
    body = _preflight_body()
    check_pos = body.index("_check_claude_cli_version()")
    skip_pos = body.index("if not skip_smoke")
    assert check_pos < skip_pos, (
        "_check_claude_cli_version() must run before the skip_smoke gate"
    )


def test_min_claude_cli_defined_in_source():
    """Constant must be a module-level binding, not buried in a function —
    test code reads it via the `pila` fixture."""
    src = PILA_PY.read_text()
    assert "\nMIN_CLAUDE_CLI = (2, 1, 22)" in src, (
        "MIN_CLAUDE_CLI must be defined at module scope with value (2, 1, 22)"
    )
