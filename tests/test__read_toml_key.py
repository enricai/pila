"""Tests for _read_toml_key().

The hand-rolled pila.toml line parser used by both
resolve_source_of_truth() and resolve_models(). Both resolvers depend
on its quoting/comment/whitespace behavior, so it gets dedicated
coverage.
"""
from __future__ import annotations

import pytest


def test_missing_file_returns_none(pila, tmp_path):
    assert pila._read_toml_key(tmp_path / "no.toml", "anything") is None


def test_missing_key_returns_none(pila, tmp_path):
    (tmp_path / "pila.toml").write_text("other_key = value\n")
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") is None


def test_bare_value(pila, tmp_path):
    (tmp_path / "pila.toml").write_text("model = opus\n")
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "opus"


def test_double_quoted_value(pila, tmp_path):
    (tmp_path / "pila.toml").write_text('model = "opus"\n')
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "opus"


def test_single_quoted_value(pila, tmp_path):
    (tmp_path / "pila.toml").write_text("model = 'haiku'\n")
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "haiku"


def test_comments_and_blank_lines_skipped(pila, tmp_path):
    (tmp_path / "pila.toml").write_text(
        "# header comment\n"
        "\n"
        "  model = sonnet  \n"
        "# trailing\n"
    )
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "sonnet"


def test_first_matching_key_wins(pila, tmp_path):
    # If the file has the same key twice, the first occurrence is
    # returned. This isn't a documented guarantee, but it's the
    # behavior and it should stay deterministic across runs.
    (tmp_path / "pila.toml").write_text("model = opus\nmodel = haiku\n")
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "opus"


def test_key_substring_does_not_match(pila, tmp_path):
    # `model_planner` should not be returned when asked for `model`.
    (tmp_path / "pila.toml").write_text(
        "model_planner = opus\nmodel = haiku\n")
    assert pila._read_toml_key(tmp_path / "pila.toml", "model") == "haiku"
    assert pila._read_toml_key(
        tmp_path / "pila.toml", "model_planner") == "opus"
