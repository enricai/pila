"""Tests for `wrap_with_mise_exec` — the one-line argv wrapper that
prepends `mise exec --` so every install command runs with the
resolved per-repo toolchain active.

Idempotency matters: the wrapper is called from both the orchestrator
side (phase_provision) and the per-worktree replay path, and a future
refactor that double-wraps a command would silently work but produce
worse logs and slower exec — we'd rather catch it here.
"""
from __future__ import annotations

import pytest


def test_prepends_mise_exec(pila):
    assert pila.wrap_with_mise_exec(["pnpm", "install"]) == [
        "mise", "exec", "--", "pnpm", "install",
    ]


def test_preserves_extra_argv(pila):
    assert pila.wrap_with_mise_exec(
        ["pnpm", "install", "--frozen-lockfile"]) == [
        "mise", "exec", "--", "pnpm", "install", "--frozen-lockfile",
    ]


def test_idempotent_on_already_wrapped(pila):
    """Double-wrap is a bug magnet (nested `mise exec` activations layer
    PATH entries) — the helper recognizes its own prefix and returns
    the input unchanged."""
    already = ["mise", "exec", "--", "pnpm", "install"]
    assert pila.wrap_with_mise_exec(already) == already


def test_does_not_mutate_input(pila):
    """The helper returns a new list; the caller's list is unchanged."""
    cmd = ["pnpm", "install"]
    pila.wrap_with_mise_exec(cmd)
    assert cmd == ["pnpm", "install"]


@pytest.mark.parametrize("cmd", [
    ["go", "mod", "download"],
    ["cargo", "fetch"],
    ["uv", "sync"],
    ["bundle", "install"],
    ["mvn", "-B", "dependency:go-offline"],
])
def test_wraps_every_allowlisted_manager(pila, cmd):
    out = pila.wrap_with_mise_exec(cmd)
    assert out[:3] == ["mise", "exec", "--"]
    assert out[3:] == cmd
