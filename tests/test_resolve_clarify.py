"""Tests for resolve_clarify().

Covers the CLI flag → env var → per-repo file → False resolution order,
boolean parsing, and the die() path for invalid env / file values.
Mirrors test_resolve_source_of_truth.py / test_resolve_no_push.py
because resolve_clarify() and resolve_no_push() share the same shape
via _resolve_bool_pref().
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with CENTELLA_CLARIFY unset."""
    monkeypatch.delenv("CENTELLA_CLARIFY", raising=False)
    return tmp_path


def test_cli_true_wins(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_CLARIFY", "false")
    (repo_root / "centella.toml").write_text("clarify = false\n")
    assert centella.resolve_clarify(repo_root, cli_value=True) is True


def test_cli_false_falls_back_to_env(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_CLARIFY", "true")
    assert centella.resolve_clarify(repo_root, cli_value=False) is True


def test_env_wins_over_file(centella, repo_root, monkeypatch):
    (repo_root / "centella.toml").write_text("clarify = false\n")
    monkeypatch.setenv("CENTELLA_CLARIFY", "true")
    assert centella.resolve_clarify(repo_root, cli_value=False) is True


def test_file_when_env_unset(centella, repo_root):
    (repo_root / "centella.toml").write_text("clarify = true\n")
    assert centella.resolve_clarify(repo_root, cli_value=False) is True


def test_default_false_when_all_unset(centella, repo_root):
    assert centella.resolve_clarify(repo_root, cli_value=False) is False


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_truthy_env_values(centella, repo_root, monkeypatch, truthy):
    monkeypatch.setenv("CENTELLA_CLARIFY", truthy)
    assert centella.resolve_clarify(repo_root, cli_value=False) is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "FALSE"])
def test_falsy_env_values(centella, repo_root, monkeypatch, falsy):
    monkeypatch.setenv("CENTELLA_CLARIFY", falsy)
    assert centella.resolve_clarify(repo_root, cli_value=False) is False


def test_empty_env_treated_as_unset(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_CLARIFY", "")
    (repo_root / "centella.toml").write_text("clarify = true\n")
    # empty env should not short-circuit; the file takes over.
    assert centella.resolve_clarify(repo_root, cli_value=False) is True


def test_bad_env_value_dies(centella, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_CLARIFY", "maybe")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_clarify(repo_root, cli_value=False)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a boolean" in err


def test_bad_file_value_dies(centella, repo_root, capsys):
    (repo_root / "centella.toml").write_text("clarify = sometimes\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_clarify(repo_root, cli_value=False)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a boolean" in err
