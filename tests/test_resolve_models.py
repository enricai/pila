"""Tests for resolve_models().

Per-worker precedence (highest first):
  1. --model-<worker> CLI flag
  2. --model CLI flag
  3. PILA_MODEL_<WORKER> env var
  4. PILA_MODEL env var
  5. model_<worker> in pila.toml
  6. model in pila.toml
  7. MODEL_DEFAULT_PER_WORKER[<worker>] (e.g. implementer → sonnet)
  8. MODEL_DEFAULT (opus)

The judgment-vs-implementation default split was introduced when the
reconciler worker landed: classifier / planner / reconciler / integrator
all default to opus, implementer / conformer default to sonnet (cost
mitigation for workers that run most often).
"""
from __future__ import annotations

import argparse

import pytest


WORKERS = ("classifier", "planner", "reconciler", "implementer",
           "integrator", "conformer")

# The expected default per worker, with no overrides.
DEFAULTS = {
    "classifier": "opus",
    "planner":    "opus",
    "reconciler": "opus",
    "integrator": "opus",
    "implementer": "sonnet",
    "conformer":  "sonnet",
}


def ns(**overrides):
    """Build an argparse.Namespace with --model and every --model-<w>
    defaulted to None (the argparse default when the flag isn't passed)."""
    base = {"model": None, **{f"model_{w}": None for w in WORKERS}}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """An empty repo-root with every PILA_MODEL* env var unset."""
    monkeypatch.delenv("PILA_MODEL", raising=False)
    for w in WORKERS:
        monkeypatch.delenv(f"PILA_MODEL_{w.upper()}", raising=False)
    return tmp_path


def test_all_unset_defaults_per_worker(pila, repo_root):
    """With no overrides, judgment workers default to opus and the
    implementer defaults to sonnet. Pins both the global default and
    the per-worker override table together.

    resolve_models() also resolves the post-run skill workers (judge, heal);
    we check only the WORKER_TYPES slice here so this test doesn't need
    updating when additional post-run workers are added."""
    models = pila.resolve_models(repo_root, ns())
    worker_slice = {w: models[w] for w in WORKERS}
    assert worker_slice == DEFAULTS
    assert pila.MODEL_DEFAULT == "opus"
    # implementer, judge and heal are the current per-worker overrides.
    assert pila.MODEL_DEFAULT_PER_WORKER.get("implementer") == "sonnet"
    assert pila.MODEL_DEFAULT_PER_WORKER.get("judge") == "sonnet"
    assert pila.MODEL_DEFAULT_PER_WORKER.get("heal") == "sonnet"


