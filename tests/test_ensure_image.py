"""Tests for ensure_image() in the pila launcher.

Phase 1B: the launcher's RUNTIME=fly branch calls ensure_image() before
provision_machine to close the operator-step gap where the first remote
run fails because the registry tag wasn't built/pushed yet.

Strategy: cache positive hits at ~/.cache/pila/published-tags.txt; on
miss, invoke build-push.sh --push (which is idempotent at the registry).

ensure_image() lives in the bash launcher, so the tests use the same
isolated-harness pattern as test_launcher_runtime_knob.py / source the
launcher's function block into a minimal bash script with build-push.sh
stubbed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Bash harness that mirrors ensure_image() from the launcher. We don't
# source `pila` directly because it runs preflight + dispatch on source;
# the function block is small enough to keep in sync via the
# coupling test below.
_HARNESS = r"""
#!/usr/bin/env bash
set -euo pipefail

# Test inputs:
#   $XDG_CACHE_HOME → forced to a temp dir so the test never touches
#                     the real user cache.
#   $PILA_REPO      → forced to a temp dir holding a stub build-push.sh.
#   $PILA_FLY_APP   → app name (default: pila).
#   $1              → image tag to ensure.

ensure_image() {
  local tag="$1" cache_dir cache_file
  cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/pila"
  cache_file="$cache_dir/published-tags.txt"
  if [ -f "$cache_file" ] && grep -Fxq "$tag" "$cache_file" 2>/dev/null; then
    return 0
  fi
  local build_push="$PILA_REPO/scripts/remote/build-push.sh"
  if [ ! -x "$build_push" ]; then
    echo "pila: error: $build_push not found or not executable" >&2
    return 1
  fi
  local fly_app="${PILA_FLY_APP:-pila}"
  echo "[pila] remote: ensuring image $tag is published (cache miss)" >&2
  if ! "$build_push" --app "$fly_app" --push; then
    echo "pila: error: build-push.sh failed; remote run cannot proceed" >&2
    return 1
  fi
  mkdir -p "$cache_dir"
  printf '%s\n' "$tag" >> "$cache_file"
  return 0
}

