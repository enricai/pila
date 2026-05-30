"""Tests for the pr_writer worker output schema.

The schema lives in SCHEMAS["pr_writer"] and is passed to `claude -p`
via --json-schema; the CLI validates worker output against it. These
tests document what the schema requires and guard the contract from
silent drift (e.g., title-length cap, required fields).
"""
from __future__ import annotations

import json
import pytest

try:
    import jsonschema  # type: ignore
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


def _validate(pila, instance: dict) -> None:
    """Validate using jsonschema when available; otherwise fall back to
    structural assertions that mirror what the schema declares. Tests
    must pass in both modes so CI without jsonschema installed still
    catches regressions."""
    schema = pila.SCHEMAS["pr_writer"]
    if HAS_JSONSCHEMA:
        jsonschema.validate(instance, schema)
        return
    # Manual structural check matching the schema's `required` and the
    # title's length cap.
    for k in schema["required"]:
        assert k in instance, f"missing required field {k}"
    if "title" in instance:
        assert isinstance(instance["title"], str)
        assert 1 <= len(instance["title"]) <= 200, "title length out of range"
    if "body" in instance:
        assert isinstance(instance["body"], str)
        assert len(instance["body"]) >= 1
    if "used_template" in instance:
        assert instance["used_template"] is None or isinstance(
            instance["used_template"], str)


def test_pr_writer_schema_required_fields(pila):
    schema = pila.SCHEMAS["pr_writer"]
    assert set(schema["required"]) == {"title", "body", "used_template"}


def test_pr_writer_schema_accepts_valid_no_template(pila):
    _validate(pila, {
        "title": "Fix Dynamo pagination returning duplicate items",
        "body": "## Summary\n\nFixes a bug.\n",
        "used_template": None,
    })


def test_pr_writer_schema_accepts_valid_with_template(pila):
    _validate(pila, {
        "title": "Migrate Reddit ingest from polling to SNS",
        "body": "## Description\n\n<filled template content>\n",
        "used_template": ".github/pull_request_template.md",
    })


def test_pr_writer_schema_rejects_empty_title(pila):
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not available; minLength check requires it")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"title": "", "body": "x", "used_template": None},
            pila.SCHEMAS["pr_writer"],
        )


def test_pr_writer_schema_rejects_empty_body(pila):
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not available; minLength check requires it")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"title": "ok", "body": "", "used_template": None},
            pila.SCHEMAS["pr_writer"],
        )


def test_pr_writer_schema_rejects_too_long_title(pila):
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not available; maxLength check requires it")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"title": "x" * 201, "body": "ok", "used_template": None},
            pila.SCHEMAS["pr_writer"],
        )


def test_pr_writer_schema_rejects_missing_used_template(pila):
    if not HAS_JSONSCHEMA:
        pytest.skip("jsonschema not available; required check requires it")
    # used_template is required even when null — guards against the
    # worker silently dropping the field when no template was filled.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"title": "ok", "body": "ok"},
            pila.SCHEMAS["pr_writer"],
        )


def test_pr_writer_schema_is_json_serializable(pila):
    # The schema is passed to `claude -p` as --json-schema <inline-json>;
    # any non-serializable surface would blow up at runtime, not import.
    json.dumps(pila.SCHEMAS["pr_writer"])


def test_pr_writer_in_allowed_schema_keys(pila):
    """claude_p() rejects unknown schema_key values — the allowlist must
    contain pr_writer or the _compose_pr_via_llm call would die."""
    src = (pila.__file__ and open(pila.__file__).read()) or ""
    # The allowlist is constructed inside claude_p; presence of the
    # literal in the source is the most stable check.
    assert '"pr_writer"' in src


# --- _strip_pila_prefix --------------------------------------------------
# DESIGN §12 *prompts are advisory, code enforces*. The pr_writer prompt
# tells the worker not to emit a `pila:` prefix; the launcher
# unconditionally prepends `pila: `. Without code-side enforcement, a
# single drift produces `pila: pila: …` on every PR until the prompt is
# patched. _strip_pila_prefix is the code half of the guarantee.

def test_strip_pila_prefix_removes_lowercase(pila):
    assert pila._strip_pila_prefix("pila: Fix Dynamo paging") == "Fix Dynamo paging"


def test_strip_pila_prefix_removes_uppercase(pila):
    assert pila._strip_pila_prefix("PILA: Fix Dynamo paging") == "Fix Dynamo paging"


def test_strip_pila_prefix_removes_mixed_case(pila):
    assert pila._strip_pila_prefix("Pila: x") == "x"
    assert pila._strip_pila_prefix("pILa: y") == "y"


def test_strip_pila_prefix_handles_extra_whitespace(pila):
    assert pila._strip_pila_prefix("pila:   leading spaces") == "leading spaces"
    assert pila._strip_pila_prefix("pila:\tFix bug") == "Fix bug"
    assert pila._strip_pila_prefix("pila:no space") == "no space"


def test_strip_pila_prefix_no_op_when_absent(pila):
    assert pila._strip_pila_prefix("Fix the bug") == "Fix the bug"


def test_strip_pila_prefix_does_not_false_positive_on_pilates(pila):
    """Anchor must be `pila:` exactly — not `pila` followed by anything.
    A title like "pilates is great" must pass through unchanged."""
    assert pila._strip_pila_prefix("pilates is great") == "pilates is great"
    assert pila._strip_pila_prefix("pila is a word") == "pila is a word"
    assert pila._strip_pila_prefix("Pilatesify") == "Pilatesify"


def test_strip_pila_prefix_only_strips_leading_match(pila):
    """A mid-string `pila:` is part of the user's title (e.g. a quote)
    and must not be stripped."""
    assert pila._strip_pila_prefix("Add pila: marker to commits") == \
        "Add pila: marker to commits"


def test_strip_pila_prefix_strips_only_once(pila):
    """A worker emitting `pila: pila: x` — the exact bug this guard
    defends against — gets one prefix stripped; the second pass would
    happen on a re-invocation, not in a single call. This documents
    the intent: the launcher will still add ONE `pila: ` on top."""
    out = pila._strip_pila_prefix("pila: pila: x")
    # One strip leaves "pila: x"; the launcher's unconditional prepend
    # then produces "pila: pila: x" — still wrong, but the same wrong
    # as the original bug. Document the limit: a single strip handles
    # the realistic drift case.
    assert out == "pila: x"


def test_strip_pila_prefix_empty_string(pila):
    assert pila._strip_pila_prefix("") == ""
