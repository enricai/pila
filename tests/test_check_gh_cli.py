"""Tests for `_check_gh_cli(no_push)` — the preflight gate for the
push + PR finalize step (DESIGN §6 "Finalization").

Covers:
- `no_push=True` short-circuits silently (no shell-outs).
- `gh` not on PATH → die with install hint.
- `gh auth status` non-zero → die with `gh auth login` hint.
- `git remote get-url origin` non-zero → die with `git remote add` hint.
- All checks pass → returns silently.

Uses `monkeypatch` to control `shutil.which` and `subprocess.run`. This
is one of the few cases in the codebase where mocking is unavoidable
because the function shells out to real binaries and there's no pure
fallback path the way `_parse_claude_version` exists for the CLI check.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess-shaped object for subprocess.run mocks."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_no_push_short_circuits(pila, monkeypatch):
    """When --no-push is set, _check_gh_cli must return without running
    any of its subprocess checks. The user explicitly opted out."""
    called = []
    monkeypatch.setattr(pila.shutil, "which",
                        lambda _: called.append("which") or "/usr/bin/gh")
    monkeypatch.setattr(pila.subprocess, "run",
                        lambda *a, **kw: called.append("run") or _fake_run(0))
    pila._check_gh_cli(no_push=True)
    assert called == [], (
        "_check_gh_cli(no_push=True) must short-circuit without any "
        f"subprocess calls; instead saw: {called}"
    )


def test_gh_not_on_path_dies(pila, monkeypatch):
    monkeypatch.setattr(pila.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit):
        pila._check_gh_cli(no_push=False)


def test_gh_auth_failure_dies(pila, monkeypatch):
    """gh exists but `gh auth status` exits non-zero → die with the
    login hint."""
    monkeypatch.setattr(pila.shutil, "which", lambda _: "/usr/bin/gh")

    def run(cmd, **kwargs):
        if cmd[:3] == ["gh", "auth", "status"]:
            return _fake_run(1, stderr="not logged in")
        return _fake_run(0)
    monkeypatch.setattr(pila.subprocess, "run", run)
    with pytest.raises(SystemExit):
        pila._check_gh_cli(no_push=False)


def test_no_origin_remote_dies(pila, monkeypatch):
    """gh authed but no `origin` remote → die with the `git remote add`
    hint. Push has nowhere to go."""
    monkeypatch.setattr(pila.shutil, "which", lambda _: "/usr/bin/gh")

    def run(cmd, **kwargs):
        if cmd[:3] == ["gh", "auth", "status"]:
            return _fake_run(0)
        if cmd[:4] == ["git", "remote", "get-url", "origin"]:
            return _fake_run(1, stderr="fatal: No such remote 'origin'")
        return _fake_run(0)
    monkeypatch.setattr(pila.subprocess, "run", run)
    with pytest.raises(SystemExit):
        pila._check_gh_cli(no_push=False)


def test_all_checks_pass_returns_silently(pila, monkeypatch):
    """gh installed, authed, origin remote present → returns None."""
    monkeypatch.setattr(pila.shutil, "which", lambda _: "/usr/bin/gh")

    def run(cmd, **kwargs):
        return _fake_run(0, stdout="ok")
    monkeypatch.setattr(pila.subprocess, "run", run)
    # Must not raise.
    assert pila._check_gh_cli(no_push=False) is None


def test_error_messages_mention_no_push_escape_hatch(pila, monkeypatch, capsys):
    """All three failure modes should tell the user about --no-push as
    an alternative to fixing the failure — so a user who can't fix the
    failure (e.g., no GitHub access from this machine) has an
    immediate path forward."""
    monkeypatch.setattr(pila.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit):
        pila._check_gh_cli(no_push=False)
    err = capsys.readouterr().err
    assert "--no-push" in err
