"""Tests for resolve_no_push() — the --no-push preference resolver.

Covers the precedence order: CLI flag → PILA_NO_PUSH env var →
no_push in pila.toml → False (push by default per DESIGN §6).

Also covers boolean parsing (1/0, true/false, yes/no, on/off) and the
die() path for typos in env or TOML.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root directory with PILA_NO_PUSH unset."""
    monkeypatch.delenv("PILA_NO_PUSH", raising=False)
    return tmp_path


def test_default_is_push_enabled(pila, repo_root):
    """No CLI flag, no env, no file → False (push by default)."""
    assert pila.resolve_no_push(repo_root, cli_value=False) is False


def test_cli_flag_wins(pila, repo_root, monkeypatch):
    """--no-push CLI flag is the highest precedence."""
    monkeypatch.setenv("PILA_NO_PUSH", "0")
    (repo_root / "pila.toml").write_text("no_push = false\n")
    assert pila.resolve_no_push(repo_root, cli_value=True) is True


def test_env_set_true(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_NO_PUSH", "1")
    assert pila.resolve_no_push(repo_root, cli_value=False) is True


def test_env_set_false_falls_through_to_default(pila, repo_root, monkeypatch):
    """An env value of 'false' is an explicit "use the default" — but
    since the default is False, the result is False either way. Pin
    behavior so callers know an env-false isn't 'unset'."""
    monkeypatch.setenv("PILA_NO_PUSH", "false")
    assert pila.resolve_no_push(repo_root, cli_value=False) is False


def test_file_set_true_no_env(pila, repo_root):
    (repo_root / "pila.toml").write_text("no_push = true\n")
    assert pila.resolve_no_push(repo_root, cli_value=False) is True


def test_env_wins_over_file(pila, repo_root, monkeypatch):
    """Env is a session knob and outranks the committed pila.toml
    default — same precedence pattern as source-of-truth."""
    (repo_root / "pila.toml").write_text("no_push = true\n")
    monkeypatch.setenv("PILA_NO_PUSH", "false")
    assert pila.resolve_no_push(repo_root, cli_value=False) is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on", "ON"])
def test_env_truthy_spellings(pila, repo_root, monkeypatch, value):
    monkeypatch.setenv("PILA_NO_PUSH", value)
    assert pila.resolve_no_push(repo_root, cli_value=False) is True


@pytest.mark.parametrize("value", ["0", "false", "False", "FALSE", "no", "off", "OFF"])
def test_env_falsy_spellings(pila, repo_root, monkeypatch, value):
    monkeypatch.setenv("PILA_NO_PUSH", value)
    assert pila.resolve_no_push(repo_root, cli_value=False) is False


def test_env_garbage_dies(pila, repo_root, monkeypatch):
    """Unrecognized boolean spelling in env → die so a typo doesn't get
    silently treated as False (push by default would be a worse surprise)."""
    monkeypatch.setenv("PILA_NO_PUSH", "maybe")
    with pytest.raises(SystemExit):
        pila.resolve_no_push(repo_root, cli_value=False)


def test_file_garbage_dies(pila, repo_root):
    (repo_root / "pila.toml").write_text("no_push = sometimes\n")
    with pytest.raises(SystemExit):
        pila.resolve_no_push(repo_root, cli_value=False)


def test_env_empty_string_falls_through(pila, repo_root, monkeypatch):
    """PILA_NO_PUSH="" should be treated as unset, not as a value."""
    monkeypatch.setenv("PILA_NO_PUSH", "")
    assert pila.resolve_no_push(repo_root, cli_value=False) is False


def test_cli_false_with_env_true(pila, repo_root, monkeypatch):
    """CLI cli_value=False means '--no-push not passed' (action=store_true
    default). The env/TOML can still set no_push=True. CLI doesn't
    override env in this case because cli_value=False isn't an explicit
    'I want push on' signal — it's just 'I didn't pass --no-push'."""
    monkeypatch.setenv("PILA_NO_PUSH", "1")
    assert pila.resolve_no_push(repo_root, cli_value=False) is True
