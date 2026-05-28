"""Shared pytest fixtures for the pila test suite.

pila.py is a single script (no package), so we load it once as a
module via importlib and expose it to every test via the `pila`
fixture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


@pytest.fixture(scope="session")
def pila():
    """The pila module loaded from orchestrator/pila.py."""
    spec = importlib.util.spec_from_file_location("pila", PILA_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
