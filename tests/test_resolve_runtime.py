"""Tests for resolve_runtime().

Covers the CLI flag → env var → per-repo file → 'local' resolution order,
the value enum, comment/whitespace handling, and the die() path for
invalid values.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with PILA_RUNTIME unset."""
    monkeypatch.delenv("PILA_RUNTIME", raising=False)
    return tmp_path


def test_default_is_local(pila, repo_root):
    assert pila.resolve_runtime(repo_root) == "local"


def test_file_present_env_unset(pila, repo_root):
    (repo_root / "pila.toml").write_text("runtime = fly\n")
    assert pila.resolve_runtime(repo_root) == "fly"


def test_file_absent_env_set(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_RUNTIME", "fly")
    assert pila.resolve_runtime(repo_root) == "fly"


def test_env_wins_over_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("runtime = fly\n")
    monkeypatch.setenv("PILA_RUNTIME", "local")
    assert pila.resolve_runtime(repo_root) == "local"


def test_cli_value_wins_over_env_and_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("runtime = fly\n")
    monkeypatch.setenv("PILA_RUNTIME", "fly")
    assert pila.resolve_runtime(repo_root, cli_value="local") == "local"


def test_cli_value_none_falls_back(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_RUNTIME", "fly")
    assert pila.resolve_runtime(repo_root, cli_value=None) == "fly"


def test_quoted_file_value(pila, repo_root):
    (repo_root / "pila.toml").write_text('runtime = "fly"\n')
    assert pila.resolve_runtime(repo_root) == "fly"


def test_single_quoted_file_value(pila, repo_root):
    (repo_root / "pila.toml").write_text("runtime = 'local'\n")
    assert pila.resolve_runtime(repo_root) == "local"


def test_comments_and_blank_lines_tolerated(pila, repo_root):
    (repo_root / "pila.toml").write_text(
        "# pila config\n\n  runtime = fly  \n# trailing\n"
    )
    assert pila.resolve_runtime(repo_root) == "fly"


@pytest.mark.parametrize("value", ["local", "fly"])
def test_both_values_accepted_in_file(pila, repo_root, value):
    (repo_root / "pila.toml").write_text(f"runtime = {value}\n")
    assert pila.resolve_runtime(repo_root) == value


@pytest.mark.parametrize("value", ["local", "fly"])
def test_both_values_accepted_in_env(pila, repo_root, monkeypatch, value):
    monkeypatch.setenv("PILA_RUNTIME", value)
    assert pila.resolve_runtime(repo_root) == value


def test_bad_file_value_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("runtime = bogus\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_runtime(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "bogus" in err


def test_bad_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_RUNTIME", "nope")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_runtime(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "nope" in err


def test_empty_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_RUNTIME", "")
    assert pila.resolve_runtime(repo_root) == "local"


def test_whitespace_only_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_RUNTIME", "   ")
    assert pila.resolve_runtime(repo_root) == "local"
