"""Tests for resolve_source_of_truth().

Covers the per-repo file → env var → 'ask' resolution order, the value
enum, comment/whitespace handling, and the die() path for invalid values.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with CENTELLA_SOURCE_OF_TRUTH unset."""
    monkeypatch.delenv("CENTELLA_SOURCE_OF_TRUTH", raising=False)
    return tmp_path


def test_file_present_env_unset(centella, repo_root):
    (repo_root / "centella.toml").write_text("source_of_truth = codebase\n")
    assert centella.resolve_source_of_truth(repo_root) == "codebase"


def test_file_absent_env_set(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", "research")
    assert centella.resolve_source_of_truth(repo_root) == "research"


def test_both_unset_defaults_to_ask(centella, repo_root):
    assert centella.resolve_source_of_truth(repo_root) == "ask"


def test_file_wins_over_env(centella, repo_root, monkeypatch):
    (repo_root / "centella.toml").write_text("source_of_truth = codebase\n")
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", "research")
    assert centella.resolve_source_of_truth(repo_root) == "codebase"


def test_quoted_file_value(centella, repo_root):
    (repo_root / "centella.toml").write_text('source_of_truth = "both"\n')
    assert centella.resolve_source_of_truth(repo_root) == "both"


def test_single_quoted_file_value(centella, repo_root):
    (repo_root / "centella.toml").write_text("source_of_truth = 'research'\n")
    assert centella.resolve_source_of_truth(repo_root) == "research"


def test_comments_and_blank_lines_tolerated(centella, repo_root):
    (repo_root / "centella.toml").write_text(
        "# centella config\n\n  source_of_truth = research  \n# trailing\n"
    )
    assert centella.resolve_source_of_truth(repo_root) == "research"


@pytest.mark.parametrize("value", ["codebase", "research", "both", "ask"])
def test_all_four_values_accepted_in_file(centella, repo_root, value):
    (repo_root / "centella.toml").write_text(f"source_of_truth = {value}\n")
    assert centella.resolve_source_of_truth(repo_root) == value


@pytest.mark.parametrize("value", ["codebase", "research", "both", "ask"])
def test_all_four_values_accepted_in_env(centella, repo_root, monkeypatch, value):
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", value)
    assert centella.resolve_source_of_truth(repo_root) == value


def test_bad_file_value_dies(centella, repo_root, capsys):
    (repo_root / "centella.toml").write_text("source_of_truth = bogus\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_source_of_truth(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "bogus" in err


def test_bad_env_value_dies(centella, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", "nope")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_source_of_truth(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "nope" in err


def test_empty_env_treated_as_unset(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", "")
    assert centella.resolve_source_of_truth(repo_root) == "ask"


def test_whitespace_only_env_treated_as_unset(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_SOURCE_OF_TRUTH", "   ")
    assert centella.resolve_source_of_truth(repo_root) == "ask"
