"""Tests for discover_rules_files() — repo-agnostic discovery of rule
files for the post-work conformance phase (DESIGN §9 *Post-work
conformance*).

The function checks a fixed, capped allowlist of paths and returns
existing ones in declaration order. It never raises and never recurses;
the location of rules files varies across repos so discovery is broad
on the allowlist axis and narrow on the search axis.
"""
from __future__ import annotations


def test_returns_empty_when_no_rules_files_exist(pila, tmp_path):
    assert pila.discover_rules_files(tmp_path) == []


def test_finds_claude_md_at_root(pila, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    out = pila.discover_rules_files(tmp_path)
    assert out == [tmp_path / "CLAUDE.md"]


def test_finds_agents_md_at_root(pila, tmp_path):
    (tmp_path / "AGENTS.md").write_text("# rules\n")
    out = pila.discover_rules_files(tmp_path)
    assert out == [tmp_path / "AGENTS.md"]


def test_finds_readme_when_present(pila, tmp_path):
    (tmp_path / "README.md").write_text("# readme\n")
    out = pila.discover_rules_files(tmp_path)
    assert out == [tmp_path / "README.md"]


def test_finds_docs_files(pila, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "CONVENTIONS.md").write_text("c\n")
    (docs / "STYLE.md").write_text("s\n")
    out = pila.discover_rules_files(tmp_path)
    rels = [p.relative_to(tmp_path).as_posix() for p in out]
    assert "docs/CONVENTIONS.md" in rels
    assert "docs/STYLE.md" in rels


def test_returns_priority_order_not_filesystem_order(pila, tmp_path):
    """CLAUDE.md is declared before AGENTS.md in the allowlist — so when
    both exist, CLAUDE.md comes first regardless of mtime / creation."""
    (tmp_path / "AGENTS.md").write_text("a\n")
    (tmp_path / "CLAUDE.md").write_text("c\n")
    out = pila.discover_rules_files(tmp_path)
    rels = [p.relative_to(tmp_path).as_posix() for p in out]
    assert rels.index("CLAUDE.md") < rels.index("AGENTS.md")


def test_capped_at_allowlist_length(pila, tmp_path):
    """Even if every candidate exists, the output is bounded by the
    allowlist — discovery never recurses or globs."""
    # Touch every candidate (using internal allowlist constant)
    for rel in pila._RULES_FILE_CANDIDATES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    # Make a random extra file that is NOT in the allowlist — it should
    # not appear in the output.
    (tmp_path / "RANDOM_RULES.md").write_text("nope\n")
    out = pila.discover_rules_files(tmp_path)
    assert len(out) == len(pila._RULES_FILE_CANDIDATES)
    rels = [p.relative_to(tmp_path).as_posix() for p in out]
    assert "RANDOM_RULES.md" not in rels


def test_directory_with_candidate_name_is_skipped(pila, tmp_path):
    """If a candidate path happens to be a directory (e.g. someone made
    `CLAUDE.md/` a directory by mistake), discovery silently skips it."""
    (tmp_path / "CLAUDE.md").mkdir()
    assert pila.discover_rules_files(tmp_path) == []


def test_nonexistent_repo_root_returns_empty(pila, tmp_path):
    """A repo root that doesn't exist returns [] without raising — the
    contract is "never raises."""
    out = pila.discover_rules_files(tmp_path / "does-not-exist")
    assert out == []
