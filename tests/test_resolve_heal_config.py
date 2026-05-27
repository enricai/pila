"""Tests for resolve_heal_max_rounds() and resolve_heal_success_threshold().

Covers the CLI flag → env var → per-repo file → default resolution order,
validation (positive int for max_rounds, float in (0,1] for threshold),
and die() paths for invalid values.
Mirrors the structure of test_resolve_confidence_rounds.py.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# resolve_heal_max_rounds
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root_hmr(tmp_path, monkeypatch):
    """An empty repo-root with CENTELLA_HEAL_MAX_ROUNDS unset."""
    monkeypatch.delenv("CENTELLA_HEAL_MAX_ROUNDS", raising=False)
    return tmp_path


def test_heal_max_rounds_default(centella, repo_root_hmr):
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == centella.HEAL_MAX_ROUNDS_DEFAULT
    assert centella.HEAL_MAX_ROUNDS_DEFAULT == 10


def test_heal_max_rounds_file_value(centella, repo_root_hmr):
    (repo_root_hmr / "centella.toml").write_text("heal_max_rounds = 5\n")
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == 5


def test_heal_max_rounds_env_value(centella, repo_root_hmr, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "7")
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == 7


def test_heal_max_rounds_env_wins_over_file(centella, repo_root_hmr, monkeypatch):
    (repo_root_hmr / "centella.toml").write_text("heal_max_rounds = 5\n")
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "3")
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == 3


def test_heal_max_rounds_cli_wins_over_env_and_file(centella, repo_root_hmr, monkeypatch):
    (repo_root_hmr / "centella.toml").write_text("heal_max_rounds = 5\n")
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "3")
    assert centella.resolve_heal_max_rounds(repo_root_hmr, cli_value=20) == 20


def test_heal_max_rounds_cli_none_falls_back(centella, repo_root_hmr, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "6")
    assert centella.resolve_heal_max_rounds(repo_root_hmr, cli_value=None) == 6


def test_heal_max_rounds_bad_env_dies(centella, repo_root_hmr, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "not-a-number")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_max_rounds(repo_root_hmr)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a positive integer" in err


def test_heal_max_rounds_zero_env_dies(centella, repo_root_hmr, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "0")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_max_rounds(repo_root_hmr)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "positive integer" in err


def test_heal_max_rounds_negative_env_dies(centella, repo_root_hmr, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "-2")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_max_rounds(repo_root_hmr)
    assert exc.value.code != 0


def test_heal_max_rounds_bad_file_dies(centella, repo_root_hmr, capsys):
    (repo_root_hmr / "centella.toml").write_text("heal_max_rounds = bogus\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_max_rounds(repo_root_hmr)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "not a positive integer" in err


def test_heal_max_rounds_zero_file_dies(centella, repo_root_hmr, capsys):
    (repo_root_hmr / "centella.toml").write_text("heal_max_rounds = 0\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_max_rounds(repo_root_hmr)
    assert exc.value.code != 0


def test_heal_max_rounds_empty_env_treated_as_unset(centella, repo_root_hmr, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "")
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == centella.HEAL_MAX_ROUNDS_DEFAULT


def test_heal_max_rounds_whitespace_env_treated_as_unset(centella, repo_root_hmr, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_MAX_ROUNDS", "   ")
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == centella.HEAL_MAX_ROUNDS_DEFAULT


def test_heal_max_rounds_quoted_file_value(centella, repo_root_hmr):
    (repo_root_hmr / "centella.toml").write_text('heal_max_rounds = "15"\n')
    assert centella.resolve_heal_max_rounds(repo_root_hmr) == 15


# ---------------------------------------------------------------------------
# resolve_heal_success_threshold
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root_hst(tmp_path, monkeypatch):
    """An empty repo-root with CENTELLA_HEAL_SUCCESS_THRESHOLD unset."""
    monkeypatch.delenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", raising=False)
    return tmp_path


def test_heal_success_threshold_default(centella, repo_root_hst):
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(
        centella.HEAL_SUCCESS_THRESHOLD_DEFAULT)
    assert centella.HEAL_SUCCESS_THRESHOLD_DEFAULT == pytest.approx(0.9)


def test_heal_success_threshold_file_value(centella, repo_root_hst):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 0.8\n")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(0.8)


def test_heal_success_threshold_env_value(centella, repo_root_hst, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "0.75")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(0.75)


def test_heal_success_threshold_env_wins_over_file(centella, repo_root_hst, monkeypatch):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 0.8\n")
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "0.6")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(0.6)


def test_heal_success_threshold_cli_wins_over_env_and_file(centella, repo_root_hst, monkeypatch):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 0.8\n")
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "0.6")
    assert centella.resolve_heal_success_threshold(repo_root_hst, cli_value=0.95) == pytest.approx(0.95)


def test_heal_success_threshold_cli_none_falls_back(centella, repo_root_hst, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "0.7")
    assert centella.resolve_heal_success_threshold(repo_root_hst, cli_value=None) == pytest.approx(0.7)


def test_heal_success_threshold_accepts_one(centella, repo_root_hst):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 1.0\n")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(1.0)


def test_heal_success_threshold_bad_env_dies(centella, repo_root_hst, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "not-a-float")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "CENTELLA_HEAL_SUCCESS_THRESHOLD" in err


def test_heal_success_threshold_zero_env_dies(centella, repo_root_hst, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "0.0")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "must be in (0, 1]" in err


def test_heal_success_threshold_above_one_env_dies(centella, repo_root_hst, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "1.1")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "must be in (0, 1]" in err


def test_heal_success_threshold_bad_file_dies(centella, repo_root_hst, capsys):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = bogus\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0


def test_heal_success_threshold_zero_file_dies(centella, repo_root_hst, capsys):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 0.0\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0


def test_heal_success_threshold_above_one_file_dies(centella, repo_root_hst, capsys):
    (repo_root_hst / "centella.toml").write_text("heal_success_threshold = 1.5\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_heal_success_threshold(repo_root_hst)
    assert exc.value.code != 0


def test_heal_success_threshold_empty_env_treated_as_unset(centella, repo_root_hst, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(
        centella.HEAL_SUCCESS_THRESHOLD_DEFAULT)


def test_heal_success_threshold_whitespace_env_treated_as_unset(centella, repo_root_hst, monkeypatch):
    monkeypatch.setenv("CENTELLA_HEAL_SUCCESS_THRESHOLD", "   ")
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(
        centella.HEAL_SUCCESS_THRESHOLD_DEFAULT)


def test_heal_success_threshold_quoted_file_value(centella, repo_root_hst):
    (repo_root_hst / "centella.toml").write_text('heal_success_threshold = "0.85"\n')
    assert centella.resolve_heal_success_threshold(repo_root_hst) == pytest.approx(0.85)
