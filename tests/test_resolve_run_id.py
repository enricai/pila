"""Tests for `resolve_run_id()` — pick the run_id to operate on for
`--resume` / `--list`. Policy is fails-closed: ambiguity is a hard error,
never a heuristic guess.

Cases:
- Zero runs → die.
- Exactly one run → auto-pick (common case for single-run users).
- Multiple runs, no --run-id → die with the available list.
- Multiple runs, --run-id matches → use it.
- Multiple runs, --run-id doesn't match → die with the available list.

`resolve_run_id` exits via `die()` (which calls `sys.exit(1)`) on failures,
so we catch `SystemExit`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_run(pila_root: Path, run_id: str, state: dict) -> None:
    run_dir = pila_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps(state))


def test_resolve_zero_runs_dies(pila, tmp_path):
    """Empty `.pila/runs/` is a hard error — there's nothing to resume."""
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, None)


def test_resolve_single_run_auto_picks(pila, tmp_path):
    """One run + no --run-id → use it. Preserves the existing
    single-run experience."""
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    assert pila.resolve_run_id(tmp_path, None) == "feat-foo-abc123"


def test_resolve_single_run_explicit_match(pila, tmp_path):
    """One run + matching --run-id → use it."""
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    assert pila.resolve_run_id(tmp_path, "feat-foo-abc123") == "feat-foo-abc123"


def test_resolve_single_run_wrong_explicit_dies(pila, tmp_path):
    """Even with only one run, a wrong --run-id dies — `--run-id` requires
    an exact match, never a fuzzy auto-pick."""
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, "feat-bar-xyz999")


def test_resolve_multiple_runs_no_run_id_dies(pila, tmp_path):
    """Ambiguity is never resolved by heuristic. User must pass --run-id."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"task": "a", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "feat-b-bbbbbb",
              {"task": "b", "started_at": "2026-05-26T11:00:00+00:00"})
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, None)


def test_resolve_multiple_runs_explicit_match(pila, tmp_path):
    """With --run-id and multiple runs, the exact match wins."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"task": "a", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "feat-b-bbbbbb",
              {"task": "b", "started_at": "2026-05-26T11:00:00+00:00"})
    assert pila.resolve_run_id(tmp_path, "feat-b-bbbbbb") == "feat-b-bbbbbb"


def test_resolve_multiple_runs_wrong_explicit_dies(pila, tmp_path):
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"task": "a", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "feat-b-bbbbbb",
              {"task": "b", "started_at": "2026-05-26T11:00:00+00:00"})
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, "feat-nope-zzz999")


def test_resolve_ignores_bootstrap_runs(pila, tmp_path):
    """A bootstrap directory (pre-classify) is not a real run — resolution
    treats it as not existing. If only a bootstrap dir exists, resolve dies."""
    _make_run(tmp_path, "_bootstrap-abcdef",
              {"task": "bootstrap", "started_at": "2026-05-26T10:00:00+00:00"})
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, None)


def test_resolve_message_includes_available_runs(pila, tmp_path, capsys):
    """When resolution fails, the error should list available run ids so
    the user can copy-paste one — not just 'try again'."""
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "fix-bar-def456",
              {"task": "y", "started_at": "2026-05-26T11:00:00+00:00"})
    with pytest.raises(SystemExit):
        pila.resolve_run_id(tmp_path, None)
    err = capsys.readouterr().err
    assert "feat-foo-abc123" in err
    assert "fix-bar-def456" in err
