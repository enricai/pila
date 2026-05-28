"""Tests for is_protected_path() — the rule that gates which paths an
implementer may write to.

DESIGN §9: `.pila/` and `.git/` are coordination-only. Inside
`.claude/`, the three documented user-deliverable subtrees (`agents/`,
`commands/`, `skills/`) are exempt because pila's own self-healing
skill instructs consumers to write subagent files at
`.claude/agents/<name>.md`. Top-level `.claude/` files (`settings.json`,
`settings.local.json`) stay protected — they are coordination/config,
not deliverable customizations.
"""
from __future__ import annotations


def test_pila_path_is_protected(pila):
    assert pila.is_protected_path(".pila/state.json")
    assert pila.is_protected_path(".pila/runs/feat-x-abc/state.json")


def test_git_path_is_protected(pila):
    assert pila.is_protected_path(".git/HEAD")
    assert pila.is_protected_path(".git/refs/heads/main")


def test_claude_settings_json_is_protected(pila):
    assert pila.is_protected_path(".claude/settings.json")
    assert pila.is_protected_path(".claude/settings.local.json")


def test_claude_top_level_files_are_protected(pila):
    # Any unexpected top-level file under .claude/ stays protected by
    # default — the carve-out is for the three documented subtrees, not
    # for arbitrary new files.
    assert pila.is_protected_path(".claude/some-new-config.json")
    assert pila.is_protected_path(".claude/cache.db")


def test_claude_agents_is_a_deliverable(pila):
    # The barnacle failure case: a subagent at .claude/agents/ is the
    # documented Claude Code location. Must be writable.
    assert not pila.is_protected_path(
        ".claude/agents/recon-flow-patch-generator.md")
    assert not pila.is_protected_path(".claude/agents/my-helper.md")


def test_claude_commands_is_a_deliverable(pila):
    assert not pila.is_protected_path(".claude/commands/my-command.md")
    assert not pila.is_protected_path(
        ".claude/commands/sub/nested-cmd.md")


def test_claude_skills_is_a_deliverable(pila):
    assert not pila.is_protected_path(
        ".claude/skills/my-skill/SKILL.md")
    assert not pila.is_protected_path(
        ".claude/skills/llm-self-heal/SKILL.md")


def test_normal_source_path_is_unprotected(pila):
    assert not pila.is_protected_path("src/main.py")
    assert not pila.is_protected_path("docs/DESIGN.md")
    assert not pila.is_protected_path("CLAUDE.md")
    assert not pila.is_protected_path("pila.toml")


def test_exact_prefix_required_not_substring(pila):
    # `is_protected_path` matches on prefix, not substring. A source file
    # that happens to contain ".claude" in its name (not at the start)
    # is unrelated.
    assert not pila.is_protected_path("src/dot-claude-helper.py")
    assert not pila.is_protected_path("docs/about-.pila.md")


def test_claude_root_path_is_protected(pila):
    # The bare ".claude/" prefix without a subtree (which shouldn't occur
    # as a real path but is structurally possible) stays protected.
    assert pila.is_protected_path(".claude/")
