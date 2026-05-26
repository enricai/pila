"""Tests for legacy (pre-per-run) layout detection.

The per-run refactor moves state from `.centella/state.json` to
`.centella/runs/<run-id>/state.json`. Users upgrading from a previous
centella version need a clear migration path: when centella detects the
old layout, it dies with an instruction to run `scripts/cleanup.sh
--legacy`.

Covers:
- Source-text pin: main() has the legacy detection guard.
- Source-text pin: cleanup.sh has the --legacy mode.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CENTELLA_PY = REPO_ROOT / "orchestrator" / "centella.py"
CLEANUP_SH = REPO_ROOT / "scripts" / "cleanup.sh"


def test_main_detects_legacy_state_json():
    """main() checks for `.centella/state.json` at the top of the run
    and dies with a migration hint. Source-text pin guards against the
    check being silently removed."""
    src = CENTELLA_PY.read_text()
    # Locate main()'s body. main() is the last top-level def in
    # centella.py, so we grab everything from `def main():` to either
    # the next top-level def/class OR the if __name__ == "__main__":
    # block, whichever comes first.
    m = re.search(
        r"^def main\(\) -> None:\n(.*?)(?=^(?:def |class |if __name__))",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate main() in centella.py"
    main_body = m.group(1)
    assert 'Path(".centella/state.json").exists()' in main_body, (
        "main() must detect the legacy `.centella/state.json` layout "
        "and die — otherwise users upgrading from pre-per-run silently "
        "lose their state.json's contents"
    )
    assert 'legacy state layout' in main_body, (
        "the legacy-detection die() should name the failure mode clearly"
    )
    assert 'cleanup.sh --legacy' in main_body, (
        "the legacy-detection die() should tell the user the exact "
        "command to migrate"
    )


def test_cleanup_legacy_mode_exists():
    """cleanup.sh has a `--legacy` mode."""
    src = CLEANUP_SH.read_text()
    assert '--legacy' in src
    # The legacy mode removes the old top-level state.json.
    assert 'rm -f .centella/state.json' in src


def test_cleanup_legacy_removes_old_worktrees():
    """The --legacy mode removes the top-level .centella/worktrees/ dir
    (the pre-per-run worktree location). Per-run worktrees under
    .centella/runs/<id>/worktrees/ are not touched."""
    src = CLEANUP_SH.read_text()
    assert '.centella/worktrees' in src


def test_cleanup_legacy_removes_centella_staging_branch():
    """The legacy mode deletes the old shared `centella/staging` branch."""
    src = CLEANUP_SH.read_text()
    assert 'centella/staging' in src
    # Specifically, the branch-delete line.
    assert 'git branch -D centella/staging' in src


def test_cleanup_legacy_preserves_per_run_branches():
    """Per-run branches (centella/<run-id>/<sid>) have two segments
    after `centella/`. The legacy mode must NOT delete those."""
    src = CLEANUP_SH.read_text()
    # The mode iterates centella/* branches and uses a case statement to
    # skip two-segment names. We pin the structural element.
    assert 'centella/*/*' in src, (
        "cleanup.sh --legacy must use the centella/*/* glob to preserve "
        "two-segment per-run subtask branches"
    )
