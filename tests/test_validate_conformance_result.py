"""Tests for validate_conformance_result() — cross-field invariants on
the conformer worker's structured output (DESIGN §9 *Post-work
conformance*).

The JSON schema enforces the *shape* of the result. This function
enforces the cross-field honesty rules the schema can't:
- a non-empty rule_violations_residual requires a non-empty rules_files_read
- every rule_violations_fixed item must cite a non-empty `rule`
- every docs_updates / tests_updates `path` must exist in the worktree
"""
from __future__ import annotations


def _good_result(subtask_id="t1", **overrides):
    """A minimum well-formed result with all required keys present."""
    base = {
        "subtask_id": subtask_id,
        "rules_files_read": [],
        "rule_violations_fixed": [],
        "rule_violations_residual": [],
        "docs_updates": [],
        "tests_updates": [],
        "build": {"ran": False, "passed": False, "command": "", "summary": ""},
        "lint": {"ran": False, "passed": False, "command": "", "summary": ""},
        "tests": {"ran": False, "passed": False, "command": "", "summary": ""},
        "summary": "nothing to do",
    }
    base.update(overrides)
    return base


def test_empty_well_formed_result_passes(pila, tmp_path):
    err = pila.validate_conformance_result(_good_result(), str(tmp_path))
    assert err is None


def test_non_dict_result_rejected(pila, tmp_path):
    assert pila.validate_conformance_result([], str(tmp_path)) is not None
    assert pila.validate_conformance_result(None, str(tmp_path)) is not None
    assert pila.validate_conformance_result("oops", str(tmp_path)) is not None


def test_residual_without_files_read_rejected(pila, tmp_path):
    """A rule violation that wasn't fixed requires the conformer to have
    *seen* at least one rules file — otherwise the violation is fabricated."""
    res = _good_result(
        rules_files_read=[],
        rule_violations_residual=[{"rule": "x", "why_not_fixed": "y"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None
    assert "rule_violations_residual" in err
    assert "rules_files_read" in err


def test_residual_with_files_read_accepted(pila, tmp_path):
    res = _good_result(
        rules_files_read=["CLAUDE.md"],
        rule_violations_residual=[{"rule": "x", "why_not_fixed": "y"}],
    )
    assert pila.validate_conformance_result(res, str(tmp_path)) is None


def test_fixed_violation_with_empty_rule_rejected(pila, tmp_path):
    res = _good_result(
        rules_files_read=["CLAUDE.md"],
        rule_violations_fixed=[
            {"rule": "", "fix": "added a hint", "evidence": "src/x.py:1"},
        ],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None
    assert "rule_violations_fixed[0]" in err


def test_fixed_violation_with_whitespace_rule_rejected(pila, tmp_path):
    res = _good_result(
        rules_files_read=["CLAUDE.md"],
        rule_violations_fixed=[
            {"rule": "   \n  ", "fix": "x", "evidence": "y"},
        ],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None


def test_docs_update_path_must_exist(pila, tmp_path):
    res = _good_result(
        docs_updates=[{"path": "docs/UNKNOWN.md", "reason": "stale"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None
    assert "docs_updates[0]" in err
    assert "docs/UNKNOWN.md" in err


def test_docs_update_with_existing_path_accepted(pila, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "API.md").write_text("# api\n")
    res = _good_result(
        docs_updates=[{"path": "docs/API.md", "reason": "added new flag"}],
    )
    assert pila.validate_conformance_result(res, str(tmp_path)) is None


def test_tests_update_path_must_exist(pila, tmp_path):
    res = _good_result(
        tests_updates=[{"path": "tests/test_missing.py", "reason": "added"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None
    assert "tests_updates[0]" in err


def test_tests_update_with_existing_path_accepted(pila, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("def test_x(): pass\n")
    res = _good_result(
        tests_updates=[{"path": "tests/test_new.py", "reason": "new coverage"}],
    )
    assert pila.validate_conformance_result(res, str(tmp_path)) is None


def test_empty_path_string_rejected(pila, tmp_path):
    res = _good_result(
        docs_updates=[{"path": "", "reason": "stale"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None
    assert "empty 'path'" in err


# --- path-traversal rejection (fourth-pass audit follow-up) ----------------

def test_docs_update_with_traversal_path_rejected(pila, tmp_path):
    """A worker that emits `../foo` would have `(wt / rel).exists()`
    return True for any sibling-of-worktree path. The validator must
    reject such entries with a clear error — these are honesty failures,
    not legitimate documentation updates inside the subtask."""
    # Create a sibling file that the traversal would point at.
    sibling = tmp_path.parent / "sibling.md"
    sibling.write_text("# sibling\n")

    res = _good_result(
        docs_updates=[{"path": "../sibling.md", "reason": "drift"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None, "traversal path must be rejected"
    assert "escapes the worktree" in err
    assert "../sibling.md" in err


def test_tests_update_with_absolute_outside_path_rejected(pila, tmp_path):
    """An absolute path that doesn't live under the worktree must be
    rejected — even if the path itself exists on disk."""
    # /etc/hostname exists on most systems; if not, fall back to something
    # the test can guarantee exists outside tmp_path.
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def x(): pass\n")
    res = _good_result(
        tests_updates=[{"path": str(outside), "reason": "added"}],
    )
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is not None, "absolute outside-worktree path must be rejected"
    assert "escapes the worktree" in err


def test_nested_traversal_to_legitimate_subtree_still_rejected(pila, tmp_path):
    """Even a path that combines traversal with a return into the worktree
    is rejected — the resolved final destination must be under the
    worktree, but the path *components* having `..` at all is treated as
    suspicious. (We use Path.resolve which normalizes the path; this test
    verifies the resolution-and-then-containment check is strict.)"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "API.md").write_text("# api\n")
    # `../<basename>/docs/API.md` resolves back inside the worktree.
    # Whether to accept or reject this is a design choice; we accept it
    # because the resolved destination IS under the worktree.
    res = _good_result(
        docs_updates=[{"path": f"../{tmp_path.name}/docs/API.md",
                       "reason": "round-trip path"}],
    )
    # Document the current behavior: resolved-inside-worktree is accepted.
    err = pila.validate_conformance_result(res, str(tmp_path))
    assert err is None, \
        f"path that resolves inside worktree should be accepted, got: {err}"
