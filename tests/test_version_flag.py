"""Tests for `centella --version`.

The version is read from `.claude-plugin/plugin.json`'s `version` field
(single source of truth). The flag must exit 0 and print a string of the
form `centella <semver>`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CENTELLA_PY = REPO_ROOT / "orchestrator" / "centella.py"
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"


def test_version_flag_prints_plugin_json_version():
    expected = json.loads(PLUGIN_JSON.read_text())["version"]
    result = subprocess.run(
        [sys.executable, str(CENTELLA_PY), "--version"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    # argparse prints --version output to stdout on Python 3.4+.
    assert re.fullmatch(rf"centella {re.escape(expected)}\s*", result.stdout), (
        f"unexpected --version output: {result.stdout!r}"
    )
    assert re.match(r"\d+\.\d+\.\d+", expected), (
        f"plugin.json version is not semver-shaped: {expected!r}"
    )
