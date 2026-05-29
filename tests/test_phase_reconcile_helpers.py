"""Tests for the pure-Python helpers behind `phase_reconcile`:

- `_compute_unresolved_requires` — set lookup mirroring `validate_plan`'s
  cross-domain check, but emitting data instead of raising.
- `_apply_reconciler_output` — mechanical mutation of the merged plan
  according to the reconciler worker's output.

The actual LLM-driven reconciler worker is exercised separately (and
end-to-end at PR-review time); these tests pin the deterministic Python
that wraps it.
"""
from __future__ import annotations

import pytest


# --- _compute_unresolved_requires --------------------------------------

def _plan(domain: str, *subtasks: dict) -> dict:
    """Build a planner-shaped plan dict from a list of subtask dicts."""
    return {"domain": domain, "status": "ready", "subtasks": list(subtasks)}


def test_unresolved_empty_when_plan_has_no_requires(pila):
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x", "provides": ["a"]})]
    assert pila._compute_unresolved_requires(plans) == []


def test_unresolved_empty_when_every_requires_has_a_provider(pila):
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["a"]}),
        _plan("testing",
              {"id": "test-001", "title": "y", "requires": ["a"]}),
    ]
    assert pila._compute_unresolved_requires(plans) == []


def test_unresolved_lists_missing_tags(pila):
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["a"]}),
        _plan("testing",
              {"id": "test-001", "title": "y", "requires": ["a", "missing-1"]}),
        _plan("testing",
              {"id": "test-002", "title": "z", "requires": ["missing-2"]}),
    ]
    out = pila._compute_unresolved_requires(plans)
    # Order is by iteration order over plans → subtasks → requires;
    # we don't pin it tightly but the (sid, tag) pairs are stable.
    pairs = {(u["sid"], u["tag"]) for u in out}
    assert pairs == {("test-001", "missing-1"), ("test-002", "missing-2")}


def test_unresolved_handles_subtask_with_no_requires_field(pila):
    """A subtask that omits `requires` entirely (default empty) doesn't
    crash the lookup."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x"})]
    assert pila._compute_unresolved_requires(plans) == []


def test_unresolved_handles_subtask_with_no_provides_field(pila):
    """A subtask that omits `provides` entirely doesn't contribute to
    `all_provides`. The lookup still works."""
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x"}),
        _plan("testing",
              {"id": "test-001", "title": "y", "requires": ["a"]}),
    ]
    out = pila._compute_unresolved_requires(plans)
    assert out == [{"sid": "test-001", "tag": "a", "domain": "testing"}]


def test_unresolved_duplicate_requires_emits_once_per_subtask(pila):
    """A subtask declaring the same `requires` tag twice should not
    crash; the duplicate is fine (the scheduler dedup is unaffected by
    our emit ordering)."""
    plans = [
        _plan("testing",
              {"id": "test-001", "title": "y",
               "requires": ["missing-1", "missing-1"]}),
    ]
    out = pila._compute_unresolved_requires(plans)
    # Two entries — same sid + tag, both surfaced. The reconciler
    # consumes the list as a set internally; preserving duplicates here
    # is harmless and avoids hiding a planner bug.
    assert len(out) == 2


# --- _apply_reconciler_output ------------------------------------------

def test_apply_empty_output_is_noop(pila):
    """An all-empty output leaves plans unchanged."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x", "requires": ["foo"]})]
    out = {"renames": [], "added_provides": [],
           "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    # No mutations.
    assert plans[0]["subtasks"][0]["requires"] == ["foo"]
    assert len(plans) == 1


def test_apply_rename_rewrites_requires_on_named_subtask(pila):
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x", "provides": ["canonical"]}),
        _plan("testing",
              {"id": "test-001", "title": "y",
               "requires": ["old-name", "other-req"]}),
    ]
    out = {"renames": [{"sid": "test-001", "from": "old-name",
                        "to": "canonical"}],
           "added_provides": [], "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    # The `from` tag is gone; `to` replaces it; other reqs untouched.
    assert plans[1]["subtasks"][0]["requires"] == ["canonical", "other-req"]


def test_apply_rename_with_nonexistent_sid_is_silently_skipped(pila):
    """Defensive: if the reconciler emits a rename for a sid that
    doesn't exist, drop it rather than crash. (The reconciler is told
    only existing sids; this is belt-and-suspenders.)"""
    plans = [_plan("testing",
                   {"id": "test-001", "title": "y", "requires": ["foo"]})]
    out = {"renames": [{"sid": "nonexistent-001", "from": "foo",
                        "to": "bar"}],
           "added_provides": [], "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    # test-001 was not the target; its requires is unchanged.
    assert plans[0]["subtasks"][0]["requires"] == ["foo"]


def test_apply_added_provides_appends_to_subtask(pila):
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x", "provides": ["a"]})]
    out = {"renames": [],
           "added_provides": [{"sid": "feat-001", "tag": "b"}],
           "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    assert plans[0]["subtasks"][0]["provides"] == ["a", "b"]


def test_apply_added_provides_idempotent(pila):
    """If the reconciler emits an already-present tag, don't duplicate it."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x", "provides": ["a"]})]
    out = {"renames": [],
           "added_provides": [{"sid": "feat-001", "tag": "a"}],
           "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    assert plans[0]["subtasks"][0]["provides"] == ["a"]


def test_apply_added_provides_to_subtask_with_no_provides_field(pila):
    """Subtask missing `provides` entirely → field is added."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x"})]
    out = {"renames": [],
           "added_provides": [{"sid": "feat-001", "tag": "b"}],
           "added_subtasks": [], "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    assert plans[0]["subtasks"][0]["provides"] == ["b"]


def test_apply_added_subtasks_appends_reconciler_plan(pila):
    """Added subtasks land in a new pseudo-plan with domain="_reconciler".
    The scheduler flattens by id, so the domain only affects logs."""
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "x"})]
    new_subtask = {
        "id": "feat-008",
        "title": "Added connector",
        "success_criteria_seed": "criterion",
        "provides": ["new-cap"],
        "_added_by_reconciler": True,
    }
    out = {"renames": [], "added_provides": [],
           "added_subtasks": [new_subtask],
           "unresolvable": []}
    pila._apply_reconciler_output(plans, out)
    assert len(plans) == 2
    assert plans[1]["domain"] == "_reconciler"
    assert plans[1]["subtasks"] == [new_subtask]


