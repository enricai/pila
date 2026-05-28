"""Tests for `discover_runs()` — enumerates `.pila/runs/*/state.json`
for `--list` and `--resume` discovery.

Covers: empty repo, single run, multiple runs (sorted), bootstrap dir
skipped, malformed state.json skipped with warning, non-dict state.json
skipped.

Uses a `tmp_path` fixture for filesystem isolation — no mocking; this is
a pure-I/O function reading real files."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_run(pila_root: Path, run_id: str, state: dict) -> Path:
    run_dir = pila_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps(state))
    return run_dir


def test_discover_runs_empty_dir(pila, tmp_path):
    """No `.pila/runs/` directory → empty list, no error."""
    assert pila.discover_runs(tmp_path) == []


def test_discover_runs_empty_runs_dir(pila, tmp_path):
    """`.pila/runs/` exists but has no children → empty list."""
    (tmp_path / "runs").mkdir()
    assert pila.discover_runs(tmp_path) == []


def test_discover_runs_single_run(pila, tmp_path):
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "do thing", "started_at": "2026-05-26T10:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "feat-foo-abc123"
    assert runs[0]["task"] == "do thing"
    assert "path" in runs[0]


def test_discover_runs_multiple_runs_sorted_newest_first(pila, tmp_path):
    """Newest by `started_at` sorts first, so `--list` shows most-recent at top."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"task": "a", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "feat-b-bbbbbb",
              {"task": "b", "started_at": "2026-05-26T12:00:00+00:00"})
    _make_run(tmp_path, "feat-c-cccccc",
              {"task": "c", "started_at": "2026-05-26T11:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert [r["run_id"] for r in runs] == [
        "feat-b-bbbbbb", "feat-c-cccccc", "feat-a-aaaaaa"
    ]


def test_discover_runs_skips_bootstrap_dirs(pila, tmp_path):
    """`_bootstrap-<hex>/` directories are pre-classify scratch space;
    they should never appear in discovery results."""
    _make_run(tmp_path, "_bootstrap-abc123",
              {"task": "in-progress", "started_at": "2026-05-26T13:00:00+00:00"})
    _make_run(tmp_path, "feat-foo-def456",
              {"task": "real", "started_at": "2026-05-26T10:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "feat-foo-def456"


def test_discover_runs_skips_non_dirs(pila, tmp_path):
    """A regular file in `runs/` is not a run; ignore it silently."""
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "stray-file").write_text("garbage")
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 1


def test_discover_runs_skips_dirs_without_state_json(pila, tmp_path):
    """A run directory missing `state.json` (mid-bootstrap, corrupted) is
    skipped — not surfaced as an error and not treated as a run."""
    (tmp_path / "runs" / "feat-broken-xyz789").mkdir(parents=True)
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "feat-foo-abc123"


def test_discover_runs_skips_malformed_json(pila, tmp_path, capsys):
    """A state.json with invalid JSON triggers a warning log but doesn't
    raise — `--list` should still work in the presence of corrupted runs."""
    run_dir = tmp_path / "runs" / "feat-bad-xyz999"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{not valid json")
    _make_run(tmp_path, "feat-foo-abc123",
              {"task": "x", "started_at": "2026-05-26T10:00:00+00:00"})
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "feat-foo-abc123"


def test_discover_runs_skips_non_object_state(pila, tmp_path):
    """state.json that contains valid JSON but not an object (array,
    string) is still useless to pila; skip it."""
    run_dir = tmp_path / "runs" / "feat-array-xyz000"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text('["this is", "an array"]')
    runs = pila.discover_runs(tmp_path)
    assert runs == []


def test_discover_runs_handles_missing_started_at(pila, tmp_path):
    """A run without `started_at` sorts last (treated as the empty string).
    Doesn't crash the sort."""
    _make_run(tmp_path, "feat-newer-aaa111",
              {"task": "a", "started_at": "2026-05-26T10:00:00+00:00"})
    _make_run(tmp_path, "feat-undated-bbb222",
              {"task": "b"})  # no started_at
    runs = pila.discover_runs(tmp_path)
    assert len(runs) == 2
    # The one with started_at sorts first; undated sorts last.
    assert runs[0]["run_id"] == "feat-newer-aaa111"
    assert runs[1]["run_id"] == "feat-undated-bbb222"


def test_discover_runs_preserves_state_fields(pila, tmp_path):
    """Discovered summary includes the full state.json contents, plus
    `run_id` and `path` overlay fields — callers (--list) need access to
    `categories`, `worker_count`, etc."""
    _make_run(tmp_path, "feat-foo-abc123", {
        "task": "x",
        "started_at": "2026-05-26T10:00:00+00:00",
        "finished_at": "2026-05-26T11:00:00+00:00",
        "categories": ["feature-implementation"],
        "worker_count": 17,
    })
    runs = pila.discover_runs(tmp_path)
    assert runs[0]["finished_at"] == "2026-05-26T11:00:00+00:00"
    assert runs[0]["categories"] == ["feature-implementation"]
    assert runs[0]["worker_count"] == 17
