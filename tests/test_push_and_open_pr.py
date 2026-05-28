"""Source-text pins for `push_and_open_pr()` and its wiring.

End-to-end behavior (git push + gh pr create) requires a real GitHub
remote, so the bulk of the verification is by source-text inspection:
the function uses the right primitives (compose_pr_body, _write_run_json,
compute_run_branch), branches on push success/failure, and the wiring
in phase_finalize and main()/argparse plumbs --no-push and --no-verify
through correctly.

The plan's end-to-end manual verification covers the live `gh` /
`origin` path. Source-text pins prevent silent regressions of the
documented behavior.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PILA_PY = REPO_ROOT / "orchestrator" / "pila.py"


# --- push_and_open_pr internal contract ----------------------------------

def test_push_and_open_pr_exists(pila):
    """The helper is exported at module scope."""
    assert callable(getattr(pila, "push_and_open_pr", None))


def test_push_and_open_pr_uses_compute_run_branch(pila):
    """The function derives the branch name from compute_run_branch(st.run_id),
    not by hardcoding `pila/<something>`. Pin so a refactor doesn't
    silently re-introduce a stale branch reference."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert "compute_run_branch(st.run_id)" in src


def test_push_and_open_pr_uses_compose_pr_body(pila):
    """The PR body is generated via compose_pr_body — not by inlining
    the template, which would create a second source of truth."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert "compose_pr_body(st.data, st.run_id)" in src


def test_push_and_open_pr_writes_run_json_on_success(pila):
    """On a successful push, push_and_open_pr writes pushed_at to
    run.json. On a successful PR, it writes pr_url."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert "pushed_at=pushed_at" in src
    assert "pr_url=pr_url" in src


def test_push_and_open_pr_writes_run_json_on_failure(pila):
    """On a failed push, push_error goes into run.json. On a failed PR,
    pr_error does."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert "push_error=" in src
    assert "pr_error=" in src


def test_push_and_open_pr_push_failure_is_fatal(pila):
    """Push failure calls die() — the run branch is intact, but the run
    is considered failed at the push step (the user must retry)."""
    src = inspect.getsource(pila.push_and_open_pr)
    # The push failure branch dies; the PR failure branch returns.
    # Look for both: at least one `die(` call and at least one `return`
    # statement in the body.
    assert "die(" in src
    # PR failure path: non-fatal, just logs and returns.
    assert "return\n" in src


def test_push_and_open_pr_passes_no_verify(pila):
    """The --no-verify flag is plumbed to the git push command (not to
    worker commits or gh)."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert "no_verify" in src
    # The flag conditionally appends `--no-verify` to push_cmd.
    assert '"--no-verify"' in src


def test_push_and_open_pr_uses_origin(pila):
    """The push target is hardcoded to `origin` per the plan's
    documented limitation. Pin so a refactor doesn't accidentally
    change this (and the doc claim becomes false)."""
    src = inspect.getsource(pila.push_and_open_pr)
    assert '"origin"' in src


# --- phase_finalize wiring -------------------------------------------------

def test_phase_finalize_takes_no_push_and_no_verify(pila):
    sig = inspect.signature(pila.phase_finalize)
    assert "no_push" in sig.parameters
    assert "no_verify" in sig.parameters


def test_phase_finalize_calls_push_and_open_pr_unless_no_push(pila):
    """When --no-push is set, phase_finalize logs the skip and does NOT
    call push_and_open_pr. Otherwise it does."""
    src = inspect.getsource(pila.phase_finalize)
    assert "if no_push:" in src
    assert "push_and_open_pr(st, no_verify=no_verify)" in src
    # The skip log must mention --no-push so the user sees it.
    assert "--no-push" in src


# --- initial run.json write (regression: must not be lost) ----------------

def test_orchestrate_writes_initial_run_json_after_rename():
    """After the bootstrap → final-run-id rename, orchestrate() must
    write the immutable identity fields (run_id, branch, working_branch,
    started_at, task) into run.json. Without this initial write, a run
    that fails before phase_finalize has no run.json on disk, and
    `pila --list` (commit 5) can't enumerate it.

    Source-text pin: a future refactor that loses this call site would
    silently regress `--list` behavior for in-progress runs."""
    src = PILA_PY.read_text()
    # Locate the bootstrap-rename block inside orchestrate().
    block_match = re.search(
        r"st\.rename_to\(final_run_id\)(.*?)(?=^\s*#\s*gather_answers blocks)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert block_match, (
        "could not locate the bootstrap-rename block in orchestrate()"
    )
    block = block_match.group(1)
    assert "_write_run_json(" in block, (
        "orchestrate() must call _write_run_json after st.rename_to so "
        "the run.json sidecar exists from the moment the run has a "
        "stable identity (not only after finalize)."
    )
    # Pin every required field by name.
    for field in ("run_id=", "branch=", "working_branch=",
                  "started_at=", "task="):
        assert field in block, (
            f"initial _write_run_json call is missing field '{field}'. "
            "Fields needed for pila --list: run_id, branch, "
            "working_branch, started_at, task."
        )


def test_orchestrate_initial_write_uses_compute_run_branch():
    """The `branch` field in the initial run.json must derive from
    compute_run_branch(run_id), not be hardcoded."""
    src = PILA_PY.read_text()
    block_match = re.search(
        r"st\.rename_to\(final_run_id\)(.*?)(?=^\s*#\s*gather_answers blocks)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert block_match
    block = block_match.group(1)
    assert "compute_run_branch(final_run_id)" in block, (
        "initial _write_run_json must use compute_run_branch(final_run_id) "
        "for the `branch` field — hardcoding would break if compute_run_branch's "
        "shape ever changes."
    )


# --- preflight wiring ------------------------------------------------------

def test_preflight_calls_check_gh_cli(pila):
    """preflight() invokes _check_gh_cli with the resolved no_push value."""
    src = inspect.getsource(pila.preflight)
    assert "_check_gh_cli(no_push)" in src


def test_check_gh_cli_short_circuits_on_no_push(pila):
    """The first thing _check_gh_cli does on no_push=True is return,
    so the gh/origin checks don't fire under --no-push."""
    src = inspect.getsource(pila._check_gh_cli)
    # Look for an early-return guarded by no_push.
    assert "if no_push:" in src


# --- argparse wiring -------------------------------------------------------

def test_argparse_has_no_push_and_no_verify():
    """Both new flags are declared in main()'s argparse setup. Pin by
    source-text since argparse is hard to introspect after parse_args."""
    src = PILA_PY.read_text()
    assert '"--no-push"' in src
    assert '"--no-verify"' in src


def test_no_verify_only_affects_push(pila):
    """--no-verify is plumbed only into the git push at finalize, not
    into any other git command. Specifically, no `--no-verify` should
    appear inside a worker subprocess command."""
    # The flag string appears in two locations: argparse declaration
    # and the push_and_open_pr command construction. Anywhere else
    # would be drift. Grep the whole file and confirm the count.
    src = PILA_PY.read_text()
    count = src.count('"--no-verify"')
    assert count >= 2  # argparse + push command
    # Stronger: confirm `--no-verify` doesn't appear in any obviously-wrong
    # command construction (worker invocation, integrator, etc.). Quick
    # heuristic: scan for `git commit` lines that include --no-verify.
    for line in src.splitlines():
        if "git" in line and "commit" in line.lower() and "--no-verify" in line:
            raise AssertionError(
                f"--no-verify must not appear in a worker `git commit`: {line!r}"
            )
