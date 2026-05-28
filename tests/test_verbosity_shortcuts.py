"""Tests for verbosity_from_shortcuts() — the argparse -v/-vv/-q/-qq
count → verbosity-level mapping.

The shortcuts deliberately anchor to `normal`, not to the resolved
default, so `-v` always means "show me the streaming feature" and `-q`
always means "back to pila's pre-streaming terse output",
regardless of what PILA_VERBOSITY or pila.toml default to.
This avoids surprising users whose config sets a non-default level.

Saturation at the endpoints (rather than wrapping or raising) is the
modern stackable-flag convention (cargo, kubectl). A user typing
`-vvvv` gets debug, not an error.
"""
from __future__ import annotations

import pytest


# ----- neither shortcut → None (caller falls through to resolver) -----------

def test_no_shortcut_returns_none(pila):
    """When neither -v nor -q was passed, the function returns None so
    the caller can fall through to env / TOML / default."""
    assert pila.verbosity_from_shortcuts(0, 0) is None


# ----- -v stacking ----------------------------------------------------------

def test_single_v_returns_stream(pila):
    assert pila.verbosity_from_shortcuts(1, 0) == "stream"


def test_double_v_returns_debug(pila):
    assert pila.verbosity_from_shortcuts(2, 0) == "debug"


def test_triple_v_saturates_at_debug(pila):
    """Stackable verbosity flags saturate rather than wrap. A user who
    types `-vvv` (or argparse count > 2) gets debug, not an error."""
    assert pila.verbosity_from_shortcuts(3, 0) == "debug"
    assert pila.verbosity_from_shortcuts(10, 0) == "debug"


# ----- -q stacking ----------------------------------------------------------

def test_single_q_returns_normal(pila):
    """-q anchors at `normal` (the pre-streaming behavior), NOT at the
    resolved default. So a user with PILA_VERBOSITY=debug who
    passes -q goes to `normal`, not to `stream`."""
    assert pila.verbosity_from_shortcuts(0, 1) == "normal"


def test_double_q_returns_quiet(pila):
    assert pila.verbosity_from_shortcuts(0, 2) == "quiet"


def test_triple_q_saturates_at_quiet(pila):
    assert pila.verbosity_from_shortcuts(0, 3) == "quiet"


# ----- precedence between -q and -v -----------------------------------------

def test_quiet_wins_when_both_given(pila):
    """If a user accidentally passes both -v and -q on the same line,
    quiet wins. This is arbitrary but deterministic — matches the
    function's branch order. Pinning it so a future refactor doesn't
    silently flip the precedence."""
    assert pila.verbosity_from_shortcuts(1, 1) == "normal"
    assert pila.verbosity_from_shortcuts(2, 2) == "quiet"
    assert pila.verbosity_from_shortcuts(1, 2) == "quiet"


# ----- contract: never returns a value outside VERBOSITY_VALUES -------------

@pytest.mark.parametrize("v,q", [
    (0, 0), (1, 0), (2, 0), (5, 0),
    (0, 1), (0, 2), (0, 5),
    (1, 1), (2, 2), (3, 3),
])
def test_return_value_is_valid(pila, v, q):
    result = pila.verbosity_from_shortcuts(v, q)
    assert result is None or result in pila.VERBOSITY_VALUES
