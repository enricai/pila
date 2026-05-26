"""Regression test for the per-run refactor: `check_diff_scope` and the
finalize divergence check must compute diffs against the run branch
(`centella/<run-id>`), not the legacy `centella/staging` branch.

Background: commit 3 of the parallel-safe refactor moved branch names
from a global `centella/staging` to per-run `centella/<run-id>`. Two
git-diff call sites kept the old name hardcoded, which means git
returned non-zero and the functions silently returned `None` —
disabling DESIGN §12's protected-path enforcement (check_diff_scope)
and the finalize divergence-warning check.

This test pins the source so a future regression would fail loudly
instead of silently disabling enforcement again. Source-text pin
(no live git), matches the style of `test_validator_tools.py` and
`test_preflight_cli_version.py`.
"""
from __future__ import annotations

import inspect


def test_check_diff_scope_uses_run_branch(centella):
    """check_diff_scope must compute its diff against the run branch,
    not the legacy `centella/staging`. Failure here means workers can
    write to `.centella/`, `.git/`, or `.claude/` without being caught."""
    src = inspect.getsource(centella.check_diff_scope)
    # Must use compute_run_branch (or otherwise derive the run branch
    # from st.run_id) — never the literal `centella/staging`.
    assert "centella/staging..HEAD" not in src, (
        "check_diff_scope is back to using the legacy "
        "'centella/staging' branch, which no longer exists under "
        "per-run. The protected-path enforcement is silently disabled."
    )
    assert "compute_run_branch(st.run_id)" in src, (
        "check_diff_scope must derive its diff base from "
        "compute_run_branch(st.run_id) so the check fires against the "
        "actual per-run branch."
    )


def test_phase_finalize_divergence_uses_run_branch(centella):
    """phase_finalize's post-merge divergence warning must compute the
    diff against the run branch, not centella/staging. Otherwise the
    warning never fires (git returns non-zero) and a silent merge that
    drops changes goes unreported."""
    src = inspect.getsource(centella.phase_finalize)
    assert "centella/staging..HEAD" not in src, (
        "phase_finalize is back to using the legacy 'centella/staging' "
        "branch for the divergence check. The warning would never fire."
    )
    assert "compute_run_branch(st.run_id)" in src, (
        "phase_finalize must compute the divergence diff against "
        "compute_run_branch(st.run_id)."
    )


def test_no_legacy_staging_branch_in_executable_code(centella):
    """Sweep guard: no remaining executable references to `centella/staging`
    in centella.py. The string may appear in comments, docstrings, or the
    legacy-detection code (which must mention it by name to clean it up),
    but never as a live argument to a git command."""
    import re
    from pathlib import Path
    src = Path(centella.__file__).read_text()

    # Strip comments and string literals' contents that are documentation.
    # Easiest: line-by-line, look for `centella/staging` outside of `#`
    # comments and outside of the legacy-detection block (which lives
    # entirely inside main() at the end of the file).
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "centella/staging" not in line:
            continue
        # Allow it inside the legacy-detection block in main() — the
        # block name-checks the string.
        if "legacy" in line.lower() or 'Path(".centella/state.json")' in line:
            continue
        # Allow string literals that are clearly documentation/error
        # text about the historical bug (the docstring we added in the
        # fix). We're looking for *git commands*, which are list args.
        if 'subprocess' in line or '"git"' in line or "'git'" in line:
            raise AssertionError(
                f"centella.py:{lineno} still passes 'centella/staging' "
                f"to a git command: {line.strip()!r}"
            )