def test_global_env_applies_to_every_worker(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_MODEL", "opus")
    models = pila.resolve_models(repo_root, ns())
    assert {w: models[w] for w in WORKERS} == {w: "opus" for w in WORKERS}


def test_per_worker_env_overrides_global_env(pila, repo_root, monkeypatch):
    monkeypatch.setenv("PILA_MODEL", "haiku")
    monkeypatch.setenv("PILA_MODEL_IMPLEMENTER", "opus")
    models = pila.resolve_models(repo_root, ns())
    assert models["implementer"] == "opus"
    for w in WORKERS:
        if w != "implementer":
            assert models[w] == "haiku"


def test_global_toml_applies_to_every_worker(pila, repo_root):
    (repo_root / "pila.toml").write_text("model = opus\n")
    models = pila.resolve_models(repo_root, ns())
    assert {w: models[w] for w in WORKERS} == {w: "opus" for w in WORKERS}


def test_per_worker_toml_overrides_global_toml(pila, repo_root):
    (repo_root / "pila.toml").write_text(
        "model = opus\nmodel_integrator = haiku\n")
    models = pila.resolve_models(repo_root, ns())
    assert models["integrator"] == "haiku"
    for w in WORKERS:
        if w != "integrator":
            assert models[w] == "opus"


def test_env_beats_toml(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("model = haiku\n")
    monkeypatch.setenv("PILA_MODEL", "opus")
    models = pila.resolve_models(repo_root, ns())
    assert {w: models[w] for w in WORKERS} == {w: "opus" for w in WORKERS}


def test_global_cli_beats_global_env_and_toml(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text("model = haiku\n")
    monkeypatch.setenv("PILA_MODEL", "haiku")
    models = pila.resolve_models(repo_root, ns(model="opus"))
    assert {w: models[w] for w in WORKERS} == {w: "opus" for w in WORKERS}


def test_per_worker_cli_beats_everything(pila, repo_root, monkeypatch):
    (repo_root / "pila.toml").write_text(
        "model = haiku\nmodel_planner = haiku\n")
    monkeypatch.setenv("PILA_MODEL", "haiku")
    monkeypatch.setenv("PILA_MODEL_PLANNER", "haiku")
    models = pila.resolve_models(repo_root,
                                     ns(model="haiku", model_planner="opus"))
    assert models["planner"] == "opus"
    for w in WORKERS:
        if w != "planner":
            assert models[w] == "haiku"


def test_full_precedence_per_worker(pila, repo_root, monkeypatch):
    # Per-worker CLI > global CLI > per-worker env > global env >
    # per-worker TOML > global TOML > per-worker default > MODEL_DEFAULT
    # — exercise one rung at a time on the same worker (planner).
    cfg = repo_root / "pila.toml"

    # rung 8 (MODEL_DEFAULT, planner has no per-worker override → opus)
    assert pila.resolve_models(repo_root, ns())["planner"] == "opus"

    # rung 6: global TOML beats default
    cfg.write_text("model = haiku\n")
    assert pila.resolve_models(repo_root, ns())["planner"] == "haiku"

    # rung 5: per-worker TOML beats global TOML
    cfg.write_text("model = haiku\nmodel_planner = sonnet\n")
    assert pila.resolve_models(repo_root, ns())["planner"] == "sonnet"

    # rung 4: global env beats both TOML rungs
    monkeypatch.setenv("PILA_MODEL", "opus")
    assert pila.resolve_models(repo_root, ns())["planner"] == "opus"

    # rung 3: per-worker env beats global env
    monkeypatch.setenv("PILA_MODEL_PLANNER", "haiku")
    assert pila.resolve_models(repo_root, ns())["planner"] == "haiku"

    # rung 2: global CLI beats env (per-worker CLI still unset)
    assert pila.resolve_models(repo_root, ns(model="sonnet"))["planner"] == "sonnet"

    # rung 1: per-worker CLI beats global CLI
    assert pila.resolve_models(
        repo_root, ns(model="sonnet", model_planner="opus"))["planner"] == "opus"


def test_bad_global_env_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_MODEL", "gpt5")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "PILA_MODEL" in err
    assert "gpt5" in err
    assert "is not one of" in err


def test_bad_per_worker_env_dies(pila, repo_root, monkeypatch, capsys):
    monkeypatch.setenv("PILA_MODEL_INTEGRATOR", "claude2")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "PILA_MODEL_INTEGRATOR" in err
    assert "claude2" in err


def test_bad_global_toml_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("model = bogus\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "pila.toml" in err
    assert "bogus" in err
    assert "is not one of" in err


def test_bad_per_worker_toml_dies(pila, repo_root, capsys):
    (repo_root / "pila.toml").write_text("model_classifier = nope\n")
    with pytest.raises(SystemExit) as exc:
        pila.resolve_models(repo_root, ns())
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "model_classifier" in err
    assert "nope" in err


def test_empty_env_treated_as_unset(pila, repo_root, monkeypatch):
    """Empty / whitespace-only env vars fall through as if unset, so the
    per-worker default table (DEFAULTS) wins — not "sonnet for all".
    Pins that the strip-then-falsy check in resolve_models hasn't been
    replaced with a default-value substitution."""
    monkeypatch.setenv("PILA_MODEL", "")
    monkeypatch.setenv("PILA_MODEL_PLANNER", "   ")
    models = pila.resolve_models(repo_root, ns())
    # Check only the WORKER_TYPES slice; post-run skill workers (judge, heal)
    # also appear in the returned dict but are not part of DEFAULTS.
    assert {w: models[w] for w in WORKERS} == DEFAULTS


def test_every_alias_accepted_in_global_env(pila, repo_root, monkeypatch):
    for alias in pila.MODEL_VALUES:
        monkeypatch.setenv("PILA_MODEL", alias)
        models = pila.resolve_models(repo_root, ns())
        assert {w: models[w] for w in WORKERS} == {w: alias for w in WORKERS}


def test_worker_types_match_expected_set(pila):
    # If a new worker type is added to the orchestrator, this test
    # catches that the test suite needs to extend WORKERS too.
    assert set(pila.WORKER_TYPES) == set(WORKERS)
