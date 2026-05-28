"""Tests for resolve_source_of_truth().

Covers the CLI flag → env var → per-repo file → 'both' resolution order,
the value enum, comment/whitespace handling, and the die() path for
invalid values.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with PILA_SOURCE_OF_TRUTH unset."""
    monkeypatch.delenv("PILA_SOURCE_OF_TRUTH", raising=False)
    return tmp_path


def test_file_present_env_unset(pila, repo_root):
    (repo_root / "pila.toml").write_text("source_of_truth = codebase\n")
    assert pila.resolve_source_of_truth(repo_root) == "codebase"


def test_file_absent_env_set(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "research")
    assert pila.resolve_source_of_truth(repo_root) == "research"


def test_both_unset_defaults_to_both(pila, repo_root):
    assert pila.resolve_source_of_truth(repo_root) == "both"


def test_env_wins_over_file(pila, repo_root, monkeypatch):
    # File/env priority was flipped when the --source-of-truth CLI flag
    # was added: env and CLI are session knobs and outrank the
    # committed pila.toml default.
    (repo_root / "pila.toml").write_text("source_of_truth = codebase\n")
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "research")
    assert pila.resolve_source_of_truth(repo_root) == "research"


def test_cli_value_wins_over_env_and_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("source_of_truth = codebase\n")
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "research")
    assert pila.resolve_source_of_truth(repo_root, cli_value="both") == "both"


def test_cli_value_none_falls_back(pila, repo_root, monkeypatch):
    # An unset --source-of-truth flag (None from argparse) is the same
    # as if the parameter weren't passed at all.
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "research")
    assert pila.resolve_source_of_truth(repo_root, cli_value=None) == "research"


def test_quoted_file_value(pila, repo_root):
    (repo_root / "pila.toml").write_text('source_of_truth = "both"\n')
    assert pila.resolve_source_of_truth(repo_root) == "both"


def test_single_quoted_file_value(pila, repo_root):
    (repo_root / "pila.toml").write_text("source_of_truth = 'research'\n")
    assert pila.resolve_source_of_truth(repo_root) == "research"


def test_comments_and_blank_lines_tolerated(pila, repo_root):
    (repo_root / "pila.toml").write_text(
        "# pila config\n\n  source_of_truth = research  \n# trailing\n"
    )
    assert pila.resolve_source_of_truth(repo_root) == "research"


@pytest.mark.parametrize("value", ["codebase", "research", "both"])
def test_all_three_values_accepted_in_file(pila, repo_root, value):
    (repo_root / "pila.toml").write_text(f"source_of_truth = {value}\n")
    assert pila.resolve_source_of_truth(repo_root) == value


@pytest.mark.parametrize("value", ["codebase", "research", "both"])
def test_all_three_values_accepted_in_env(pila, repo_root, monkeypatch, value):
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", value)
    assert pila.resolve_source_of_truth(repo_root) == value


def test_bad_file_value_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("source_of_truth = bogus\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_source_of_truth(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "bogus" in err


def test_bad_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "nope")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_source_of_truth(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "nope" in err


def test_empty_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "")
    assert pila.resolve_source_of_truth(repo_root) == "both"


def test_whitespace_only_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_SOURCE_OF_TRUTH", "   ")
    assert pila.resolve_source_of_truth(repo_root) == "both"
