"""Tests for the orchestrator memory sampler.

`_memory_sampler` is a background coroutine that writes one ndjson line
per ~30s to `.pila/runs/<run-id>/memory.ndjson` while orchestrate() is
alive. Each line records RSS, current phase, worker count, open FDs,
and thread count — the four axes we need to tell "natural heavy run"
from "real orchestrator leak."

The contract these tests pin:

- `_collect_memory_sample` returns the documented keys with sensible
  types (positive RSS, current phase from st.data, worker_count
  defaulted, open_fds/thread_count never None).
- `_memory_sampler` writes valid ndjson at the configured interval to
  `memory.ndjson` under the run dir.
- Cancellation writes a final sample line and then propagates the
  CancelledError so the awaiting caller can clean up.
- A failure inside `_collect_memory_sample` does not kill the sampler
  or the orchestrator — telemetry that crashes its host is worse than
  no telemetry.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_st(tmp_path: Path, phase: str = "phase 2: planning",
             worker_count: int = 3) -> SimpleNamespace:
    """A minimal State stand-in. `_memory_sampler` only reads `run_dir`
    and the `current_phase` / `worker_count` keys on `data`; a
    SimpleNamespace mirrors that surface without dragging in the full
    State machinery (atomic save, lock, etc.)."""
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    return SimpleNamespace(
        run_dir=run_dir,
        data={"current_phase": phase, "worker_count": worker_count},
    )


def test_collect_returns_expected_keys(pila, tmp_path):
    st = _make_st(tmp_path)
    sample = pila._collect_memory_sample(st)
    assert set(sample.keys()) == {
        "ts", "rss_kb", "phase", "worker_count", "open_fds", "thread_count",
    }
    assert sample["phase"] == "phase 2: planning"
    assert sample["worker_count"] == 3
    # RSS should be positive on any real OS where the test runs (the
    # value's KB-vs-bytes interpretation differs by platform, but
    # ru_maxrss is always > 0 for a live process).
    assert sample["rss_kb"] > 0
    # open_fds may be -1 on platforms without /proc (e.g. macOS host),
    # but thread_count is always >= 1 via threading.active_count().
    assert sample["thread_count"] >= 1


def test_collect_defaults_when_state_keys_missing(pila, tmp_path):
    """If `current_phase` / `worker_count` are absent the collector
    falls back to documented sentinels rather than KeyError-ing."""
    run_dir = tmp_path / "runs" / "bare"
    run_dir.mkdir(parents=True)
    st = SimpleNamespace(run_dir=run_dir, data={})
    sample = pila._collect_memory_sample(st)
    assert sample["phase"] == "<unknown>"
    assert sample["worker_count"] == 0


def test_sampler_writes_ndjson_lines(pila, tmp_path):
    """Drive the sampler for two ticks at 50ms each. Expect at least
    two valid-JSON lines in memory.ndjson by the time we cancel."""
    st = _make_st(tmp_path)

    async def run() -> int:
        task = asyncio.create_task(pila._memory_sampler(st, interval_sec=0.05))
        await asyncio.sleep(0.18)  # ≥ 2 ticks (immediate + one sleep)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        out = st.run_dir / "memory.ndjson"
        lines = out.read_text().splitlines()
        for line in lines:
            obj = json.loads(line)
            assert "rss_kb" in obj and "phase" in obj
        return len(lines)

    n = asyncio.run(run())
    assert n >= 2


def test_cancel_writes_final_sample(pila, tmp_path):
    """Cancelling the sampler triggers one final sample write before the
    CancelledError propagates, so we capture state at orchestrator
    exit."""
    st = _make_st(tmp_path, phase="phase 6: finalize", worker_count=11)

    async def run() -> list[dict]:
        # interval long enough that only the initial sample fires before
        # cancellation; the cancel-path sample is the second line.
        task = asyncio.create_task(pila._memory_sampler(st, interval_sec=5.0))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return [json.loads(l) for l
                in (st.run_dir / "memory.ndjson").read_text().splitlines()]

    samples = asyncio.run(run())
    assert len(samples) >= 2
    # Phase recorded on the final-cancel sample is the value at cancel
    # time (we never mutated st.data, so both lines see phase 6).
    assert samples[-1]["phase"] == "phase 6: finalize"
    assert samples[-1]["worker_count"] == 11


def test_collector_exception_does_not_kill_sampler(pila, tmp_path,
                                                   monkeypatch):
    """If `_collect_memory_sample` raises, the sampler swallows it and
    keeps looping. The orchestrator must not be taken down by a
    telemetry-side bug. We monkeypatch the collector to raise on every
    call; the sampler should still cancel cleanly when asked."""
    st = _make_st(tmp_path)

    def boom(_st):
        raise RuntimeError("synthetic collector failure")

    monkeypatch.setattr(pila, "_collect_memory_sample", boom)

    async def run() -> None:
        task = asyncio.create_task(pila._memory_sampler(st, interval_sec=0.02))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The bar is "this does not raise" — the sampler must absorb the
    # collector failure each iteration.
    asyncio.run(run())
    # And memory.ndjson should not have been created (every collect
    # attempt failed before the write).
    assert not (st.run_dir / "memory.ndjson").exists()
