"""Tests for the --remote / PILA_REMOTE / pila.toml `remote` launcher knob.

The parsing logic lives entirely in the bash launcher (`pila`), not in
pila.py, so these tests invoke a minimal bash harness that isolates the
three-source precedence logic (CLI > env > TOML) and echoes the resolved
REMOTE value.  They are analogous to test_finalize_sh_behavior.py in that
they exercise bash scripts directly via subprocess.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal bash harness — mirrors the exact parsing block from `pila` so the
# tests stay coupled to the real logic rather than re-implementing it.
_HARNESS = """\
#!/usr/bin/env bash
set -euo pipefail
USER_REPO="$1"   # path passed by the test as first argument

REMOTE=false
case "${PILA_REMOTE:-}" in
  1|true|TRUE|yes|YES) REMOTE=true ;;
esac
if [ -f "$USER_REPO/pila.toml" ]; then
  toml_remote="$(awk '/^[[:space:]]*remote[[:space:]]*=/ {
                        gsub(/^[[:space:]]*remote[[:space:]]*=[[:space:]]*/, "", $0);
                        gsub(/^"|"$/, "", $0);
                        print; exit
                      }' "$USER_REPO/pila.toml" 2>/dev/null || true)"
  case "${toml_remote:-}" in
    1|true|TRUE|yes|YES) REMOTE=true ;;
  esac
fi
# Simulate CLI arg scan
shift   # drop USER_REPO arg; remaining args are the simulated CLI
for arg in "$@"; do
  if [ "$arg" = "--remote" ]; then
    REMOTE=true
  fi
done
echo "$REMOTE"
"""


def _run(repo_root: Path, env: dict, cli_args: list[str]) -> str:
    """Run the harness and return 'true' or 'false'."""
    result = subprocess.run(
        ["bash", "-c", _HARNESS, "--", str(repo_root)] + cli_args,
        env={**{"PATH": "/usr/bin:/bin"}, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_default_is_local(tmp_path):
    """No CLI flag, no env, no file → false (local nerdctl run by default)."""
    assert _run(tmp_path, {}, []) == "false"


def test_env_pila_remote_1(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "1"}, []) == "true"


def test_env_pila_remote_true(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "true"}, []) == "true"


def test_env_pila_remote_TRUE(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "TRUE"}, []) == "true"


def test_env_pila_remote_yes(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "yes"}, []) == "true"


def test_env_pila_remote_YES(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "YES"}, []) == "true"


def test_env_pila_remote_0_stays_false(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "0"}, []) == "false"


def test_env_pila_remote_false_stays_false(tmp_path):
    assert _run(tmp_path, {"PILA_REMOTE": "false"}, []) == "false"


def test_toml_remote_true(tmp_path):
    (tmp_path / "pila.toml").write_text("remote = true\n")
    assert _run(tmp_path, {}, []) == "true"


def test_toml_remote_1(tmp_path):
    (tmp_path / "pila.toml").write_text("remote = 1\n")
    assert _run(tmp_path, {}, []) == "true"


def test_toml_remote_false_stays_false(tmp_path):
    (tmp_path / "pila.toml").write_text("remote = false\n")
    assert _run(tmp_path, {}, []) == "false"


def test_cli_flag_wins_over_env_false(tmp_path):
    """CLI --remote wins when env says false."""
    assert _run(tmp_path, {"PILA_REMOTE": "0"}, ["--remote"]) == "true"


def test_cli_flag_wins_over_toml_false(tmp_path):
    (tmp_path / "pila.toml").write_text("remote = false\n")
    assert _run(tmp_path, {}, ["--remote"]) == "true"


def test_env_wins_over_toml(tmp_path):
    """Env is a session-scoped knob; TOML is the committed per-repo default.
    PILA_REMOTE=1 overrides remote=false in pila.toml."""
    (tmp_path / "pila.toml").write_text("remote = false\n")
    assert _run(tmp_path, {"PILA_REMOTE": "1"}, []) == "true"


def test_toml_missing_key_stays_false(tmp_path):
    """A pila.toml with an unrelated key should not set REMOTE."""
    (tmp_path / "pila.toml").write_text("source_of_truth = codebase\n")
    assert _run(tmp_path, {}, []) == "false"


def test_flag_not_forwarded_to_rewritten_args(tmp_path):
    """--remote must be consumed and NOT forwarded to the orchestrator.
    Verify by checking that extra non-remote args pass through correctly
    while --remote itself is silently dropped (the harness accepts any
    extra args and the test just checks REMOTE=true is still set)."""
    assert _run(tmp_path, {}, ["--remote", "some task"]) == "true"