def test_apply_dies_on_duplicate_added_subtask_id(pila):
    """If the reconciler emits an added_subtask whose `id` collides with
    an existing subtask, `_apply_reconciler_output` must die() — not
    silently append. The scheduler later merges all subtasks into a
    single dict keyed by id; a collision would silently drop one of
    them, losing its requires/provides/depends_on from the DAG. This
    is exactly the kind of mechanical guarantee CLAUDE.md says must
    live in the code, not the prompt.
    """
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "shim",
                    "provides": ["shim-cap"]})]
    out = {
        "renames": [], "added_provides": [],
        "added_subtasks": [{
            "id": "feat-001",  # collides with the existing subtask
            "title": "Conflicting reconciler subtask",
            "success_criteria_seed": "x",
            "provides": ["new-cap"],
            "_added_by_reconciler": True,
        }],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit) as exc:
        pila._apply_reconciler_output(plans, out)
    assert exc.value.code != 0
    # The original subtask is still there — the helper must NOT have
    # mutated plans before dying. (die() runs at the top of the
    # added_subtasks branch, before the append.)
    assert len(plans) == 1
    assert plans[0]["subtasks"][0]["id"] == "feat-001"
    assert plans[0]["subtasks"][0]["title"] == "shim"


def test_apply_dies_names_colliding_ids_in_error(pila, capsys):
    """The die() message must name the colliding id(s) so a user reading
    the error can map straight back to the offending plan. Pin the
    surface form so a future refactor can't degrade it to a generic
    'collision detected' message.
    """
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "x"},
              {"id": "feat-002", "title": "y"}),
    ]
    out = {
        "renames": [], "added_provides": [],
        "added_subtasks": [
            {"id": "feat-002", "title": "collision-1",
             "success_criteria_seed": "x", "_added_by_reconciler": True},
            {"id": "feat-009", "title": "ok",
             "success_criteria_seed": "y", "_added_by_reconciler": True},
        ],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, out)
    err = capsys.readouterr().err
    # The colliding id is named; the non-colliding one is not.
    assert "feat-002" in err
    assert "feat-009" not in err


