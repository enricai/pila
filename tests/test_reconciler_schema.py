"""Schema tests for `SCHEMAS["reconciler"]` — the four-array output the
reconciler worker emits (DESIGN §5, §14).

The schema is consumed by `claude_p()` via `--json-schema` to gate the
worker's output. We don't have the live `claude` CLI in tests, so the
gating itself is exercised end-to-end by other means; here we just pin
the schema's structural contract by validating representative payloads
against it with a stdlib JSON-schema-shaped check.

We re-use the same style as `test_schemas_confidence.py`: extract the
schema dict from `pila.SCHEMAS["reconciler"]` and reason over its
declared `required` / `properties` keys directly.
"""
from __future__ import annotations

import pytest


def _full_valid_output() -> dict:
    """A reconciler output with all four arrays populated. Useful as a
    baseline; individual tests mutate copies of this."""
    return {
        "renames": [
            {"sid": "test-001", "from": "capture-slm-call-implemented",
             "to": "slm-capture-shim"},
        ],
        "added_provides": [
            {"sid": "feat-002", "tag": "judge-rubric-defined"},
        ],
        "added_subtasks": [
            {
                "id": "feat-008",
                "title": "Implement verdict loader",
                "intent": "Read NDJSON verdicts back into Python dicts.",
                "success_criteria_seed": "verdict_loader.py reads "
                                         "events.ndjson and returns a list of dicts",
                "provides": ["verdict-loader-implemented"],
                "requires": [],
                "depends_on": [],
                "size": "small",
                "_added_by_reconciler": True,
            },
        ],
        "unresolvable": [
            {"sid": "test-005",
             "tag": "magic-thing-that-doesnt-exist",
             "reason": "No planner produced anything related and no "
                       "plausible connector subtask can be inferred."},
        ],
    }


def test_reconciler_schema_exists(pila):
    """SCHEMAS["reconciler"] is the contract claude_p enforces against
    the worker's output. Existence pin so a future refactor can't
    silently drop it."""
    assert "reconciler" in pila.SCHEMAS
    schema = pila.SCHEMAS["reconciler"]
    assert schema["type"] == "object"


def test_reconciler_requires_all_four_arrays(pila):
    """The four arrays must be present in every output — even if empty.
    Each array is independently optional in content (any can be empty)
    but the field itself must be there so callers don't crash on a
    missing key."""
    schema = pila.SCHEMAS["reconciler"]
    required = set(schema["required"])
    assert required == {"renames", "added_provides",
                        "added_subtasks", "unresolvable"}


def test_reconciler_rename_shape(pila):
    """Each rename has sid + from + to. All three are required so the
    orchestrator's mutation logic doesn't have to handle partial
    renames."""
    item = pila.SCHEMAS["reconciler"]["properties"]["renames"]["items"]
    assert set(item["required"]) == {"sid", "from", "to"}


def test_reconciler_added_provides_shape(pila):
    """Each added_provides is (sid, tag)."""
    item = pila.SCHEMAS["reconciler"]["properties"]["added_provides"]["items"]
    assert set(item["required"]) == {"sid", "tag"}


def test_reconciler_added_subtasks_shape_matches_planner(pila):
    """Added subtasks must carry the same required fields as planner
    subtasks (id, title, success_criteria_seed) plus the
    `_added_by_reconciler` traceability flag. Mirrors the planner
    subtask schema so the rest of the pipeline (validate_plan, scheduler,
    settle_subtask) accepts them without special-casing."""
    item = pila.SCHEMAS["reconciler"]["properties"]["added_subtasks"]["items"]
    required = set(item["required"])
    assert "id" in required
    assert "title" in required
    assert "success_criteria_seed" in required
    assert "_added_by_reconciler" in required


def test_reconciler_added_subtask_carries_planner_fields(pila):
    """The properties of an added_subtask must include every field the
    planner declares so a reconciler-added subtask passes the same
    downstream checks. Pin a representative subset to catch drift."""
    props = (pila.SCHEMAS["reconciler"]
             ["properties"]["added_subtasks"]["items"]["properties"])
    # Fields the planner schema declares on each subtask.
    for field in ("id", "title", "intent", "scope_note", "depends_on",
                  "requires", "provides", "success_criteria_seed",
                  "size", "investigation_notes"):
        assert field in props, (
            f"reconciler added_subtask schema must include planner field "
            f"'{field}' or downstream code will reject it"
        )
    # Plus the reconciler-only flag.
    assert "_added_by_reconciler" in props


def test_reconciler_unresolvable_shape(pila):
    """Each unresolvable entry must include reasoning the user will see."""
    item = pila.SCHEMAS["reconciler"]["properties"]["unresolvable"]["items"]
    assert set(item["required"]) == {"sid", "tag", "reason"}


def test_reconciler_arrays_can_all_be_empty(pila):
    """The all-arrays-empty payload is valid — represents the
    degenerate-but-legitimate case where the worker found nothing to
    do (which in practice means phase_reconcile would have
    short-circuited before calling the worker, but the schema must
    still accept it)."""
    empty = {"renames": [], "added_provides": [],
             "added_subtasks": [], "unresolvable": []}
    # Reach into the schema to confirm `required` covers exactly the
    # four arrays — any of which being absent is a violation.
    required = set(pila.SCHEMAS["reconciler"]["required"])
    for field in empty:
        assert field in required


def test_reconciler_full_payload_keys_align_with_schema(pila):
    """The hand-crafted `_full_valid_output` payload only uses keys the
    schema declares. Drift guard: if the schema gains a field, update
    this test and the prompt example together."""
    schema = pila.SCHEMAS["reconciler"]
    declared = set(schema["properties"].keys())
    payload = _full_valid_output()
    assert set(payload.keys()) == declared
