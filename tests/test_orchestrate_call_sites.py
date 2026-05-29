"""Source-text coupling tests for the call sites in `_run_phases` (the
phase-sequence body extracted from `orchestrate()`) and `settle_subtask()`
that wire up the P4-P5 features.

The risk this guards against: a future refactor (or AI rewrite) of
either function removes a load-bearing call site, the unit tests for
each helper still pass (because they call the helper directly), and
the regression ships unnoticed. That's exactly the shape of the P5-1
bug — the documented `--resume --answers` flow was broken because
nobody had pinned the `gather_answers` call to the resume path.

Pattern mirrors `test_state_fields.py`'s code-vs-spec coupling.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


def _function_body(name: str) -> str:
    """Extract the source text of `def <name>(` or `async def <name>(`
    from pila.py, ending at the next top-level def/async def. Used
    to scope source-text assertions to one function so a string that
    happens to appear elsewhere in the file doesn't false-pass."""
    src = PILA_PY.read_text()
    for prefix in (f"async def {name}(", f"def {name}("):
        idx = src.find(prefix)
        if idx >= 0:
            start = idx
            break
    else:
        raise AssertionError(f"function `{name}` not found in pila.py")
    # End at the next top-level def or async def (matching the same
    # column-0 indentation). The conservative bound is the next
    # occurrence of either, whichever comes first.
    next_async = src.find("\nasync def ", start + 1)
    next_sync = src.find("\ndef ", start + 1)
    candidates = [i for i in (next_async, next_sync) if i > 0]
    end = min(candidates) if candidates else len(src)
    return src[start:end]


# ----- P5-1 regression guard ------------------------------------------------

def test_orchestrate_calls_absorb_supplied_answers_on_resume():
    """The documented user flow for a deferred non-interactive
    clarification (Phase-1 OR DESIGN §11 mid-execution) requires that
    `--answers FILE` is re-read on `--resume`. The P5-1 fix wired this
    by adding `absorb_supplied_answers(args, st, pila_dir)` inside
    the `if args.resume:` branch. Pin that call so a future refactor
    that removes it fails this test instead of silently re-breaking
    the documented flow.
    """
    body = _function_body("_run_phases")
    # The resume branch exists.
    assert "if args.resume:" in body, (
        "orchestrate() must keep its `if args.resume:` branch — "
        "the absorb call lives inside it")
    # absorb_supplied_answers is called somewhere in the function.
    assert "absorb_supplied_answers(" in body, (
        "orchestrate() must call absorb_supplied_answers() — without "
        "it, --resume --answers FILE silently drops the answers "
        "(P5-1). Add the call back to the `if args.resume:` block.")
    # And specifically, the call must be BEFORE the `else:` branch
    # that handles the initial-run path — i.e. on the resume path.
    resume_idx = body.index("if args.resume:")
    else_idx = body.index("\n    else:", resume_idx)
    absorb_idx = body.index("absorb_supplied_answers(")
    assert resume_idx < absorb_idx < else_idx, (
        "absorb_supplied_answers() must be called inside the "
        "`if args.resume:` block, not on the initial-run path "
        "(the initial path is already handled by gather_answers + "
        "write_plan, which writes specs fresh after the merge). "
        "Without the resume-path call, --resume --answers silently "
        "drops the answers (P5-1).")


# ----- P4-2 regression guard ------------------------------------------------

def test_settle_subtask_has_needs_clarification_branch():
    """settle_subtask() must route a worker's `needs-clarification`
    result through surface_clarification + re-spawn. The unit tests
    for validate_result and the schema cover the contract on the
    worker's output, but nothing pins that the orchestrator actually
    handles the new status. Without this guard, a refactor could
    remove the branch and validate_result would still reject malformed
    results (a green test suite) while the orchestrator's switch fell
    through to the `return res` default path — surfacing the worker's
    needs-clarification dict to the wave aggregator, which would not
    treat it as terminal-blocking, producing wedged-wave behavior.
    """
    body = _function_body("settle_subtask")
    # The status branch exists.
    assert 'status == "needs-clarification"' in body, (
        "settle_subtask() must have an `if status == "
        '"needs-clarification":` branch to route the new exit '
        "through surface_clarification + re-spawn. Without it, the "
        "worker's needs-clarification result falls through to the "
        "`return res` default and the wave aggregator treats it as "
        "an unexpected status — see DESIGN §11.")
    # The branch must call surface_clarification — that's how the
    # question reaches the user.
    branch_start = body.index('status == "needs-clarification"')
    # Bound the branch by the next `if status ==` or `return` at
    # function-tail indentation, whichever comes first.
    rest = body[branch_start:]
    next_branch = rest.find('if status ==', 1)
    branch_end = next_branch if next_branch > 0 else len(rest)
    branch = rest[:branch_end]
    assert "surface_clarification(" in branch, (
        "the needs-clarification branch must call "
        "surface_clarification() — that's how the question is "
        "routed to the user (interactively or via "
        "pending-clarifications.json + EXIT_NEEDS_ANSWERS).")


