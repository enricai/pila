"""Tests for `phase_reconcile()` — the orchestrator-level wrapper that
spawns the reconciler worker when planners produced mismatched
capability tags.

The live LLM call (`claude_p`) is exercised end-to-end at PR-review
time, not in unit tests (the codebase's testing convention; see
CLAUDE.md "The worker invocation path is not unit-tested"). Here we
cover:

- The **short-circuit path** — when every `requires` is already
  resolved, `phase_reconcile` returns the plan unchanged without
  spawning a worker (the most common case in practice).
- **Source-text pins** on the worker invocation shape, the die() paths,
  and the second-pass check.
- The **mutation logic** is tested in test_phase_reconcile_helpers.py
  against `_apply_reconciler_output`; here we just confirm
  `phase_reconcile` plumbs everything correctly.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


def _plan(domain: str, *subtasks: dict) -> dict:
    return {"domain": domain, "status": "ready", "subtasks": list(subtasks)}


def _minimal_state(pila, tmp_path):
    """A State with just enough plumbing for phase_reconcile to read
    st.bump_workers and not crash. No actual workers will run — the
    short-circuit path never invokes claude_p."""
    pila_root = tmp_path / ".pila"
    run_id = "test-reconcile-aaa111"
    (pila_root / "runs" / run_id).mkdir(parents=True)
    st = pila.State(pila_root, run_id)
    st.data = {"task": "test", "worker_count": 0}
    st.save()
    return st


# --- short-circuit -------------------------------------------------------

def test_short_circuit_no_unresolved_returns_plans_unchanged(pila, tmp_path):
    """The common case: planners agreed on capability vocabulary, every
    `requires` has a matching `provides`. phase_reconcile must return
    the plans list without spawning a worker."""
    req_a = {"tag": "a", "extent": "in_plan"}
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["a"]}),
        _plan("testing",
              {"id": "test-001", "title": "y", "requires": [req_a]}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    # `models` doesn't need a "reconciler" key for the short-circuit
    # path — the worker is never invoked.
    models: dict[str, str] = {}

    result = asyncio.run(pila.phase_reconcile(plans, "test task", st,
                                                  caps, models, {}))
    # Same list, unchanged.
    assert result is plans
    assert plans[1]["subtasks"][0]["requires"] == [req_a]
    # No worker spawned → worker_count unchanged.
    assert st.data.get("worker_count", 0) == 0


def test_phase_reconcile_dies_on_planner_vs_planner_id_collision(pila, tmp_path):
    """Two planners (different domains) both emit a subtask with id
    `feat-001`. Each planner's prompt tells it to prefix its ids with
    its domain, but the prompt is advisory per CLAUDE.md; if a planner
    ignores the rule and the orchestrator doesn't catch it, schedule()'s
    dict-flatten would silently overwrite — the same silent-data-loss
    failure class as the reconciler-output collisions caught
    downstream.

    The check must fire BEFORE the short-circuit return (otherwise a
    collision that doesn't manifest as an unresolved `requires` would
    slip through), and BEFORE any reconciler mutation. This test pins
    both invariants: the die() message names the colliding id AND every
    domain that emitted it.
    """
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "feature side",
               "provides": ["cap-a"]}),
        _plan("testing",
              # WRONG prefix — testing should emit `test-001`, not
              # `feat-001`. The check must catch this regardless of
              # whether `requires` happens to be resolved.
              {"id": "feat-001", "title": "testing side",
               "provides": ["cap-b"]}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    models: dict[str, str] = {}

    with pytest.raises(SystemExit) as exc:
        asyncio.run(pila.phase_reconcile(plans, "task", st, caps, models, {}))
    assert exc.value.code != 0


def test_phase_reconcile_collision_check_runs_before_short_circuit(pila, tmp_path):
    """The planner-vs-planner check must run even when every `requires`
    is already resolved — otherwise the short-circuit at
    `if not unresolved: return plans` would let a collision slip
    through. Pin by setting up plans with NO unresolved requires but
    WITH a duplicate id across domains.
    """
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["cap-a"]}),
        _plan("testing",
              # Same id as feature-implementation's subtask, different
              # domain — and `requires` is empty so there are zero
              # unresolved tags. The short-circuit would otherwise
              # return `plans` immediately without catching the
              # collision.
              {"id": "feat-001", "title": "y", "provides": []}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    with pytest.raises(SystemExit):
        asyncio.run(pila.phase_reconcile(plans, "task", st, {**caps}, {}, {}))


def test_phase_reconcile_collision_error_names_id_and_domains(pila, tmp_path, capsys):
    """The die() message must name the colliding id AND every domain
    that emitted it, so a user reading the error can trace it back to
    the specific planners that misbehaved. Distinct surface form from
    the reconciler-output collision error (which uses 'collide with
    existing subtasks' / 'duplicated within added_subtasks').
    """
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "a"}),
        _plan("testing",
              {"id": "feat-001", "title": "b"}),
        _plan("refactoring",
              {"id": "feat-001", "title": "c"}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    with pytest.raises(SystemExit):
        asyncio.run(pila.phase_reconcile(plans, "task", st, caps, {}, {}))
    err = capsys.readouterr().err
    # The colliding id and all three domains are named.
    assert "feat-001" in err
    assert "feature-implementation" in err
    assert "testing" in err
    assert "refactoring" in err
    # Distinct surface form so this error is distinguishable from the
    # reconciler-output collision errors.
    assert "planner-vs-planner" in err


def test_short_circuit_empty_plans(pila, tmp_path):
    """Defensive: empty plans list short-circuits without error."""
    plans: list = []
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    result = asyncio.run(pila.phase_reconcile(plans, "x", st, caps, {}, {}))
    assert result is plans
    assert result == []


def test_short_circuit_plan_with_no_requires(pila, tmp_path):
    """Plan with subtasks that have `provides` but no `requires` →
    nothing to reconcile."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x", "provides": ["a"]})]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    result = asyncio.run(pila.phase_reconcile(plans, "x", st, caps, {}, {}))
    assert result is plans
    assert st.data.get("worker_count", 0) == 0


