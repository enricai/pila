"""Tests for resolve_verbosity().

Mirrors test_resolve_source_of_truth.py's structure: CLI > env >
pila.toml > default, with bad values rejected at startup via die().
Also pins the 4-level enum and the default.

The -v/-vv/-q/-qq shortcuts are NOT exercised here — they are resolved
in main() and pass through resolve_verbosity as the cli_value
argument. See test_verbosity_shortcuts.py for the shortcut logic.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with PILA_VERBOSITY unset."""
    monkeypatch.delenv("PILA_VERBOSITY", raising=False)
    return tmp_path


def test_default_is_stream(pila):
    """The default is `stream` because a user invoking pila is
    typically opening to watch. See clig.dev / research notes."""
    assert pila.VERBOSITY_DEFAULT == "stream"


def test_four_levels(pila):
    """Pin the level enum so a future change that adds/removes a
    level is a deliberate choice and not a silent drift."""
    assert pila.VERBOSITY_VALUES == ("quiet", "normal", "stream", "debug")


def test_unset_falls_back_to_default(pila, repo_root):
    assert pila.resolve_verbosity(repo_root) == "stream"


def test_file_present_env_unset(pila, repo_root):
    (repo_root / "pila.toml").write_text("verbosity = quiet\n")
    assert pila.resolve_verbosity(repo_root) == "quiet"


def test_env_set(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_VERBOSITY", "debug")
    assert pila.resolve_verbosity(repo_root) == "debug"


def test_env_wins_over_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("verbosity = quiet\n")
    monkeypatch.setenv("PILA_VERBOSITY", "debug")
    assert pila.resolve_verbosity(repo_root) == "debug"


def test_cli_wins_over_env_and_file(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("verbosity = quiet\n")
    monkeypatch.setenv("PILA_VERBOSITY", "debug")
    assert pila.resolve_verbosity(repo_root, cli_value="normal") == "normal"


@pytest.mark.parametrize("value", ["quiet", "normal", "stream", "debug"])
def test_all_levels_accepted(pila, repo_root, value):
    (repo_root / "pila.toml").write_text(f"verbosity = {value}\n")
    assert pila.resolve_verbosity(repo_root) == value


def test_bad_file_value_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("verbosity = chatty\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_verbosity(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err
    assert "chatty" in err


def test_bad_env_value_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_VERBOSITY", "loud")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_verbosity(repo_root)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "is not one of" in err


def test_empty_env_treated_as_unset(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_VERBOSITY", "")
    assert pila.resolve_verbosity(repo_root) == "stream"


def test_cli_value_none_falls_back(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_VERBOSITY", "normal")
    assert pila.resolve_verbosity(repo_root, cli_value=None) == "normal"
