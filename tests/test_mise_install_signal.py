"""Tests for the no-version-signals short-circuit in
`run_mise_install` (DESIGN §6½).

A repo with no version files at all (no `mise.toml`, no
`.tool-versions`, no idiomatic files, no `.go-version`) should NOT
invoke `mise install` — there's nothing for mise to do, and the
image-baked LTS Node and Python on PATH are the LTS-fallback story
the design promises. Without this guard, mise's exact behavior for
`mise install` with zero declared tools could die() with a confusing
"no tools to install" error and break unversioned repos.

The signal detection itself is a pure file-presence check; we test it
directly. The async run_mise_install no-signals path is tested by
verifying it does NOT shell out (no mise binary needed in CI).
"""
from __future__ import annotations

import asyncio


def _make_state(pila, tmp_path):
    pila_root = tmp_path / ".pila"
    run_id = "_test-run"
    (pila_root / "runs" / run_id / "logs").mkdir(parents=True, exist_ok=True)
    st = pila.State(pila_root, run_id)
    st.data = {"task": "test"}
    st.save()
    return st


def test_no_signals_returns_false(pila, tmp_path):
    """An empty repo has no version pin signals."""
    assert pila._repo_has_version_signal(tmp_path, None) is False


def test_mise_toml_is_a_signal(pila, tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20.11.0"\n')
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_dot_mise_toml_is_a_signal(pila, tmp_path):
    """mise supports both `mise.toml` and `.mise.toml`; both should
    count as signals."""
    (tmp_path / ".mise.toml").write_text('[tools]\n')
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_tool_versions_is_a_signal(pila, tmp_path):
    (tmp_path / ".tool-versions").write_text("node 20.11.0\n")
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_nvmrc_is_a_signal(pila, tmp_path):
    (tmp_path / ".nvmrc").write_text("20.11.0\n")
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_python_version_is_a_signal(pila, tmp_path):
    (tmp_path / ".python-version").write_text("3.11.7\n")
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_go_version_is_a_signal(pila, tmp_path):
    (tmp_path / ".go-version").write_text("1.22.3\n")
    assert pila._repo_has_version_signal(tmp_path, None) is True


def test_synth_override_path_is_a_signal_even_without_files(pila, tmp_path):
    """When pila has already synthesized an override (e.g. from go.mod),
    the override file path is the signal — mise will read it via
    MISE_OVERRIDE_CONFIG_FILENAMES — even if no idiomatic file is
    present."""
    override = tmp_path / ".pila" / "runs" / "x" / "mise-overrides.toml"
    override.parent.mkdir(parents=True)
    override.write_text('[tools]\ngo = "1.22"\n')
    assert pila._repo_has_version_signal(tmp_path, override) is True


def test_run_mise_install_no_signals_short_circuits(pila, tmp_path):
    """The no-signals path is a logged no-op: `mise install` is never
    shelled out, `mise_versions` is set to an empty dict so downstream
    consumers can rely on its presence."""
    st = _make_state(pila, tmp_path)
    log_dir = st.run_dir / "logs"
    # No version files — should short-circuit without shelling out.
    # If it tried to invoke mise, the test would either find a real
    # mise binary (and install something — bad) or fail with
    # FileNotFoundError. The early return prevents both.
    asyncio.run(pila.run_mise_install(tmp_path, log_dir, st, None))
    assert st.data["provision"]["mise_versions"] == {}