# --- source-text pins on phase_reconcile's contract ----------------------

def test_phase_reconcile_uses_reconciler_schema(pila):
    """Worker is gated on SCHEMAS["reconciler"] — pin so the schema-key
    arg doesn't drift."""
    src = inspect.getsource(pila.phase_reconcile)
    assert 'schema_key="reconciler"' in src


def test_phase_reconcile_uses_inspect_tools(pila):
    """Reconciler is read-only — same tool bucket as classifier/planner.
    Pin so a refactor doesn't accidentally upgrade it to ACT_TOOLS
    (write/edit) which would let the worker modify files. INSPECT_TOOLS
    replaced READ_TOOLS to allow allowlisted read-only Bash without
    relying on --dangerously-skip-permissions (DESIGN §12)."""
    src = inspect.getsource(pila.phase_reconcile)
    assert "allowed_tools=INSPECT_TOOLS" in src


def test_phase_reconcile_uses_reconciler_model(pila):
    """The worker uses models['reconciler'] — pin so commit 4's wiring
    pre-condition (commit 3 adds 'reconciler' to WORKER_TYPES) doesn't
    silently regress."""
    src = inspect.getsource(pila.phase_reconcile)
    assert 'models["reconciler"]' in src


def test_phase_reconcile_uses_reconciler_prompt(pila):
    """The system prompt comes from prompts/reconciler.md."""
    src = inspect.getsource(pila.phase_reconcile)
    assert 'load_prompt("reconciler")' in src


def test_phase_reconcile_dies_on_unresolvable(pila):
    """When the reconciler returns a non-empty `unresolvable` array, the
    orchestrator dies with the worker's reasoning. The check runs
    BEFORE _apply_reconciler_output so a fail-closed run leaves no
    phantom mutations. Implemented via `_check_unresolvable(output)`
    which factors the check + die out of the inline retry-loop body
    (cycle-resolution retry calls it for both attempts)."""
    src = inspect.getsource(pila.phase_reconcile)
    # die() and the unresolvable check must be present.
    assert "unresolvable" in src
    # `_check_unresolvable` is the helper that does the check + die. It
    # must be called BEFORE `_apply_reconciler_output(plans, output)`.
    check_pos = src.find("_check_unresolvable(output)")
    apply_pos = src.find("_apply_reconciler_output(plans, output)")
    assert check_pos != -1, (
        "phase_reconcile must invoke _check_unresolvable on the first "
        "reconciler attempt before applying any mutations"
    )
    assert apply_pos != -1
    assert check_pos < apply_pos, (
        "unresolvable check must run BEFORE _apply_reconciler_output "
        "so a fail-closed run leaves no phantom mutations"
    )
    # The helper itself must call die() with a non-empty unresolvable.
    helper_src = inspect.getsource(pila.phase_reconcile)
    assert "die(" in helper_src


def test_phase_reconcile_second_pass_check_present(pila):
    """phase_reconcile calls `_compute_unresolved_requires` at three
    distinct sites, each gating a different state. Pin so a future
    refactor can't silently regress any of them:

    1. **Initial check** (before spawning the reconciler): if the
       merged planner output already has every `requires` satisfied,
       short-circuit — no worker call needed.
    2. **Post-attempt-1 check** (after applying reconciler output):
       if anything is still unresolved (e.g. an `added_subtask`
       itself has unresolved `requires`, or the model invented a
       new tag without renaming the original consumer's tag to
       match), trigger the unresolved-retry loop instead of dying
       immediately.
    3. **Post-retry final check** (after the retry's apply step):
       catches the edge case where attempt-2's `added_subtasks`
       introduce a NEW unresolved requires entry not in the
       original `still_unresolved` set. If non-empty, die with the
       full report — retry budget exhausted.

    The assertion uses `>= 3` rather than `== 3` so a future
    refactor can ADD another check without tripping the pin.
    """
    src = inspect.getsource(pila.phase_reconcile)
    count = src.count("_compute_unresolved_requires(plans)")
    assert count >= 3, (
        f"phase_reconcile should call _compute_unresolved_requires at "
        f"three sites (initial short-circuit, post-attempt-1 gate, "
        f"post-retry final check), found {count}"
    )


