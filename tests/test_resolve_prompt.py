"""Tests for resolve_prompt(call_type) -> (source_kind, content, location_hint).

Covers:
- Every WORKER_TYPES member returns a valid (kind, content, hint) triple.
- Parity/coupling: WORKER_TYPES and resolve_prompt have consistent coverage.
- Every worker resolves to a file under prompts/.
- Unknown call_type raises ValueError.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("call_type", [
    "classifier", "planner", "reconciler", "provision", "implementer",
    "integrator", "conformer",
])
def test_resolve_prompt_returns_valid_triple(pila, call_type):
    kind, content, hint = pila.resolve_prompt(call_type)
    assert kind == "file", f"unexpected kind {kind!r} for {call_type}"
    assert content and content.strip(), f"empty content for {call_type}"
    assert hint == f"prompts/{call_type}.md"


def test_resolve_prompt_covers_all_worker_types(pila):
    """Parity: every WORKER_TYPES member must be handled without error."""
    for call_type in pila.WORKER_TYPES:
        kind, content, hint = pila.resolve_prompt(call_type)
        assert kind == "file"
        assert content


def test_resolve_prompt_unknown_raises(pila):
    with pytest.raises((ValueError, KeyError)):
        pila.resolve_prompt("nonexistent_worker")
