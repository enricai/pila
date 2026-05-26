"""Tests for resolve_models().

Per-worker precedence (highest first):
  1. --model-<worker> CLI flag
  2. --model CLI flag
  3. CENTELLA_MODEL_<WORKER> env var
  4. CENTELLA_MODEL env var
  5. model_<worker> in centella.toml
  6. model in centella.toml
  7. MODEL_DEFAULT_PER_WORKER[<worker>] (e.g. implementer → sonnet)
  8. MODEL_DEFAULT (opus)

The judgment-vs-implementation default split was introduced when the
reconciler worker landed: classifier / planner / reconciler / integrator
/ validator all default to opus, implementer defaults to sonnet (cost
mitigation for the worker that runs most often).
"""
from __future__ import annotations

import argparse

import pytest


WORKERS = ("classifier", "planner", "reconciler", "implementer",
           "integrator", "validator")

# The expected default per worker, with no overrides.
DEFAULTS = {
    "classifier": "opus",
    "planner":    "opus",
    "reconciler": "opus",
    "integrator": "opus",
    "validator":  "opus",
    "implementer": "sonnet",
}


def ns(**overrides):
    """Build an argparse.Namespace with --model and every --model-<w>
    defaulted to None (the argparse default when the flag isn't passed)."""
    base = {"model": None, **{f"model_{w}": None for w in WORKERS}}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root with every CENTELLA_MODEL* env var unset."""
    monkeypatch.delenv("CENTELLA_MODEL", raising=False)
    for w in WORKERS:
        monkeypatch.delenv(f"CENTELLA_MODEL_{w.upper()}", raising=False)
    return tmp_path


def test_all_unset_defaults_per_worker(centella, repo_root):
    """With no overrides, judgment workers default to opus and the
    implementer defaults to sonnet. Pins both the global default and
    the per-worker override table together."""
    models = centella.resolve_models(repo_root, ns())
    assert models == DEFAULTS
    assert centella.MODEL_DEFAULT == "opus"
    assert centella.MODEL_DEFAULT_PER_WORKER == {"implementer": "sonnet"}


def test_global_env_applies_to_every_worker(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_MODEL", "opus")
    models = centella.resolve_models(repo_root, ns())
    assert models == {w: "opus" for w in WORKERS}


def test_per_worker_env_overrides_global_env(centella, repo_root, monkeypatch):
    monkeypatch.setenv("CENTELLA_MODEL", "haiku")
    monkeypatch.setenv("CENTELLA_MODEL_IMPLEMENTER", "opus")
    models = centella.resolve_models(repo_root, ns())
    assert models["implementer"] == "opus"
    for w in WORKERS:
        if w != "implementer":
            assert models[w] == "haiku"


def test_global_toml_applies_to_every_worker(centella, repo_root):
    (repo_root / "centella.toml").write_text("model = opus\n")
    models = centella.resolve_models(repo_root, ns())
    assert models == {w: "opus" for w in WORKERS}


def test_per_worker_toml_overrides_global_toml(centella, repo_root):
    (repo_root / "centella.toml").write_text(
        "model = opus\nmodel_validator = haiku\n")
    models = centella.resolve_models(repo_root, ns())
    assert models["validator"] == "haiku"
    for w in WORKERS:
        if w != "validator":
            assert models[w] == "opus"


def test_env_beats_toml(centella, repo_root, monkeypatch):
    (repo_root / "centella.toml").write_text("model = haiku\n")
    monkeypatch.setenv("CENTELLA_MODEL", "opus")
    models = centella.resolve_models(repo_root, ns())
    assert models == {w: "opus" for w in WORKERS}


def test_global_cli_beats_global_env_and_toml(centella, repo_root, monkeypatch):
    (repo_root / "centella.toml").write_text("model = haiku\n")
    monkeypatch.setenv("CENTELLA_MODEL", "haiku")
    models = centella.resolve_models(repo_root, ns(model="opus"))
    assert models == {w: "opus" for w in WORKERS}


def test_per_worker_cli_beats_everything(centella, repo_root, monkeypatch):
    (repo_root / "centella.toml").write_text(
        "model = haiku\nmodel_planner = haiku\n")
    monkeypatch.setenv("CENTELLA_MODEL", "haiku")
    monkeypatch.setenv("CENTELLA_MODEL_PLANNER", "haiku")
    models = centella.resolve_models(repo_root,
                                     ns(model="haiku", model_planner="opus"))
    assert models["planner"] == "opus"
    for w in WORKERS:
        if w != "planner":
            assert models[w] == "haiku"


def test_full_precedence_per_worker(centella, repo_root, monkeypatch):
    # Per-worker CLI > global CLI > per-worker env > global env >
    # per-worker TOML > global TOML > per-worker default > MODEL_DEFAULT
    # — exercise one rung at a time on the same worker (planner).
    cfg = repo_root / "centella.toml"

    # rung 8 (MODEL_DEFAULT, planner has no per-worker override → opus)
    assert centella.resolve_models(repo_root, ns())["planner"] == "opus"

    # rung 6: global TOML beats default
    cfg.write_text("model = haiku\n")
    assert centella.resolve_models(repo_root, ns())["planner"] == "haiku"

    # rung 5: per-worker TOML beats global TOML
    cfg.write_text("model = haiku\nmodel_planner = sonnet\n")
    assert centella.resolve_models(repo_root, ns())["planner"] == "sonnet"

    # rung 4: global env beats both TOML rungs
    monkeypatch.setenv("CENTELLA_MODEL", "opus")
    assert centella.resolve_models(repo_root, ns())["planner"] == "opus"

    # rung 3: per-worker env beats global env
    monkeypatch.setenv("CENTELLA_MODEL_PLANNER", "haiku")
    assert centella.resolve_models(repo_root, ns())["planner"] == "haiku"

    # rung 2: global CLI beats env (per-worker CLI still unset)
    assert centella.resolve_models(repo_root, ns(model="sonnet"))["planner"] == "sonnet"

    # rung 1: per-worker CLI beats global CLI
    assert centella.resolve_models(
        repo_root, ns(model="sonnet", model_planner="opus"))["planner"] == "opus"


def test_bad_global_env_dies(centella, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_MODEL", "gpt5")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "CENTELLA_MODEL" in err
    assert "gpt5" in err
    assert "is not one of" in err


def test_bad_per_worker_env_dies(centella, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("CENTELLA_MODEL_INTEGRATOR", "claude2")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "CENTELLA_MODEL_INTEGRATOR" in err
    assert "claude2" in err


def test_bad_global_toml_dies(centella, repo_root, capsys):
    (repo_root / "centella.toml").write_text("model = bogus\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "centella.toml" in err
    assert "bogus" in err
    assert "is not one of" in err


def test_bad_per_worker_toml_dies(centella, repo_root, capsys):
    (repo_root / "centella.toml").write_text("model_classifier = nope\n")
    with pytest.raises(SystemExit) as exc:
        centella.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "model_classifier" in err
    assert "nope" in err


def test_empty_env_treated_as_unset(centella, repo_root, monkeypatch):
    """Empty / whitespace-only env vars fall through as if unset, so the
    per-worker default table (DEFAULTS) wins — not "sonnet for all".
    Pins that the strip-then-falsy check in resolve_models hasn't been
    replaced with a default-value substitution."""
    monkeypatch.setenv("CENTELLA_MODEL", "")
    monkeypatch.setenv("CENTELLA_MODEL_PLANNER", "   ")
    models = centella.resolve_models(repo_root, ns())
    assert models == DEFAULTS


def test_every_alias_accepted_in_global_env(centella, repo_root, monkeypatch):
    for alias in centella.MODEL_VALUES:
        monkeypatch.setenv("CENTELLA_MODEL", alias)
        assert centella.resolve_models(repo_root, ns()) == {w: alias for w in WORKERS}


def test_worker_types_match_expected_set(centella):
    # If a new worker type is added to the orchestrator, this test
    # catches that the test suite needs to extend WORKERS too.
    assert set(centella.WORKER_TYPES) == set(WORKERS)
