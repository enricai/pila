"""Tests for the --runtime / PILA_RUNTIME / pila.toml `runtime` launcher knob.

config-004 added a canonical RUNTIME variable that supersedes the legacy
REMOTE/--remote interface.  The parsing logic lives in the bash launcher
(`pila`), so these tests use a minimal bash harness that mirrors the exact
resolution block and echoes the resolved RUNTIME value.

The legacy --remote / PILA_REMOTE / pila.toml `remote=true` aliases are also
tested here because they map to RUNTIME=fly and the two knobs interact via
the backward-compat alias path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Bash harness that mirrors the RUNTIME resolution block from `pila`.
# Precedence (lowest → highest): default → TOML → env → CLI.
# The block order matches the fixed launcher:
#   1. default RUNTIME=local
#   2. legacy pila.toml remote=true → RUNTIME=fly
#   3. canonical pila.toml runtime=...
#   4. legacy PILA_REMOTE env alias → RUNTIME=fly
#   5. canonical PILA_RUNTIME env
#   6. CLI --remote (legacy) / --runtime=VALUE / --runtime VALUE
_HARNESS = r"""
#!/usr/bin/env bash
set -euo pipefail
USER_REPO="$1"
shift   # remaining args are simulated CLI

RUNTIME=local

if [ -f "$USER_REPO/pila.toml" ]; then
  # step 2: legacy toml
  toml_remote="$(awk '/^[[:space:]]*remote[[:space:]]*=/ {
                        gsub(/^[[:space:]]*remote[[:space:]]*=[[:space:]]*/, "", $0);
                        gsub(/^"|"$/, "", $0);
                        print; exit
                      }' "$USER_REPO/pila.toml" 2>/dev/null || true)"
  case "${toml_remote:-}" in
    1|true|TRUE|yes|YES) RUNTIME=fly ;;
  esac
  # step 3: canonical toml
  toml_runtime="$(awk '/^[[:space:]]*runtime[[:space:]]*=/ {
                         gsub(/^[[:space:]]*runtime[[:space:]]*=[[:space:]]*/, "", $0);
                         gsub(/^"|"$/, "", $0);
                         print; exit
                       }' "$USER_REPO/pila.toml" 2>/dev/null || true)"
  case "${toml_runtime:-}" in
    local|fly) RUNTIME="${toml_runtime}" ;;
    "")        : ;;
    *)
      echo "pila: pila.toml: runtime=${toml_runtime} is not one of local|fly" >&2
      exit 1
      ;;
  esac
fi

# step 4: legacy env alias
case "${PILA_REMOTE:-}" in
  1|true|TRUE|yes|YES) RUNTIME=fly ;;
esac

# step 5: canonical env
case "${PILA_RUNTIME:-}" in
  local|fly) RUNTIME="${PILA_RUNTIME}" ;;
  "")        : ;;
  *)
    echo "pila: PILA_RUNTIME=${PILA_RUNTIME} is not one of local|fly" >&2
    exit 1
    ;;
esac

# step 6+7: CLI (--runtime=VALUE form)
for arg in "$@"; do
  case "$arg" in
    --remote)          RUNTIME=fly ;;
    --runtime=local)   RUNTIME=local ;;
    --runtime=fly)     RUNTIME=fly ;;
    --runtime=*)
      echo "pila: --runtime=${arg#--runtime=} is not one of local|fly" >&2
      exit 1
      ;;
  esac
done

# step 7b: two-arg form --runtime VALUE
prev_was_runtime=false
for arg in "$@"; do
  if $prev_was_runtime; then
    case "$arg" in
      local|fly) RUNTIME="$arg" ;;
      *)
        echo "pila: --runtime $arg is not one of local|fly" >&2
        exit 1
        ;;
    esac
    prev_was_runtime=false
    continue
  fi
  if [ "$arg" = "--runtime" ]; then
    prev_was_runtime=true
  fi
done