def test_phase_reconcile_precondition_passes_run_twice(pila):
    """DESIGN §5 `requires.extent`: the precondition-collection +
    collision-promotion passes must run BOTH before the reconciler call
    AND again after `_apply_reconciler_output`. Without the second run,
    `extent: external` entries on reconciler-added connector subtasks
    are silently dropped (the P2.3 finding). Pin both invocations as a
    source-text invariant so a future refactor cannot regress to the
    single-pass behavior."""
    src = inspect.getsource(pila.phase_reconcile)
    promote_count = src.count("_promote_external_collisions(plans)")
    collect_count = src.count("_collect_external_preconditions(plans)")
    assert promote_count >= 2, (
        f"phase_reconcile should call _promote_external_collisions twice "
        f"(initial pre-pass + re-run after _apply_reconciler_output), "
        f"found {promote_count}"
    )
    assert collect_count >= 2, (
        f"phase_reconcile should call _collect_external_preconditions twice "
        f"(initial pre-pass + re-run after _apply_reconciler_output), "
        f"found {collect_count}"
    )


def test_phase_reconcile_bumps_workers(pila):
    """Worker invocation must go through st.bump_workers to count
    against max_total_workers. Pin so the reconciler counts toward the
    cap (and budget).

    Note: short-circuit path doesn't bump (no worker spawned)."""
    src = inspect.getsource(pila.phase_reconcile)
    assert "st.bump_workers(caps)" in src


def test_phase_reconcile_uses_sid_reconciler(pila):
    """The worker's sid (used for logs and .pila/logs/<sid>.log) is
    'reconciler'. Pin so the log file lookup is stable."""
    src = inspect.getsource(pila.phase_reconcile)
    assert 'sid="reconciler"' in src


# --- DESIGN §5 `requires.extent` integration ---------------------------

def _req(tag: str, extent: str = "in_plan", reason: str = "") -> dict:
    entry = {"tag": tag, "extent": extent}
    if reason:
        entry["reason"] = reason
    return entry


def test_phase_reconcile_external_only_short_circuits(pila, tmp_path):
    """A plan whose only "unresolved" requires are `extent: external` must
    NOT invoke the reconciler — externals are out-of-graph by planner
    declaration. The function still completes the helper passes
    (promotion + collection), persists `external_preconditions` to
    state, and returns the plans unchanged."""
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x",
               "requires": [_req("dynamo-table", "external",
                                 "owned by api-services CDK stack")]}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    result = asyncio.run(pila.phase_reconcile(plans, "task", st, caps, {}, {}))
    assert result is plans
    # No worker spawned.
    assert st.data.get("worker_count", 0) == 0
    # External preconditions persisted for write_plan to pick up.
    pre = st.data.get("external_preconditions", [])
    assert len(pre) == 1
    assert pre[0]["tag"] == "dynamo-table"
    assert pre[0]["originating_subtasks"] == ["feat-001"]


def test_phase_reconcile_promotes_collision_before_unresolved_check(pila, tmp_path):
    """An `extent: external` entry whose tag is `provides`d by some
    plan must be promoted to in_plan *before* `_compute_unresolved_requires`
    runs. The combined effect: the entry becomes a normal graph edge,
    nothing is unresolved, and the reconciler is not invoked."""
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["redis-available"]}),
        _plan("testing",
              {"id": "test-001", "title": "y",
               "requires": [_req("redis-available", "external",
                                 "planner thought infra owned this")]}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    result = asyncio.run(pila.phase_reconcile(plans, "task", st, caps, {}, {}))
    assert result is plans
    # Promoted: the entry's extent is now in_plan, with reason preserved.
    entry = plans[1]["subtasks"][0]["requires"][0]
    assert entry["extent"] == "in_plan"
    # No worker spawned (the promoted entry has a provider).
    assert st.data.get("worker_count", 0) == 0
    # Not collected as a precondition (promoted before collection).
    assert st.data.get("external_preconditions", []) == []


def test_phase_reconcile_persists_empty_preconditions_when_none(pila, tmp_path):
    """`external_preconditions` is always persisted (even as []) so
    write_plan() has a consistent key to read from. Catches a regression
    where st.save() is skipped on the empty path."""
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["a"]}),
        _plan("testing",
              {"id": "test-001", "title": "y", "requires": [_req("a")]}),
    ]
    st = _minimal_state(pila, tmp_path)
    caps = {"max_total_workers": 40, "max_parallel": 4,
            "confidence_rounds": 8}
    asyncio.run(pila.phase_reconcile(plans, "task", st, caps, {}, {}))
    assert st.data.get("external_preconditions") == []
