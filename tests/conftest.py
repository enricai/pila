"""Shared pytest fixtures for the centella test suite.

centella.py is a single script (no package), so we load it once as a
module via importlib and expose it to every test via the `centella`
fixture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CENTELLA_PY = REPO_ROOT / "orchestrator" / "centella.py"


@pytest.fixture(scope="session")
def centella():
    """The centella module loaded from orchestrator/centella.py."""
    spec = importlib.util.spec_from_file_location("centella", CENTELLA_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
