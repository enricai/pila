"""Tests for `list_runs()` — the `pila --list` rendering function.

Behavioral tests use `tmp_path` for filesystem isolation. The function
reads `.pila/runs/*/state.json` (via discover_runs) and overlays
`.pila/runs/*/run.json` for status, then renders a sortable table
to stdout. Tests capture stdout via the `capsys` fixture.
"""
from __future__ import annotations

import json
from pathlib import Path


def _make_run(root: Path, run_id: str, state: dict,
              run_json: dict | None = None) -> None:
    rd = root / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "state.json").write_text(json.dumps(state))
    if run_json is not None:
        (rd / "run.json").write_text(json.dumps(run_json))


def test_list_runs_empty(pila, tmp_path, capsys):
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "no runs" in out


def test_list_runs_renders_table_header(pila, tmp_path, capsys):
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    # Header columns are present.
    for col in ("run_id", "started_at", "status", "branch"):
        assert col in out


def test_list_runs_in_progress_status(pila, tmp_path, capsys):
    """A run with no run.json sidecar reads as in-progress."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "in-progress" in out
    assert "feat-a-aaaaaa" in out


def test_list_runs_done_pushed_pr_status(pila, tmp_path, capsys):
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"},
              run_json={
                  "finished_at": "2026-05-26T11:00:00+00:00",
                  "pushed_at": "2026-05-26T11:00:05+00:00",
                  "pr_url": "https://github.com/owner/repo/pull/1",
              })
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "done-pushed-pr" in out


def test_list_runs_push_failed_status(pila, tmp_path, capsys):
    _make_run(tmp_path, "fix-b-bbbbbb",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "y"},
              run_json={
                  "finished_at": "2026-05-26T11:00:00+00:00",
                  "push_error": "fatal: ...",
              })
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "push-failed" in out


def test_list_runs_sorted_newest_first(pila, tmp_path, capsys):
    """discover_runs returns newest-first; list_runs preserves that
    ordering so the table reads naturally."""
    _make_run(tmp_path, "feat-old-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"})
    _make_run(tmp_path, "feat-new-bbbbbb",
              {"started_at": "2026-05-26T12:00:00+00:00", "task": "y"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    newest_pos = out.index("feat-new-bbbbbb")
    oldest_pos = out.index("feat-old-aaaaaa")
    assert newest_pos < oldest_pos


def test_list_runs_corrupt_sidecar(pila, tmp_path, capsys):
    """A run.json that violates invariants is rendered as corrupt-sidecar."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"},
              run_json={
                  "pushed_at": "2026-05-26T11:00:05+00:00",
                  "push_error": "violation: both set",
              })
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "corrupt-sidecar" in out


def test_list_runs_malformed_run_json_treated_as_missing(pila, tmp_path, capsys):
    """An unparseable run.json doesn't crash list_runs; the run renders
    as in-progress (no sidecar info usable)."""
    rd = tmp_path / "runs" / "feat-broken-xyz999"
    rd.mkdir(parents=True)
    (rd / "state.json").write_text(json.dumps({
        "started_at": "2026-05-26T10:00:00+00:00", "task": "x",
    }))
    (rd / "run.json").write_text("{not valid json")
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "feat-broken-xyz999" in out
    assert "in-progress" in out


def test_list_runs_skips_bootstrap_dirs(pila, tmp_path, capsys):
    _make_run(tmp_path, "_bootstrap-abcdef",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"})
    _make_run(tmp_path, "feat-real-bbbbbb",
              {"started_at": "2026-05-26T11:00:00+00:00", "task": "y"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "_bootstrap-abcdef" not in out
    assert "feat-real-bbbbbb" in out


def test_list_runs_renders_branch_from_run_json(pila, tmp_path, capsys):
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"},
              run_json={"branch": "pila/runs/feat-a-aaaaaa"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "pila/runs/feat-a-aaaaaa" in out


def test_list_runs_falls_back_to_compute_run_branch(pila, tmp_path, capsys):
    """If run.json is missing or has no `branch` field, list_runs derives
    it from the run_id via compute_run_branch."""
    _make_run(tmp_path, "feat-a-aaaaaa",
              {"started_at": "2026-05-26T10:00:00+00:00", "task": "x"})
    pila.list_runs(tmp_path)
    out = capsys.readouterr().out
    assert "pila/runs/feat-a-aaaaaa" in out
