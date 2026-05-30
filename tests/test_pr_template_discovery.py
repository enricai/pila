"""Tests for `find_pr_template()` — locating the target repo's PR
template in GitHub's canonical order.

Covers:
- Single-template locations (.github/, root, docs/), priority order.
- PULL_REQUEST_TEMPLATE/ directory: alphabetically first default.
- --pr-template override picks a specific basename (with or without .md).
- No template anywhere → returns None.
- Override naming a non-existent template falls back to default
  (we do not die() on a bad pr_template selector — that would block
  finalize over a cosmetic preference).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def _write(p: Path, content: str = "TEMPLATE\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_no_template_returns_none(pila, repo):
    assert pila.find_pr_template(repo) is None


def test_dot_github_single_wins(pila, repo):
    _write(repo / ".github/pull_request_template.md", "github-version")
    result = pila.find_pr_template(repo)
    assert result is not None
    path, rel = result
    assert rel == ".github/pull_request_template.md"
    assert path.read_text() == "github-version"


def test_root_single_when_no_dot_github(pila, repo):
    _write(repo / "pull_request_template.md", "root-version")
    result = pila.find_pr_template(repo)
    assert result is not None
    _, rel = result
    assert rel == "pull_request_template.md"


def test_docs_single_when_no_others(pila, repo):
    _write(repo / "docs/pull_request_template.md", "docs-version")
    result = pila.find_pr_template(repo)
    assert result is not None
    _, rel = result
    assert rel == "docs/pull_request_template.md"


def test_dot_github_beats_root(pila, repo):
    _write(repo / ".github/pull_request_template.md")
    _write(repo / "pull_request_template.md")
    _, rel = pila.find_pr_template(repo)
    assert rel == ".github/pull_request_template.md"


def test_root_beats_docs(pila, repo):
    _write(repo / "pull_request_template.md")
    _write(repo / "docs/pull_request_template.md")
    _, rel = pila.find_pr_template(repo)
    assert rel == "pull_request_template.md"


def test_multi_dir_alphabetical_first(pila, repo):
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/zebra.md", "z")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/alpha.md", "a")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/middle.md", "m")
    result = pila.find_pr_template(repo)
    assert result is not None
    path, rel = result
    assert rel == ".github/PULL_REQUEST_TEMPLATE/alpha.md"
    assert path.read_text() == "a"


def test_multi_dir_override_with_md_suffix(pila, repo):
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/bug.md", "b")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/feature.md", "f")
    _, rel = pila.find_pr_template(repo, override="feature.md")
    assert rel == ".github/PULL_REQUEST_TEMPLATE/feature.md"


def test_multi_dir_override_without_md_suffix(pila, repo):
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/bug.md", "b")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/feature.md", "f")
    _, rel = pila.find_pr_template(repo, override="feature")
    assert rel == ".github/PULL_REQUEST_TEMPLATE/feature.md"


def test_multi_dir_override_no_match_falls_back_to_first(pila, repo):
    # Bad override should NOT die — finalize must keep working. The
    # caller logs a warning; we just return the alphabetical default.
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/bug.md", "b")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/feature.md", "f")
    _, rel = pila.find_pr_template(repo, override="nonexistent")
    assert rel == ".github/PULL_REQUEST_TEMPLATE/bug.md"


def test_single_template_beats_multi_dir(pila, repo):
    # GitHub's canonical order: single .github/pull_request_template.md
    # outranks any PULL_REQUEST_TEMPLATE/ directory.
    _write(repo / ".github/pull_request_template.md", "single")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/feature.md", "multi")
    _, rel = pila.find_pr_template(repo)
    assert rel == ".github/pull_request_template.md"


def test_multi_dir_ignores_non_md(pila, repo):
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/feature.md", "f")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/README.txt", "ignore")
    _write(repo / ".github/PULL_REQUEST_TEMPLATE/.DS_Store", "ignore")
    _, rel = pila.find_pr_template(repo)
    assert rel == ".github/PULL_REQUEST_TEMPLATE/feature.md"


def test_multi_dir_empty_returns_none(pila, repo):
    (repo / ".github/PULL_REQUEST_TEMPLATE").mkdir(parents=True)
    # Empty directory with no .md files — should fall through to None,
    # not crash on sorted([]).
    assert pila.find_pr_template(repo) is None


def test_multi_dir_falls_through_when_empty(pila, repo):
    # Empty .github/PULL_REQUEST_TEMPLATE should NOT block discovery of
    # docs/PULL_REQUEST_TEMPLATE — the loop must keep scanning.
    (repo / ".github/PULL_REQUEST_TEMPLATE").mkdir(parents=True)
    _write(repo / "docs/PULL_REQUEST_TEMPLATE/feature.md", "docs-f")
    result = pila.find_pr_template(repo)
    assert result is not None
    _, rel = result
    assert rel == "docs/PULL_REQUEST_TEMPLATE/feature.md"


def test_resolve_pr_template_cli_wins(pila, repo, monkeypatch):
    monkeypatch.setenv(pila.PR_TEMPLATE_ENV, "from-env")
    assert pila.resolve_pr_template(repo, cli_value="from-cli") == "from-cli"


def test_resolve_pr_template_env_wins_over_toml(pila, repo, monkeypatch):
    (repo / "pila.toml").write_text('pr_template = "from-toml"\n')
    monkeypatch.setenv(pila.PR_TEMPLATE_ENV, "from-env")
    assert pila.resolve_pr_template(repo, cli_value=None) == "from-env"


def test_resolve_pr_template_toml_when_unset(pila, repo, monkeypatch):
    monkeypatch.delenv(pila.PR_TEMPLATE_ENV, raising=False)
    (repo / "pila.toml").write_text('pr_template = "from-toml"\n')
    assert pila.resolve_pr_template(repo, cli_value=None) == "from-toml"


def test_resolve_pr_template_none_when_nothing_set(pila, repo, monkeypatch):
    monkeypatch.delenv(pila.PR_TEMPLATE_ENV, raising=False)
    assert pila.resolve_pr_template(repo, cli_value=None) is None
