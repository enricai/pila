"""Tests for resolve_confidence_rounds() and the confidence_rounds cap.

Covers the CLI flag → env var → per-repo file → DEFAULT_CAPS resolution
order, positive-int validation, and the die() path for invalid values.
Mirrors the structure of test_resolve_source_of_truth.py.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with PILA_CONFIDENCE_ROUNDS unset."""
    monkeypatch.delenv("PILA_CONFIDENCE_ROUNDS", raising=False)
    return tmp_path


def test_default_cap_is_eight(pila):
    assert pila.DEFAULT_CAPS["confidence_rounds"] == 8


def test_default_when_nothing_set(pila, repo_root):
    assert pila.resolve_confidence_rounds(repo_root) == 8


def test_file_value(pila, repo_root):
    (repo_root / "pila.toml").write_text("confidence_rounds = 12\n")
    assert pila.resolve_confidence_rounds(repo_root) == 12


def test_env_value(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "5")
    assert pila.resolve_confidence_rounds(repo_root) == 5


def test_env_wins_over_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("confidence_rounds = 12\n")
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "3")
    assert pila.resolve_confidence_rounds(repo_root) == 3


def test_cli_wins_over_env_and_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("confidence_rounds = 12\n")
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "3")
    assert pila.resolve_confidence_rounds(repo_root, cli_value=20) == 20


def test_cli_none_falls_back(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "3")
    assert pila.resolve_confidence_rounds(repo_root, cli_value=None) == 3


def test_bad_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "not-a-number")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_confidence_rounds(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a positive integer" in err


def test_zero_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "0")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_confidence_rounds(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a positive integer" in err


def test_negative_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "-3")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_confidence_rounds(repo_root)
    assert exc.value.code != 0


def test_bad_file_value_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("confidence_rounds = bogus\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_confidence_rounds(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a positive integer" in err


def test_zero_file_value_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("confidence_rounds = 0\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_confidence_rounds(repo_root)
    assert exc.value.code != 0


def test_empty_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "")
    assert pila.resolve_confidence_rounds(repo_root) == 8


def test_whitespace_only_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_CONFIDENCE_ROUNDS", "   ")
    assert pila.resolve_confidence_rounds(repo_root) == 8


def test_positive_int_argparse_helper(pila):
    """The _positive_int argparse type helper rejects bad values with the
    standard ArgumentTypeError so argparse surfaces a clean error."""
    import argparse
    assert pila._positive_int("8") == 8
    assert pila._positive_int("1") == 1
    with pytest.raises(argparse.ArgumentTypeError):
        pila._positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        pila._positive_int("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        pila._positive_int("nope")
