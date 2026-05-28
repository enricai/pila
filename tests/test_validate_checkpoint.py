"""Tests for validate_checkpoint() — the structural and freshness checks
on an `incomplete-handoff` checkpoint file.

Cover three classes of failure:
  1. Missing section header (pre-existing rule).
  2. Required section that has no content, or only placeholder tokens
     like "none" / "n/a" / "tbd". The two `_ALLOW_NONE` sections
     (Decisions made, Open unknowns) tolerate these.
  3. Stale freshness: `## Files touched` lists a path the worktree
     doesn't have, with no [deleted] annotation.

These cases all produce confused successors that re-discover what the
prior worker should have recorded.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _full_good_checkpoint() -> str:
    """A complete checkpoint with real content in every section."""
    return (
        "# Checkpoint: feat-001\n"
        "## Frozen success criteria\n"
        "- [x] criterion 1 verified\n"
        "## Current status\n"
        "Half-implemented; endpoint exists, tests pending.\n"
        "## Files touched\n"
        "- src/api/handler.py — added new route\n"
        "## Decisions made\n"
        "- chose JSON over msgpack for the payload\n"
        "## Evidence gate status\n"
        "root_cause=9.2, solution=8.1; one falsifier still open\n"
        "## Next action\n"
        "Wire up the unit test against the new route.\n"
        "## Open unknowns\n"
        "- whether to add rate-limiting (deferred)\n"
    )


# ----- structural / presence checks ------------------------------------------

def test_well_formed_checkpoint_passes(pila, tmp_path):
    p = tmp_path / "feat-001.md"
    p.write_text(_full_good_checkpoint())
    assert pila.validate_checkpoint(str(p)) is None


def test_missing_section_fails(pila, tmp_path):
    bad = _full_good_checkpoint().replace("## Next action\n"
                                          "Wire up the unit test against the new route.\n",
                                          "")
    p = tmp_path / "bad.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None
    assert "## Next action" in err


def test_file_missing_returns_error(pila, tmp_path):
    err = pila.validate_checkpoint(str(tmp_path / "nope.md"))
    assert err is not None
    assert "does not exist" in err


# ----- empty-content rejection -----------------------------------------------

def test_empty_required_section_fails(pila, tmp_path):
    # ## Current status header is present but the section body is blank.
    bad = _full_good_checkpoint().replace(
        "## Current status\n"
        "Half-implemented; endpoint exists, tests pending.\n",
        "## Current status\n\n")
    p = tmp_path / "empty-status.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None
    assert "## Current status" in err
    assert "no content" in err


def test_whitespace_only_required_section_fails(pila, tmp_path):
    # Header present, body is just whitespace.
    bad = _full_good_checkpoint().replace(
        "## Files touched\n"
        "- src/api/handler.py — added new route\n",
        "## Files touched\n   \n\t\n")
    p = tmp_path / "ws-touched.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None
    assert "## Files touched" in err


# ----- noise-token rejection -------------------------------------------------

def test_noise_token_in_required_section_fails(pila, tmp_path):
    # `none` is a single-token placeholder. Allowed in Decisions made /
    # Open unknowns but NOT in a section that must carry handoff context.
    bad = _full_good_checkpoint().replace(
        "## Current status\n"
        "Half-implemented; endpoint exists, tests pending.\n",
        "## Current status\nnone\n")
    p = tmp_path / "none-status.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None
    assert "placeholder" in err


def test_bullet_noise_token_in_required_section_fails(pila, tmp_path):
    # `- none` should be rejected the same as bare `none` — bullet
    # markers don't change the meaning.
    bad = _full_good_checkpoint().replace(
        "## Next action\n"
        "Wire up the unit test against the new route.\n",
        "## Next action\n- tbd\n")
    p = tmp_path / "tbd-next.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None
    assert "placeholder" in err


def test_noise_token_in_open_unknowns_passes(pila, tmp_path):
    # `## Open unknowns: none` is a legitimate answer ("there are no
    # open unknowns"), distinct from missing handoff context.
    ok = _full_good_checkpoint().replace(
        "## Open unknowns\n"
        "- whether to add rate-limiting (deferred)\n",
        "## Open unknowns\nnone\n")
    p = tmp_path / "open-none.md"
    p.write_text(ok)
    assert pila.validate_checkpoint(str(p)) is None


def test_noise_token_in_decisions_made_passes(pila, tmp_path):
    # `## Decisions made: n/a` is a legitimate answer for a worker that
    # was forced to hand off before reaching any decision points.
    ok = _full_good_checkpoint().replace(
        "## Decisions made\n"
        "- chose JSON over msgpack for the payload\n",
        "## Decisions made\nn/a\n")
    p = tmp_path / "dec-na.md"
    p.write_text(ok)
    assert pila.validate_checkpoint(str(p)) is None


# Widened token set + trailing-punctuation normalization. Each value is
# the entire body of `## Current status` (a section that must carry
# handoff context, so a placeholder body is always rejected).
@pytest.mark.parametrize("body", [
    # widened English tokens
    "nothing",
    "Unknown",
    "todo",
    "pending",
    # trailing punctuation on the original tokens
    "None.",
    "n/a.",
    "TBD!",
    "tbd…",
    # repeated `?` collapses
    "???",
    "????",
    # bullet + widened + trailing punctuation, combined
    "- Nothing.",
    "* unknown!",
])
def test_widened_noise_tokens_rejected(pila, tmp_path, body):
    bad = _full_good_checkpoint().replace(
        "## Current status\n"
        "Half-implemented; endpoint exists, tests pending.\n",
        f"## Current status\n{body}\n")
    p = tmp_path / "bad.md"
    p.write_text(bad)
    err = pila.validate_checkpoint(str(p))
    assert err is not None, f"expected rejection for body={body!r}"
    assert "placeholder" in err


@pytest.mark.parametrize("body", [
    # real prose with a trailing period: must NOT be mistaken for a token
    "Half-implemented; endpoint exists, tests pending.",
    # a word that happens to start with a noise token but carries content
    "None of the auth paths have been wired yet.",
    # bullet with substantive content
    "- todo: this is real next-step content, not a placeholder",
])
def test_real_content_not_falsely_rejected(pila, tmp_path, body):
    ok = _full_good_checkpoint().replace(
        "## Current status\n"
        "Half-implemented; endpoint exists, tests pending.\n",
        f"## Current status\n{body}\n")
    p = tmp_path / "ok.md"
    p.write_text(ok)
    assert pila.validate_checkpoint(str(p)) is None, (
        f"unexpected rejection for body={body!r}")


# ----- freshness check on `## Files touched` ---------------------------------

def test_freshness_check_path_exists(pila, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "handler.py").write_text("# real file\n")

    content = _full_good_checkpoint().replace(
        "- src/api/handler.py — added new route\n",
        "- src/handler.py — added new route\n")
    p = tmp_path / "ok.md"
    p.write_text(content)
    assert pila.validate_checkpoint(str(p), worktree_root=worktree) is None


def test_freshness_check_path_missing_fails(pila, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # src/handler.py is NOT created — checkpoint references a missing
    # file with no [deleted] flag.
    content = _full_good_checkpoint().replace(
        "- src/api/handler.py — added new route\n",
        "- src/handler.py — added new route\n")
    p = tmp_path / "stale.md"
    p.write_text(content)
    err = pila.validate_checkpoint(str(p), worktree_root=worktree)
    assert err is not None
    assert "src/handler.py" in err
    assert "stale" in err or "deleted" in err


def test_freshness_check_deleted_annotation_passes(pila, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # File doesn't exist but is flagged [deleted] — that's a legitimate
    # state for a refactor that removed the file.
    content = _full_good_checkpoint().replace(
        "- src/api/handler.py — added new route\n",
        "- src/old_handler.py [deleted] — replaced by new module\n")
    p = tmp_path / "deleted.md"
    p.write_text(content)
    assert pila.validate_checkpoint(str(p), worktree_root=worktree) is None


def test_freshness_check_skipped_when_worktree_gone(pila, tmp_path):
    # If the worktree directory is missing (already cleaned up), the
    # freshness check is skipped — there's nothing to validate against.
    content = _full_good_checkpoint().replace(
        "- src/api/handler.py — added new route\n",
        "- src/whatever.py — does not exist anywhere\n")
    p = tmp_path / "no-wt.md"
    p.write_text(content)
    nonexistent = tmp_path / "ghost"
    assert pila.validate_checkpoint(str(p), worktree_root=nonexistent) is None


def test_freshness_check_narration_lines_ignored(pila, tmp_path):
    # A `## Files touched` bullet without a path token (e.g. "see above")
    # is narration, not a path claim. Should not trigger a freshness
    # failure.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    content = _full_good_checkpoint().replace(
        "- src/api/handler.py — added new route\n",
        "- see commit log for the full list\n")
    p = tmp_path / "narration.md"
    p.write_text(content)
    assert pila.validate_checkpoint(str(p), worktree_root=worktree) is None


# ----- backward compatibility ------------------------------------------------

def test_validate_checkpoint_works_without_worktree_root(pila, tmp_path):
    # The original signature was validate_checkpoint(path) — keep that path
    # working so callers that don't have a worktree handy still get the
    # structural + content checks.
    p = tmp_path / "old-style.md"
    p.write_text(_full_good_checkpoint())
    assert pila.validate_checkpoint(str(p)) is None
