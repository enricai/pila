"""Tests for `compose_pr_body()` — deterministic PR-body generation from
state.json + run_id. Used by finalize.sh (commit 4).

Critical properties:
- Deterministic: same state → same body, every time.
- Renders all required sections (Task, Classification, Run summary).
- Missing optional fields render as 'n/a' rather than the literal 'None'
  (Python's str(None) → 'None' is unhelpful in a rendered PR body).
- No KeyError or AttributeError on partially-populated state.
"""
from __future__ import annotations


def _full_state() -> dict:
    return {
        "task": "Add telemetry and self-heal skills",
        "started_at": "2026-05-26T14:31:22.847291+00:00",
        "finished_at": "2026-05-26T15:47:09.123456+00:00",
        "categories": ["feature-implementation", "testing"],
        "answers": {"source_of_truth": "both"},
        "waves": [["feat-001", "feat-002"], ["test-001"]],
        "worker_count": 17,
        "working_branch": "main",
    }


def test_compose_pr_body_deterministic(pila):
    """Same inputs → byte-identical output. Foundational property."""
    state = _full_state()
    rid = "feat-add-telemetry-and-self-heal-skills-a3f7c2"
    a = pila.compose_pr_body(state, rid)
    b = pila.compose_pr_body(state, rid)
    assert a == b


def test_compose_pr_body_contains_all_sections(pila):
    """The three top-level headings must all render so reviewers know
    what to expect."""
    body = pila.compose_pr_body(_full_state(), "feat-foo-abc123")
    assert "## Task" in body
    assert "## Classification" in body
    assert "## Run summary" in body


def test_compose_pr_body_renders_task_verbatim(pila):
    """The task description appears as-is — important for review context."""
    state = _full_state()
    body = pila.compose_pr_body(state, "feat-foo-abc123")
    assert state["task"] in body


def test_compose_pr_body_uses_first_category(pila):
    """When multiple categories were assigned, the body shows the primary
    one (consistent with how `compute_run_id` derives the abbrev)."""
    body = pila.compose_pr_body(_full_state(), "feat-foo-abc123")
    assert "feature-implementation" in body


def test_compose_pr_body_renders_run_id(pila):
    """The run_id appears in the body for traceability — a reviewer can
    grep their `.pila/runs/` for the directory."""
    rid = "feat-add-telemetry-and-self-heal-skills-a3f7c2"
    body = pila.compose_pr_body(_full_state(), rid)
    assert rid in body


def test_compose_pr_body_includes_wave_and_subtask_counts(pila):
    """`Waves: N, subtasks: M` — derived from `waves` list shape."""
    body = pila.compose_pr_body(_full_state(), "feat-foo-abc123")
    # _full_state has 2 waves, 3 subtasks total.
    assert "Waves: 2" in body
    assert "subtasks: 3" in body


def test_compose_pr_body_includes_worker_count(pila):
    body = pila.compose_pr_body(_full_state(), "feat-foo-abc123")
    assert "17" in body  # the worker_count


def test_compose_pr_body_includes_working_branch(pila):
    body = pila.compose_pr_body(_full_state(), "feat-foo-abc123")
    assert "main" in body  # the working branch


def test_compose_pr_body_includes_state_json_pointer(pila):
    """The body should point reviewers at the on-disk state.json for full
    detail beyond what the PR summary shows."""
    rid = "feat-foo-abc123"
    body = pila.compose_pr_body(_full_state(), rid)
    assert f".pila/runs/{rid}/state.json" in body


# --- missing / partial state handling --------------------------------------

def test_compose_pr_body_missing_finished_at_renders_na(pila):
    """An unfinished run (no `finished_at`) should not render 'None' in
    the PR body — 'n/a' is the convention."""
    state = _full_state()
    del state["finished_at"]
    body = pila.compose_pr_body(state, "feat-foo-abc123")
    assert "None" not in body
    assert "n/a" in body


def test_compose_pr_body_missing_categories_renders_na(pila):
    """No categories at all → primary category renders as 'n/a'."""
    state = _full_state()
    del state["categories"]
    body = pila.compose_pr_body(state, "feat-foo-abc123")
    assert "Category: n/a" in body


def test_compose_pr_body_empty_categories_renders_na(pila):
    """Empty list → 'n/a' (not 'None' or a crash)."""
    state = _full_state()
    state["categories"] = []
    body = pila.compose_pr_body(state, "feat-foo-abc123")
    assert "Category: n/a" in body


def test_compose_pr_body_missing_answers_renders_na(pila):
    """No clarification was done → source-of-truth renders as 'n/a'."""
    state = _full_state()
    del state["answers"]
    body = pila.compose_pr_body(state, "feat-foo-abc123")
    assert "Source of truth: n/a" in body


def test_compose_pr_body_empty_state(pila):
    """Defensive: an empty state still renders without raising. The body
    will be mostly 'n/a' but every section header is still present."""
    body = pila.compose_pr_body({}, "feat-foo-abc123")
    assert "## Task" in body
    assert "## Classification" in body
    assert "## Run summary" in body
    assert "None" not in body  # no literal 'None' leaked through


def test_compose_pr_body_no_literal_none(pila):
    """Sweep guard: under no realistic state shape should the literal
    string 'None' appear in the body."""
    # Various partial states
    states = [
        {},
        {"task": "x"},
        {"task": "x", "started_at": None, "finished_at": None},
        {"task": "x", "waves": []},
        {"task": "x", "answers": {}},
        {"task": "x", "categories": [None]},
    ]
    for state in states:
        body = pila.compose_pr_body(state, "feat-foo-abc123")
        assert "None" not in body, f"literal 'None' leaked for state={state}"
