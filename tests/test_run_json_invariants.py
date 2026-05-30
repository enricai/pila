"""Tests for `_validate_run_json()` — enforces the four logical
invariants on the `run.json` sidecar (IMPLEMENTATION.md §8).

Invariants:
1. `pushed_at` and `push_error` are mutually exclusive.
2. `pr_url` and `pr_error` are mutually exclusive.
3. If `pr_url` is set, `pushed_at` must be set (no PR without a push).
4. `paused_at` and `pushed_at` are mutually exclusive; if `paused_at`
   is set, `fly_machine_id` must also be set.

Valid status combinations (pila --list derives these):
- `done-local`        — no push attempted, no PR.
- `done-pushed-no-pr` — pushed, PR not attempted (offline-pr case).
- `done-pushed-pr`    — pushed + PR opened.
- `push-failed`       — push attempted and failed.
- `pr-failed`         — push succeeded, PR creation failed.
- `paused-remote`     — remote run paused on failure; resume via --resume.
- `in-progress`       — finalize hasn't run yet (no fields set).
"""
from __future__ import annotations

import pytest


def _minimal_run_json(**overrides) -> dict:
    base = {
        "run_id": "feat-foo-abc123",
        "branch": "pila/runs/feat-foo-abc123",
        "working_branch": "main",
        "started_at": "2026-05-26T10:00:00+00:00",
        "finished_at": None,
        "task": "do thing",
        "pushed_at": None,
        "push_error": None,
        "pr_url": None,
        "pr_error": None,
    }
    base.update(overrides)
    return base


# --- accepts all valid status combinations ---------------------------------

def test_accepts_in_progress(pila):
    """No push/PR fields set — run hasn't finalized yet."""
    pila._validate_run_json(_minimal_run_json())


def test_accepts_done_local(pila):
    """--no-push: finalize succeeded, nothing pushed, no PR."""
    pila._validate_run_json(_minimal_run_json(
        finished_at="2026-05-26T11:00:00+00:00",
    ))


def test_accepts_done_pushed_no_pr(pila):
    """Push succeeded, PR not attempted (e.g., --no-pr in a future flag,
    or `gh` not configured). All three pr_* fields stay null."""
    pila._validate_run_json(_minimal_run_json(
        finished_at="2026-05-26T11:00:00+00:00",
        pushed_at="2026-05-26T11:00:05+00:00",
    ))


def test_accepts_done_pushed_pr(pila):
    """Happy path: pushed and PR opened."""
    pila._validate_run_json(_minimal_run_json(
        finished_at="2026-05-26T11:00:00+00:00",
        pushed_at="2026-05-26T11:00:05+00:00",
        pr_url="https://github.com/owner/repo/pull/123",
    ))


def test_accepts_push_failed(pila):
    """Push attempted, push failed: push_error set, pushed_at null,
    no PR."""
    pila._validate_run_json(_minimal_run_json(
        finished_at="2026-05-26T11:00:00+00:00",
        push_error="fatal: unable to access ...",
    ))


def test_accepts_pr_failed(pila):
    """Push succeeded, PR creation failed: pushed_at set, pr_url null,
    pr_error set."""
    pila._validate_run_json(_minimal_run_json(
        finished_at="2026-05-26T11:00:00+00:00",
        pushed_at="2026-05-26T11:00:05+00:00",
        pr_error="gh: authentication required",
    ))


# --- rejects invariant violations ------------------------------------------

def test_rejects_pushed_at_and_push_error_both_set(pila):
    """Logically impossible: a push either succeeded or failed."""
    with pytest.raises(ValueError, match="pushed_at and push_error"):
        pila._validate_run_json(_minimal_run_json(
            pushed_at="2026-05-26T11:00:05+00:00",
            push_error="something",
        ))


def test_rejects_pr_url_and_pr_error_both_set(pila):
    with pytest.raises(ValueError, match="pr_url and pr_error"):
        pila._validate_run_json(_minimal_run_json(
            pushed_at="2026-05-26T11:00:05+00:00",
            pr_url="https://github.com/owner/repo/pull/123",
            pr_error="something",
        ))


def test_rejects_pr_url_without_pushed_at(pila):
    """A PR cannot exist without a successful push. This is the logical
    invariant called out explicitly in IMPLEMENTATION.md §8."""
    with pytest.raises(ValueError, match="PR cannot succeed without"):
        pila._validate_run_json(_minimal_run_json(
            pr_url="https://github.com/owner/repo/pull/123",
        ))


def test_rejects_pr_url_with_push_failed(pila):
    """Same invariant: if push_error is set, pushed_at is null, so pr_url
    being set is also invalid. Failure mode: the first check fires
    because pr_url is set but pushed_at is null."""
    with pytest.raises(ValueError, match="PR cannot succeed without"):
        pila._validate_run_json(_minimal_run_json(
            push_error="x",
            pr_url="https://github.com/owner/repo/pull/123",
        ))


# --- pause-on-failure invariants -------------------------------------------

def test_accepts_paused_remote(pila):
    """Valid paused run: paused_at + fly_machine_id, no pushed_at."""
    pila._validate_run_json(_minimal_run_json(
        paused_at="2026-05-29T16:00:00+00:00",
        fly_machine_id="148e445b911389",
        pause_reason="worker-error",
    ))


def test_rejects_paused_and_pushed_both_set(pila):
    """A run cannot be both paused and finalized."""
    with pytest.raises(ValueError, match="paused_at and pushed_at"):
        pila._validate_run_json(_minimal_run_json(
            paused_at="2026-05-29T16:00:00+00:00",
            fly_machine_id="abc",
            pushed_at="2026-05-29T16:01:00+00:00",
        ))


def test_rejects_paused_without_fly_machine_id(pila):
    """Cannot pause without a recoverable pointer to the machine."""
    with pytest.raises(ValueError, match="fly_machine_id is null"):
        pila._validate_run_json(_minimal_run_json(
            paused_at="2026-05-29T16:00:00+00:00",
        ))


def test_accepts_fly_machine_id_alone(pila):
    """fly_machine_id without paused_at is fine — provision.sh writes
    fly_machine_id at provision time, well before any pause decision."""
    pila._validate_run_json(_minimal_run_json(
        fly_machine_id="148e445b911389",
    ))


# --- defensive cases -------------------------------------------------------

def test_rejects_non_dict(pila):
    """A non-object run.json (e.g., array) is a hard error — the contract
    is a JSON object."""
    with pytest.raises(ValueError, match="must be a JSON object"):
        pila._validate_run_json(["not", "a", "dict"])
    with pytest.raises(ValueError, match="must be a JSON object"):
        pila._validate_run_json("string")
    with pytest.raises(ValueError, match="must be a JSON object"):
        pila._validate_run_json(None)


def test_accepts_extra_fields(pila):
    """Forward-compat: extra fields not in the documented schema don't
    break validation. Pila can read run.json from a newer version."""
    pila._validate_run_json(_minimal_run_json(
        future_field="some value",
        another_extra=42,
    ))


def test_accepts_empty_dict(pila):
    """An empty dict has no invariants violated (everything is null/missing).
    A reader can still infer 'in-progress' or 'corrupt-sidecar' from the
    absence of fields."""
    pila._validate_run_json({})