echo "$RUNTIME"
"""


def _run(
    repo_root: Path,
    env: dict,
    cli_args: list[str],
    *,
    expect_fail: bool = False,
) -> tuple[str, str]:
    """Run the harness; return (stdout, stderr).  Raises on non-zero exit
    unless expect_fail=True."""
    result = subprocess.run(
        ["bash", "-c", _HARNESS, "--", str(repo_root)] + cli_args,
        env={**{"PATH": "/usr/bin:/bin"}, **env},
        capture_output=True,
        text=True,
    )
    if not expect_fail:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip(), result.stderr.strip()


# ── defaults ──────────────────────────────────────────────────────────────────


def test_default_is_local(tmp_path):
    out, _ = _run(tmp_path, {}, [])
    assert out == "local"


# ── canonical env PILA_RUNTIME ───────────────────────────────────────────────


def test_pila_runtime_fly(tmp_path):
    out, _ = _run(tmp_path, {"PILA_RUNTIME": "fly"}, [])
    assert out == "fly"


def test_pila_runtime_local_explicit(tmp_path):
    out, _ = _run(tmp_path, {"PILA_RUNTIME": "local"}, [])
    assert out == "local"


def test_pila_runtime_empty_treated_as_unset(tmp_path):
    out, _ = _run(tmp_path, {"PILA_RUNTIME": ""}, [])
    assert out == "local"


def test_pila_runtime_invalid_exits_nonzero(tmp_path):
    _, err = _run(tmp_path, {"PILA_RUNTIME": "nope"}, [], expect_fail=True)
    assert "is not one of local|fly" in err
    assert "nope" in err


# ── canonical TOML `runtime` ─────────────────────────────────────────────────


def test_toml_runtime_fly(tmp_path):
    (tmp_path / "pila.toml").write_text("runtime = fly\n")
    out, _ = _run(tmp_path, {}, [])
    assert out == "fly"


def test_toml_runtime_local_explicit(tmp_path):
    (tmp_path / "pila.toml").write_text("runtime = local\n")
    out, _ = _run(tmp_path, {}, [])
    assert out == "local"


def test_toml_runtime_invalid_exits_nonzero(tmp_path):
    (tmp_path / "pila.toml").write_text("runtime = bogus\n")
    _, err = _run(tmp_path, {}, [], expect_fail=True)
    assert "is not one of local|fly" in err
    assert "bogus" in err


def test_toml_runtime_unrelated_key_stays_local(tmp_path):
    (tmp_path / "pila.toml").write_text("source_of_truth = codebase\n")
    out, _ = _run(tmp_path, {}, [])
    assert out == "local"


# ── canonical CLI --runtime ───────────────────────────────────────────────────


def test_cli_runtime_equals_fly(tmp_path):
    out, _ = _run(tmp_path, {}, ["--runtime=fly"])
    assert out == "fly"


def test_cli_runtime_equals_local(tmp_path):
    out, _ = _run(tmp_path, {}, ["--runtime=local"])
    assert out == "local"


def test_cli_runtime_space_fly(tmp_path):
    out, _ = _run(tmp_path, {}, ["--runtime", "fly"])
    assert out == "fly"


def test_cli_runtime_space_local(tmp_path):
    out, _ = _run(tmp_path, {}, ["--runtime", "local"])
    assert out == "local"


def test_cli_runtime_invalid_exits_nonzero(tmp_path):
    _, err = _run(tmp_path, {}, ["--runtime=bad"], expect_fail=True)
    assert "is not one of local|fly" in err


# ── precedence: CLI > env > TOML ──────────────────────────────────────────────


def test_cli_wins_over_env(tmp_path):
    out, _ = _run(tmp_path, {"PILA_RUNTIME": "fly"}, ["--runtime=local"])
    assert out == "local"


def test_env_wins_over_toml(tmp_path):
    (tmp_path / "pila.toml").write_text("runtime = fly\n")
    out, _ = _run(tmp_path, {"PILA_RUNTIME": "local"}, [])
    assert out == "local"


def test_cli_wins_over_toml(tmp_path):
    (tmp_path / "pila.toml").write_text("runtime = fly\n")
    out, _ = _run(tmp_path, {}, ["--runtime=local"])
    assert out == "local"


# ── legacy backward-compat aliases ────────────────────────────────────────────


def test_legacy_env_pila_remote_maps_to_fly(tmp_path):
    out, _ = _run(tmp_path, {"PILA_REMOTE": "1"}, [])
    assert out == "fly"


def test_legacy_cli_remote_maps_to_fly(tmp_path):
    out, _ = _run(tmp_path, {}, ["--remote"])
    assert out == "fly"


def test_legacy_toml_remote_true_maps_to_fly(tmp_path):
    (tmp_path / "pila.toml").write_text("remote = true\n")
    out, _ = _run(tmp_path, {}, [])
    assert out == "fly"


def test_canonical_env_beats_legacy_env(tmp_path):
    """PILA_RUNTIME=local beats PILA_REMOTE=1 because canonical is resolved after."""
    out, _ = _run(tmp_path, {"PILA_REMOTE": "1", "PILA_RUNTIME": "local"}, [])
    assert out == "local"


def test_canonical_toml_beats_legacy_toml(tmp_path):
    """runtime=local wins over remote=true in the same pila.toml."""
    (tmp_path / "pila.toml").write_text("remote = true\nruntime = local\n")
    out, _ = _run(tmp_path, {}, [])
    assert out == "local"
