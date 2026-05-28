"""Regression test for the git loose-ref-store collision bug.

The original branch scheme used `pila/<run-id>` for the run branch
and `pila/<run-id>/<sid>` for each subtask branch. This crashed
every run at the first `git worktree add` for a subtask:

  fatal: cannot lock ref 'refs/heads/pila/<run-id>/feat-001':
    'refs/heads/pila/<run-id>' exists;
    cannot create 'refs/heads/pila/<run-id>/feat-001'

Git's loose ref store represents each ref as a file inside
`refs/heads/.../`, so a ref AT a path and a ref UNDER that same path
cannot coexist. The fix is to put the run branch and subtask branches
under disjoint sub-prefixes (`pila/runs/...` vs.
`pila/subtasks/...`) so neither is an ancestor of the other.

This test pins that invariant: for every pair of run / subtask branches
produced by `compute_run_branch` and `compute_subtask_branch`, neither
is a strict path-prefix of the other (when split on `/`). A future
refactor that "simplifies" the prefixes back into a parent/child shape
will fail this test instead of mysteriously crashing on the first run.
"""
from __future__ import annotations

import pytest


# A handful of representative (run_id, sid) pairs. Includes ids that
# share prefixes with each other, look like the new prefix segments
# (`runs`, `subtasks`), and contain hyphens / digits typical of the
# real id format.
_SAMPLES = [
    ("feat-add-thing-aaaaaa", "feat-001"),
    ("feat-add-thing-aaaaaa", "feat-002"),
    ("fix-bug-bbbbbb", "fix-001"),
    ("refactor-xyz-cccccc", "refactor-001"),
    # Adversarial: the run id contains the literal "runs" / "subtasks"
    # so a naive prefix check would falsely fire.
    ("feat-runs-of-the-house-dddddd", "feat-001"),
    ("feat-subtasks-as-string-eeeeee", "feat-002"),
    # Adversarial: subtask id contains "/" - the planner shouldn't
    # produce these, but if it did the namespaces must still not collide.
    ("feat-zzz-ffffff", "feat-with-dashes-001"),
]


def _is_ref_ancestor(a: str, b: str) -> bool:
    """True iff ref `a` is a strict path-ancestor of ref `b` in git's
    loose ref store — i.e., `a` would be a directory containing `b`.
    Sibling refs and the identical ref both return False."""
    a_parts = a.split("/")
    b_parts = b.split("/")
    if len(a_parts) >= len(b_parts):
        return False
    return b_parts[: len(a_parts)] == a_parts


def test_is_ref_ancestor_helper_sanity():
    """Pin the helper's behavior so the real tests below mean what
    they claim. (The bug WAS the old shape being an ancestor.)"""
    # Old, broken shape — the bug we're regression-testing for.
    assert _is_ref_ancestor("pila/myrun", "pila/myrun/feat-001") is True
    # Sibling refs — fine.
    assert _is_ref_ancestor("pila/runs/myrun", "pila/subtasks/myrun/feat-001") is False
    # Identical refs — not "ancestor" in the strict sense.
    assert _is_ref_ancestor("pila/runs/myrun", "pila/runs/myrun") is False
    # Reverse direction.
    assert _is_ref_ancestor("pila/runs/myrun/feat-001", "pila/runs/myrun") is False


@pytest.mark.parametrize("run_id,sid", _SAMPLES)
def test_run_branch_is_not_subtask_branch_ancestor(pila, run_id, sid):
    """The canonical invariant: the run branch must never be a path-
    ancestor of any subtask branch (or vice versa). Violating this
    crashes git worktree add with `cannot lock ref`."""
    run_branch = pila.compute_run_branch(run_id)
    subtask_branch = pila.compute_subtask_branch(run_id, sid)
    assert not _is_ref_ancestor(run_branch, subtask_branch), (
        f"run branch {run_branch!r} is a git-ref ancestor of subtask "
        f"branch {subtask_branch!r} — this will fail `git worktree add` "
        f"with the loose-ref-store collision error."
    )
    assert not _is_ref_ancestor(subtask_branch, run_branch), (
        f"subtask branch {subtask_branch!r} is somehow an ancestor of "
        f"run branch {run_branch!r} — both shapes are inverted from "
        f"the design and the cleanup glob is wrong."
    )


@pytest.mark.parametrize("run_id,sid", _SAMPLES)
def test_subtask_branches_for_same_run_dont_collide(pila, run_id, sid):
    """Two subtask branches for the same run must not be ancestors of
    each other either. Otherwise the second worktree add in a wave
    would fail."""
    branch_a = pila.compute_subtask_branch(run_id, sid)
    branch_b = pila.compute_subtask_branch(run_id, sid + "-other")
    assert not _is_ref_ancestor(branch_a, branch_b)
    assert not _is_ref_ancestor(branch_b, branch_a)


def test_namespaces_are_explicitly_disjoint(pila):
    """Belt-and-braces: even before the path-ancestor check, the run-
    branch and subtask-branch shapes use distinct second segments
    (`runs` vs. `subtasks`). Pinning this here so a future refactor
    that uses, say, `pila/run/<id>` and `pila/run/<id>/<sid>`
    fails fast at this test rather than slipping past the ancestor
    check on some adversarial input."""
    run_branch = pila.compute_run_branch("any-id-aaaaaa")
    subtask_branch = pila.compute_subtask_branch("any-id-aaaaaa", "feat-001")
    assert run_branch.split("/")[:2] == ["pila", "runs"]
    assert subtask_branch.split("/")[:2] == ["pila", "subtasks"]