def test_orchestrate_calls_phase_reconcile_between_plan_and_schedule():
    """The reconciler (DESIGN §5 / §17) bridges cross-domain
    capability-tag mismatches before the scheduler builds its DAG.
    Order matters: `phase_reconcile` must run on the output of
    `phase_plan` and its result must feed `schedule`. If a future
    refactor swapped the order (schedule → reconcile) the scheduler
    would build a wave graph over un-reconciled tags and either:
      - fail with "nothing provides X" (the pre-reconciler behavior), or
      - silently scatter the dependency that the reconciler should
        have wired together.
    Pin the source order so a regression fails here instead of in a
    real run with mismatched planner vocabulary.
    """
    body = _function_body("_run_phases")
    plan_idx = body.find("await phase_plan(")
    reconcile_idx = body.find("await phase_reconcile(")
    schedule_idx = body.find("schedule(plans)")
    assert plan_idx >= 0, "orchestrate() must call phase_plan()"
    assert reconcile_idx >= 0, (
        "orchestrate() must call phase_reconcile() between phase_plan "
        "and schedule. Without it, cross-domain capability-tag "
        "mismatches that the planners produce slip through to "
        "validate_plan and abort the run (DESIGN §5, §17).")
    assert schedule_idx >= 0, "orchestrate() must call schedule()"
    assert plan_idx < reconcile_idx < schedule_idx, (
        "ordering must be: phase_plan → phase_reconcile → schedule. "
        f"got phase_plan@{plan_idx}, "
        f"phase_reconcile@{reconcile_idx}, schedule@{schedule_idx}. "
        "The scheduler's DAG must be built over the reconciled plan, "
        "not the raw planner output.")


def test_orchestrate_reconcile_feeds_schedule_via_plans_var():
    """The plumbing: phase_reconcile must return into the same `plans`
    variable that schedule reads. Otherwise the reconciler runs but its
    output is discarded — a silent regression where the schedule still
    sees un-reconciled tags. Pin the rebind so a future change can't
    accidentally split the variables.
    """
    body = _function_body("_run_phases")
    # The exact call shape we want — assignment back to `plans`.
    assert "plans = await phase_reconcile(plans," in body, (
        "phase_reconcile must be called as `plans = await "
        "phase_reconcile(plans, ...)` so its returned (possibly "
        "mutated) plan list feeds schedule(). A call that throws "
        "away the return value would leave schedule operating on the "
        "raw planner output.")


def test_settle_subtask_needs_clarification_uses_unified_cap():
    """The unified subtask_continuations cap is the design's defense
    against the worker drifting toward asking instead of researching
    (DESIGN §11): clarifications consume from the same per-subtask
    re-spawn budget as context-exhaustion handoffs. If a future
    change reintroduced a separate clarification cap, this test
    fails — preserving the "no extra ask-the-user allowance"
    invariant.
    """
    body = _function_body("settle_subtask")
    # The needs-clarification branch must reference the unified cap.
    branch_start = body.index('status == "needs-clarification"')
    next_branch = body.find('if status ==', branch_start + 1)
    branch_end = next_branch if next_branch > 0 else len(body)
    branch = body[branch_start:branch_end]
    assert 'caps["subtask_continuations"]' in branch, (
        "the needs-clarification branch must consume from "
        'caps["subtask_continuations"], the unified per-subtask '
        "re-spawn budget shared with incomplete-handoff. Per DESIGN "
        "§11, a separate clarification cap would invite the worker "
        "to ask instead of research.")
    # And the OLD cap name must not have crept back in.
    assert 'caps["handoff_continuations"]' not in branch, (
        "the needs-clarification branch must NOT reference "
        'caps["handoff_continuations"] — that cap was renamed to '
        "subtask_continuations in the Pass-4 work. A regression "
        "that brought the old name back would also re-introduce the "
        "two-separate-caps confusion DESIGN §11 explicitly rejects.")