ensure_image "$1"
"""


def _run(tag: str, *, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    base_env = {k: v for k, v in os.environ.items()}
    base_env.update(env)
    return subprocess.run(
        ["bash", "-c", _HARNESS, "harness", tag],
        env=base_env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _stub_build_push(repo: Path, *, exit_code: int = 0, log_file: Path | None = None) -> Path:
    """Write a stub scripts/remote/build-push.sh that records its invocation."""
    scripts = repo / "scripts" / "remote"
    scripts.mkdir(parents=True)
    bp = scripts / "build-push.sh"
    log_arg = f'"{log_file}"' if log_file is not None else '/dev/null'
    bp.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "build-push stub invoked: $*" >> {log_arg}\n'
        f"exit {exit_code}\n"
    )
    bp.chmod(0o755)
    return bp


def test_cache_hit_skips_build_push(tmp_path: Path):
    """If the tag is in the cache, ensure_image returns 0 without invoking build-push."""
    repo = tmp_path / "pila-repo"
    repo.mkdir()
    log = tmp_path / "build-push-invocations.log"
    _stub_build_push(repo, log_file=log)

    cache_home = tmp_path / "cache"
    cache_dir = cache_home / "pila"
    cache_dir.mkdir(parents=True)
    (cache_dir / "published-tags.txt").write_text(
        "registry.fly.io/pila:0.2.1\n"
    )

    result = _run(
        "registry.fly.io/pila:0.2.1",
        env={
            "XDG_CACHE_HOME": str(cache_home),
            "PILA_REPO": str(repo),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert not log.exists() or log.read_text() == "", (
        "build-push.sh should not be invoked on cache hit"
    )


def test_cache_miss_invokes_build_push_and_records_tag(tmp_path: Path):
    """On cache miss, ensure_image runs build-push.sh and appends the tag to the cache."""
    repo = tmp_path / "pila-repo"
    repo.mkdir()
    log = tmp_path / "build-push-invocations.log"
    _stub_build_push(repo, log_file=log)

    cache_home = tmp_path / "cache"

    result = _run(
        "registry.fly.io/pila:9.9.9",
        env={
            "XDG_CACHE_HOME": str(cache_home),
            "PILA_REPO": str(repo),
            "PILA_FLY_APP": "pila",
        },
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    # build-push.sh should have been invoked with --app pila --push.
    assert log.exists(), "build-push.sh should be invoked on cache miss"
    invocation = log.read_text().strip()
    assert "--app pila --push" in invocation, invocation
    # Tag should now be recorded in the cache.
    cache_file = cache_home / "pila" / "published-tags.txt"
    assert cache_file.exists()
    assert "registry.fly.io/pila:9.9.9" in cache_file.read_text()


def test_build_push_failure_propagates(tmp_path: Path):
    """If build-push.sh exits non-zero, ensure_image returns 1 and does not cache."""
    repo = tmp_path / "pila-repo"
    repo.mkdir()
    _stub_build_push(repo, exit_code=2)

    cache_home = tmp_path / "cache"

    result = _run(
        "registry.fly.io/pila:bad",
        env={
            "XDG_CACHE_HOME": str(cache_home),
            "PILA_REPO": str(repo),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 1, result.stderr
    assert "build-push.sh failed" in result.stderr
    # The failed tag must NOT be recorded — that's how the cache stays
    # a positive list (a missing tag means "probe", not "absent").
    cache_file = cache_home / "pila" / "published-tags.txt"
    assert not cache_file.exists() or "bad" not in cache_file.read_text()


def test_missing_build_push_script_errors(tmp_path: Path):
    """If scripts/remote/build-push.sh is missing, ensure_image errors with a clear message."""
    repo = tmp_path / "pila-repo"
    repo.mkdir()
    # No build-push.sh.

    cache_home = tmp_path / "cache"

    result = _run(
        "registry.fly.io/pila:0.0.0",
        env={
            "XDG_CACHE_HOME": str(cache_home),
            "PILA_REPO": str(repo),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "build-push.sh" in result.stderr
    assert "not found" in result.stderr or "not executable" in result.stderr


def test_positive_cache_only_unrelated_tags_still_probe(tmp_path: Path):
    """A cache entry for tag A must not satisfy a lookup for tag B."""
    repo = tmp_path / "pila-repo"
    repo.mkdir()
    log = tmp_path / "build-push.log"
    _stub_build_push(repo, log_file=log)

    cache_home = tmp_path / "cache"
    cache_dir = cache_home / "pila"
    cache_dir.mkdir(parents=True)
    (cache_dir / "published-tags.txt").write_text(
        "registry.fly.io/pila:0.1.0\n"
    )

    result = _run(
        "registry.fly.io/pila:0.2.0",
        env={
            "XDG_CACHE_HOME": str(cache_home),
            "PILA_REPO": str(repo),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert log.exists(), "build-push.sh should be invoked for unrelated tag"


def test_no_auto_publish_flag_consumed_by_launcher():
    """The launcher consumes --no-auto-publish in REWRITTEN_ARGS, not forwarded to orch."""
    pila_launcher = REPO_ROOT / "pila"
    text = pila_launcher.read_text()
    # The flag must be parsed early (env + arg loop).
    assert "NO_AUTO_PUBLISH" in text
    assert "PILA_NO_AUTO_PUBLISH" in text
    # The flag must be in the REWRITTEN_ARGS consumption block so the
    # orchestrator's argparse never sees it.
    assert "--no-auto-publish)" in text


def test_ensure_image_harness_matches_launcher():
    """Coupling test: the harness used in this file must match the live launcher.

    If you edit ensure_image() in the launcher, update the _HARNESS in this
    file accordingly. This test catches drift by checking that key tokens
    co-occur in both places.
    """
    pila_launcher = REPO_ROOT / "pila"
    launcher_text = pila_launcher.read_text()
    # The function body's load-bearing lines must appear in both.
    sentinels = [
        'cache_file="$cache_dir/published-tags.txt"',
        'grep -Fxq "$tag" "$cache_file"',
        '"$build_push" --app "$fly_app" --push',
        'printf \'%s\\n\' "$tag" >> "$cache_file"',
    ]
    for s in sentinels:
        assert s in launcher_text, f"missing in launcher: {s}"
        assert s in _HARNESS, f"missing in harness: {s}"
