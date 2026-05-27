"""Tests for resolve_prompt(call_type) -> (source_kind, content, location_hint).

Covers:
- Every WORKER_TYPES member returns a valid (kind, content, hint) triple.
- Parity/coupling: WORKER_TYPES and resolve_prompt have consistent coverage.
- validator returns the constant path, not a file path.
- Unknown call_type raises ValueError.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("call_type", [
    "classifier", "planner", "reconciler", "implementer", "integrator", "validator",
])
def test_resolve_prompt_returns_valid_triple(centella, call_type):
    kind, content, hint = centella.resolve_prompt(call_type)
    assert kind in ("file", "constant"), f"unexpected kind {kind!r} for {call_type}"
    assert content and content.strip(), f"empty content for {call_type}"
    assert hint and hint.strip(), f"empty hint for {call_type}"


def test_resolve_prompt_covers_all_worker_types(centella):
    """Parity: every WORKER_TYPES member must be handled without error."""
    for call_type in centella.WORKER_TYPES:
        kind, content, hint = centella.resolve_prompt(call_type)
        assert kind in ("file", "constant")
        assert content


def test_resolve_prompt_validator_is_constant(centella):
    kind, content, hint = centella.resolve_prompt("validator")
    assert kind == "constant"
    assert content == centella.VALIDATOR_SYSTEM
    assert hint == "orchestrator/centella.py:VALIDATOR_SYSTEM"


@pytest.mark.parametrize("call_type", [
    "classifier", "planner", "reconciler", "implementer", "integrator",
])
def test_resolve_prompt_file_workers_return_file_kind(centella, call_type):
    kind, content, hint = centella.resolve_prompt(call_type)
    assert kind == "file"
    assert hint == f"prompts/{call_type}.md"


def test_resolve_prompt_unknown_raises(centella):
    with pytest.raises((ValueError, KeyError)):
        centella.resolve_prompt("nonexistent_worker")
