"""Tests that `replay_provision_in_worktree` propagates
`MISE_OVERRIDE_CONFIG_FILENAMES` from state into the per-worktree
subprocess env.

Why this matters (DESIGN §6½):
`phase_provision` synthesizes a mise override at
`.pila/runs/<id>/mise-overrides.toml` when a polyglot repo needs a
synthesized go pin (`go.mod` + idiomatic files, no `.go-version`).
The override file is NOT in the worktree's tracked-file set (it lives
under `.pila/`, which is git-ignored). So mise's discovery in the
worktree wouldn't see the synth — and `mise exec -- go ...` would
fall through to system PATH, where Go isn't installed.

Persisting `override_file` in state and re-exporting it in the replay
bridges the gap.
"""
from __future__ import annotations

import asyncio


def _make_state_with_recipe(pila, tmp_path, *, override_path: str | None,
                             recipe: list[dict]):
    pila_root = tmp_path / ".pila"
    run_id = "_test-replay"
    (pila_root / "runs" / run_id / "logs").mkdir(parents=True, exist_ok=True)
    st = pila.State(pila_root, run_id)
    st.data = {
        "task": "test",
        "provision": {
            "recipe": recipe,
            "override_file": override_path,
        },
    }
    st.save()
    return st


def test_replay_no_recipe_short_circuits(pila, tmp_path, monkeypatch):
    """Empty recipe: replay must NOT shell out at all (would fail in
    CI without a real `mise` binary)."""
    called = []

    async def fake_exec(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("should not be called")

    monkeypatch.setattr(pila.asyncio, "create_subprocess_exec", fake_exec)
    st = _make_state_with_recipe(
        pila, tmp_path, override_path=None, recipe=[])
    asyncio.run(pila.replay_provision_in_worktree(tmp_path, st))
    assert not called


def test_replay_kind_none_is_skipped(pila, tmp_path, monkeypatch):
    """Docs-only short-circuit: a recipe with only `kind: none` entries
    must not invoke any subprocess."""
    called = []

    async def fake_exec(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("should not be called")

    monkeypatch.setattr(pila.asyncio, "create_subprocess_exec", fake_exec)
    st = _make_state_with_recipe(
        pila, tmp_path, override_path=None,
        recipe=[{"kind": "none", "command": [], "working_dir": ".",
                 "timeout_s": 0}])
    asyncio.run(pila.replay_provision_in_worktree(tmp_path, st))
    assert not called


def test_replay_exports_override_env_when_set(pila, tmp_path, monkeypatch):
    """The load-bearing test: when state carries an override_file path,
    the env passed to the subprocess MUST include
    MISE_OVERRIDE_CONFIG_FILENAMES pointing at it. Without this, the
    polyglot-go-with-something case silently breaks."""
    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(pila.asyncio, "create_subprocess_exec", fake_exec)
    override = str(tmp_path / "fake-override.toml")
    st = _make_state_with_recipe(
        pila, tmp_path, override_path=override,
        recipe=[{
            "kind": "install",
            "command": ["go", "mod", "download"],
            "working_dir": ".",
            "timeout_s": 600,
        }],
    )
    asyncio.run(pila.replay_provision_in_worktree(tmp_path, st))

    # subprocess was invoked once with mise exec -- ...
    assert captured["args"][:3] == ("mise", "exec", "--"), \
        f"command should be wrapped with mise exec; got {captured['args']}"

    env = captured["kwargs"]["env"]
    assert "MISE_OVERRIDE_CONFIG_FILENAMES" in env, (
        "replay must export MISE_OVERRIDE_CONFIG_FILENAMES when state "
        "carries an override_file — without it, mise's discovery in the "
        "worktree won't see the synthesized pin"
    )
    assert env["MISE_OVERRIDE_CONFIG_FILENAMES"] == override


def test_replay_does_not_export_override_when_none(pila, tmp_path,
                                                     monkeypatch):
    """When state's override_file is None (the no-synth case — repo
    has no go.mod, so no override was created), the env must NOT
    carry MISE_OVERRIDE_CONFIG_FILENAMES. mise's normal discovery walk
    is what installs the worktree's deps in this case."""
    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(pila.asyncio, "create_subprocess_exec", fake_exec)
    st = _make_state_with_recipe(
        pila, tmp_path, override_path=None,
        recipe=[{
            "kind": "install",
            "command": ["pnpm", "install"],
            "working_dir": ".",
            "timeout_s": 600,
        }],
    )
    asyncio.run(pila.replay_provision_in_worktree(tmp_path, st))

    env = captured["kwargs"]["env"]
    assert "MISE_OVERRIDE_CONFIG_FILENAMES" not in env
