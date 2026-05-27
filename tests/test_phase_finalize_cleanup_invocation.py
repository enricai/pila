"""phase_finalize must invoke cleanup.sh with --run-id <st.run_id>.

Regression pin for the silent-cleanup-no-op bug:

cleanup.sh's no-arg mode is the operator-facing interactive path
(IMPLEMENTATION.md §"cleanup.sh") — it scans for the most-recent
unfinished run, prompts y/N on stdin, and aborts on anything else. When
the orchestrator (which runs cleanup non-interactively) invokes it with
no args, `read -r answer` reads EOF, falls to the `*)` case, prints
"cleanup: aborted", and exits 0. The orchestrator sees a clean exit and
continues, while every worktree under .centella/runs/<id>/worktrees/
survives on disk.

The fix: pass --run-id <st.run_id> from phase_finalize so cleanup.sh
takes the explicit single-run path that does not consult stdin.
"""
from __future__ import annotations

import inspect


def test_phase_finalize_invokes_cleanup_with_run_id(centella):
    """phase_finalize must invoke cleanup.sh with --run-id and the run_id.

    Without the explicit --run-id, cleanup.sh falls into its interactive
    no-arg mode, reads EOF from the orchestrator's non-tty stdin, and
    silently aborts — leaving every subtask worktree on disk."""
    src = inspect.getsource(centella.phase_finalize)
    assert 'run_script("cleanup.sh", "--run-id", st.run_id, "--subtask-branches")' in src, (
        "phase_finalize must invoke cleanup.sh with --run-id st.run_id and "
        "--subtask-branches. The --run-id avoids the interactive no-arg "
        "path (which silently aborts non-interactively); the "
        "--subtask-branches deletes per-subtask branches that are pure "
        "clutter post-finalize while keeping centella/runs/<id> as the "
        "PR head."
    )


def test_phase_finalize_does_not_use_bare_cleanup(centella):
    """Defensive pin: the bare invocation must not reappear via refactor."""
    src = inspect.getsource(centella.phase_finalize)
    assert 'run_script("cleanup.sh")' not in src, (
        "phase_finalize must not invoke cleanup.sh with no args. "
        "The no-arg mode is the operator-facing y/N path and aborts "
        "non-interactively."
    )
