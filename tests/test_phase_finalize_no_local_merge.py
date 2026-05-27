"""phase_finalize must not perform a local merge into the working branch.

Regression pin for the design change: the run branch is the integration
artifact and the PR is the proposed integration into the working branch.
The working branch must be unchanged locally after a successful run.
"""
from __future__ import annotations

import inspect
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_finalize_sh_does_not_merge(centella):
    """finalize.sh must not perform a local merge into the working branch.
    The script remains as a thin run-branch verifier."""
    script = (REPO_ROOT / "scripts" / "finalize.sh").read_text()
    assert "git merge" not in script, (
        "finalize.sh must not merge into the working branch."
    )
    assert "git checkout" not in script, (
        "finalize.sh must not switch the user off their working branch."
    )


def test_phase_finalize_drops_post_merge_sanity_checks(centella):
    """The two post-merge sanity checks (centella.py:4965-4982) assume a
    merge just happened on HEAD. After the design change, they are
    incoherent and must be removed. Pin their disappearance by source-text."""
    src = inspect.getsource(centella.phase_finalize)
    assert "centella merge commit not found at HEAD" not in src, (
        "post-merge sanity check 1 (looking for the centella: merge commit "
        "subject on HEAD) is incoherent without a local merge."
    )
    assert "working branch diverges from" not in src, (
        "post-merge sanity check 2 (diff between run branch and HEAD) is "
        "incoherent without a local merge."
    )


def test_push_and_open_pr_die_message_no_final_merge_commit(centella):
    """The push-failure die() message used to say 'working branch ... (has
    the final merge commit)'. After the design change there is no final
    merge commit on the working branch."""
    src = inspect.getsource(centella.push_and_open_pr)
    assert "has the final merge commit" not in src, (
        "push_and_open_pr's push-failure message must not claim the working "
        "branch holds a final merge commit — it no longer does."
    )


def test_phase_finalize_keeps_post_push_invariants(centella):
    """The finalize path still writes finished_at, calls push_and_open_pr
    (gated on no_push), and runs cleanup with --run-id."""
    src = inspect.getsource(centella.phase_finalize)
    assert 'st.data["finished_at"] = now()' in src
    assert "push_and_open_pr(st, no_verify=no_verify)" in src
    assert 'run_script("cleanup.sh", "--run-id", st.run_id)' in src
