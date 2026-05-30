"""Tests for `_derive_run_status` — the pure-function status taxonomy
that `pila --list` renders.

Status table (in priority order):
  1. run.json invariant-invalid → `corrupt-sidecar`
  2. push_error set            → `push-failed`
  3. pr_error set              → `pr-failed`
  4. pr_url set                → `done-pushed-pr`
  5. pushed_at set             → `done-pushed-no-pr`
  6. finished_at set           → `done-local`
  7. otherwise                 → `in-progress`
"""
from __future__ import annotations


def test_status_in_progress_empty_run_json(pila):
    assert pila._derive_run_status({}, {}) == "in-progress"


def test_status_in_progress_none_run_json(pila):
    """A run with no sidecar at all (run died very early) reads as
    in-progress — that's accurate for the visible state."""
    assert pila._derive_run_status(None, {}) == "in-progress"


def test_status_done_local(pila):
    """Finalize completed but --no-push was set (or no push attempted)."""
    rj = {"finished_at": "2026-05-26T15:00:00+00:00"}
    assert pila._derive_run_status(rj, {}) == "done-local"


def test_status_done_pushed_no_pr(pila):
    """Push succeeded, PR not attempted (rare: gh missing post-push, or
    a future --no-pr flag)."""
    rj = {
        "finished_at": "2026-05-26T15:00:00+00:00",
        "pushed_at": "2026-05-26T15:00:05+00:00",
    }
    assert pila._derive_run_status(rj, {}) == "done-pushed-no-pr"


def test_status_done_pushed_pr(pila):
    """The happy path: pushed and PR opened."""
    rj = {
        "finished_at": "2026-05-26T15:00:00+00:00",
        "pushed_at": "2026-05-26T15:00:05+00:00",
        "pr_url": "https://github.com/owner/repo/pull/42",
    }
    assert pila._derive_run_status(rj, {}) == "done-pushed-pr"


def test_status_push_failed(pila):
    """push_error set: priority over everything except corrupt-sidecar."""
    rj = {
        "finished_at": "2026-05-26T15:00:00+00:00",
        "push_error": "fatal: unable to access ...",
    }
    assert pila._derive_run_status(rj, {}) == "push-failed"


def test_status_pr_failed(pila):
    """Push succeeded, PR failed. pushed_at set, pr_error set."""
    rj = {
        "finished_at": "2026-05-26T15:00:00+00:00",
        "pushed_at": "2026-05-26T15:00:05+00:00",
        "pr_error": "gh: authentication required",
    }
    assert pila._derive_run_status(rj, {}) == "pr-failed"


def test_status_corrupt_sidecar(pila):
    """An invariant-violating run.json renders as corrupt-sidecar so the
    user can spot it in --list and intervene."""
    rj = {
        "pushed_at": "2026-05-26T15:00:05+00:00",
        "push_error": "both set is a violation",
    }
    assert pila._derive_run_status(rj, {}) == "corrupt-sidecar"


def test_status_pr_url_without_pushed_at_is_corrupt(pila):
    """Logical-invariant violation: PR without push."""
    rj = {"pr_url": "https://github.com/owner/repo/pull/42"}
    assert pila._derive_run_status(rj, {}) == "corrupt-sidecar"


def test_status_paused_remote(pila):
    """A paused remote run: paused_at + fly_machine_id set, no pushed_at."""
    rj = {
        "paused_at": "2026-05-29T16:00:00+00:00",
        "fly_machine_id": "1234567890abcd",
        "pause_reason": "worker-error",
    }
    assert pila._derive_run_status(rj, {}) == "paused-remote"


def test_status_paused_without_machine_id_is_corrupt(pila):
    """Invariant: paused_at requires fly_machine_id (cannot resume otherwise)."""
    rj = {"paused_at": "2026-05-29T16:00:00+00:00"}
    assert pila._derive_run_status(rj, {}) == "corrupt-sidecar"


def test_status_paused_and_pushed_is_corrupt(pila):
    """Invariant: paused_at and pushed_at are mutually exclusive."""
    rj = {
        "paused_at": "2026-05-29T16:00:00+00:00",
        "fly_machine_id": "abc",
        "pushed_at": "2026-05-29T16:01:00+00:00",
    }
    assert pila._derive_run_status(rj, {}) == "corrupt-sidecar"


def test_status_paused_with_push_error_renders_as_push_failed(pila):
    """Precedence: push_error fires before paused_at so the rendered
    status matches the action the user needs to take (fix the push, not
    the pause)."""
    rj = {
        "paused_at": "2026-05-29T16:00:00+00:00",
        "fly_machine_id": "abc",
        "push_error": "fatal: ...",
    }
    assert pila._derive_run_status(rj, {}) == "push-failed"


def test_status_table_lists_every_value_used(pila):
    """RUN_STATUSES tuple must contain every value _derive_run_status
    can return — drift guard."""
    expected = {
        "corrupt-sidecar", "in-progress", "done-local",
        "done-pushed-no-pr", "done-pushed-pr",
        "push-failed", "pr-failed",
        "paused-remote",
    }
    assert set(pila.RUN_STATUSES) == expected


def test_push_error_priority_over_pr_url(pila):
    """If somehow both push_error and pr_url were set (impossible in
    practice), push_error wins — the cleanest signal that something is
    broken comes first."""
    rj = {
        "push_error": "fatal: ...",
        "pr_url": "https://gh.com/pr/1",
    }
    # _validate_run_json rejects this combo → corrupt-sidecar.
    assert pila._derive_run_status(rj, {}) == "corrupt-sidecar"