def test_apply_dies_on_duplicate_added_subtask_self_collision(pila):
    """The reconciler emitted two added_subtasks with the same id —
    neither colliding with an existing subtask, but colliding with each
    other. schedule()'s dict-flatten would silently drop one; this
    must die() with the same fail-loud guarantee as the
    existing-vs-added case. Pin the behavior so a future refactor of
    the collision check (e.g., to a single-pass form) can't accidentally
    drop the self-collision arm.
    """
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "existing"})]
    out = {
        "renames": [], "added_provides": [],
        "added_subtasks": [
            {"id": "feat-009", "title": "first",
             "success_criteria_seed": "a", "_added_by_reconciler": True},
            {"id": "feat-009", "title": "second",  # same id as the first
             "success_criteria_seed": "b", "_added_by_reconciler": True},
        ],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit) as exc:
        pila._apply_reconciler_output(plans, out)
    assert exc.value.code != 0
    # Plans unmutated — the helper dies before appending the
    # _reconciler pseudo-plan, so the existing plan is still alone.
    assert len(plans) == 1
    assert plans[0]["subtasks"][0]["id"] == "feat-001"


def test_apply_dies_names_self_colliding_ids_in_error(pila, capsys):
    """The die() message must use the 'duplicated within added_subtasks'
    surface form (not the 'collide with existing subtasks' form) so a
    user reading the error can tell self-collision apart from
    existing-collision and trace it back to the right reconciler-output
    array.
    """
    plans = [_plan("feature-implementation",
                   {"id": "feat-001", "title": "existing"})]
    out = {
        "renames": [], "added_provides": [],
        "added_subtasks": [
            {"id": "feat-009", "title": "first",
             "success_criteria_seed": "a", "_added_by_reconciler": True},
            {"id": "feat-009", "title": "second",
             "success_criteria_seed": "b", "_added_by_reconciler": True},
            {"id": "feat-010", "title": "ok",
             "success_criteria_seed": "c", "_added_by_reconciler": True},
        ],
        "unresolvable": [],
    }
    with pytest.raises(SystemExit):
        pila._apply_reconciler_output(plans, out)
    err = capsys.readouterr().err
    # Self-collision surface form named; the non-colliding id is not.
    assert "duplicated within added_subtasks" in err
    assert "feat-009" in err
    assert "feat-010" not in err
    # And the self-collision case must NOT be misreported as an
    # existing-vs-added collision (those use a different surface form).
    assert "collide with existing subtasks" not in err


def test_apply_combined_renames_provides_and_subtasks(pila):
    """Realistic case: all three mutation types applied in one call."""
    plans = [
        _plan("feature-implementation",
              {"id": "feat-001", "title": "shim", "provides": ["shim-cap"]}),
        _plan("testing",
              {"id": "test-001", "title": "test",
               "requires": ["wrong-name", "new-cap"]}),
    ]
    out = {
        "renames": [{"sid": "test-001", "from": "wrong-name",
                     "to": "shim-cap"}],
        "added_provides": [{"sid": "feat-001", "tag": "extra-cap"}],
        "added_subtasks": [{
            "id": "feat-009",
            "title": "New cap producer",
            "success_criteria_seed": "x",
            "provides": ["new-cap"],
            "_added_by_reconciler": True,
        }],
        "unresolvable": [],
    }
    pila._apply_reconciler_output(plans, out)
    # rename applied
    assert "wrong-name" not in plans[1]["subtasks"][0]["requires"]
    assert "shim-cap" in plans[1]["subtasks"][0]["requires"]
    # added_provides applied
    assert "extra-cap" in plans[0]["subtasks"][0]["provides"]
    # added_subtasks landed
    assert len(plans) == 3
    assert plans[2]["subtasks"][0]["id"] == "feat-009"


def test_apply_does_not_consume_unresolvable_array(pila):
    """`unresolvable` is the orchestrator's responsibility (die() before
    calling _apply). `_apply_reconciler_output` ignores it — pin so the
    helper doesn't accidentally swallow unresolvable as a non-failure
    mutation."""
    plans = [_plan("testing",
                   {"id": "test-001", "title": "y", "requires": ["x"]})]
    out = {"renames": [], "added_provides": [], "added_subtasks": [],
           "unresolvable": [{"sid": "test-001", "tag": "x",
                             "reason": "fake reason"}]}
    pila._apply_reconciler_output(plans, out)
    # Plans unchanged — unresolvable is not the helper's concern.
    assert plans[0]["subtasks"][0]["requires"] == ["x"]
    assert len(plans) == 1
