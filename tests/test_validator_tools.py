"""F-01 coupling test: the validator must run with a tool bucket that allows
execution (Bash) but forbids modification (no Write/Edit).

VALIDATOR_SYSTEM says "You do not modify code." DESIGN §12 ("prompts advisory,
code enforces") requires that contract be enforced by the tool allowlist, not
left to the model. This test pins both halves so a future edit that hands the
validator Write or Edit fails loudly.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CENTELLA_PY = REPO_ROOT / "orchestrator" / "centella.py"


def test_run_tools_includes_bash(centella):
    tools = set(centella.RUN_TOOLS.split(","))
    assert "Bash" in tools, "validator needs Bash to execute criteria"


def test_run_tools_excludes_write_and_edit(centella):
    tools = set(centella.RUN_TOOLS.split(","))
    assert "Write" not in tools, "RUN_TOOLS must not grant Write — see F-01"
    assert "Edit" not in tools, "RUN_TOOLS must not grant Edit — see F-01"


def test_act_tools_still_includes_write_and_edit(centella):
    """Sanity: implementer/integrator (ACT_TOOLS) keep their write tools."""
    tools = set(centella.ACT_TOOLS.split(","))
    assert "Write" in tools
    assert "Edit" in tools
    assert "Bash" in tools


def test_validate_wave_call_site_uses_run_tools():
    """Pin the wiring: validate_wave must invoke claude_p with RUN_TOOLS, not
    ACT_TOOLS. Source-text check (no live claude run) — same approach as
    test_retryable_failure's coupling test."""
    src = CENTELLA_PY.read_text()
    # Locate the validate_wave function body.
    start = src.index("async def validate_wave(")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "allowed_tools=RUN_TOOLS" in body, (
        "validate_wave must pass allowed_tools=RUN_TOOLS to claude_p — F-01"
    )
    assert "allowed_tools=ACT_TOOLS" not in body, (
        "validate_wave must not pass allowed_tools=ACT_TOOLS — F-01 regression"
    )
