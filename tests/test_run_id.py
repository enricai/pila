"""Tests for run_id derivation primitives — DESIGN §6 "The run identifier".

Covers:
- `CATEGORY_ABBREV` coverage of every entry in `CATEGORIES` (drift guard).
- `_sanitize_slug` correctness: kebab-casing, path-traversal rejection,
  control-char handling, word-boundary truncation, empty-input fallback.
- `compute_run_id` determinism (same inputs → same id) and shape.
- `compute_run_branch` shape.
"""
from __future__ import annotations


# --- CATEGORY_ABBREV coverage ----------------------------------------------

def test_category_abbrev_covers_every_category(pila):
    """Every category in CATEGORIES must have an abbreviation. If a future
    change adds a new category, this test fails until the abbrev is added."""
    missing = [c for c in pila.CATEGORIES if c not in pila.CATEGORY_ABBREV]
    assert not missing, (
        f"CATEGORIES has entries with no CATEGORY_ABBREV: {missing}. "
        "Add abbreviations alongside any new category."
    )


def test_category_abbrev_has_no_extras(pila):
    """CATEGORY_ABBREV should not contain abbreviations for categories that
    don't exist — catches typos in the dict keys."""
    extras = [k for k in pila.CATEGORY_ABBREV if k not in pila.CATEGORIES]
    assert not extras, (
        f"CATEGORY_ABBREV has keys not in CATEGORIES (typos?): {extras}"
    )


def test_category_abbrev_values_are_short_and_safe(pila):
    """Abbreviations are embedded in git branch names; they must be
    ASCII alphanumeric (with `-` allowed) and short enough that the full
    run_id stays under typical branch-name length limits."""
    for cat, abbrev in pila.CATEGORY_ABBREV.items():
        assert 1 <= len(abbrev) <= 8, (
            f"{cat!r} → {abbrev!r}: abbrev should be 1-8 chars"
        )
        assert all(c.isalnum() or c == "-" for c in abbrev), (
            f"{cat!r} → {abbrev!r}: abbrev has non-[a-zA-Z0-9-] chars"
        )
        assert abbrev == abbrev.lower(), (
            f"{cat!r} → {abbrev!r}: abbrev must be lowercase (matches "
            "the rest of the slug shape)"
        )


# --- _sanitize_slug --------------------------------------------------------

def test_sanitize_slug_typical(pila):
    assert pila._sanitize_slug("Fix the login timeout bug") == "fix-the-login-timeout-bug"


def test_sanitize_slug_lowercases(pila):
    assert pila._sanitize_slug("ALL CAPS TASK") == "all-caps-task"


def test_sanitize_slug_collapses_repeated_dashes(pila):
    assert pila._sanitize_slug("foo---bar___baz") == "foo-bar-baz"


def test_sanitize_slug_strips_leading_trailing(pila):
    assert pila._sanitize_slug("---foo---") == "foo"
    assert pila._sanitize_slug("...foo...") == "foo"


def test_sanitize_slug_path_traversal_neutralized(pila):
    """Path-traversal characters become dashes; no '..' should appear
    in the result. Branch names and directory names sharing this shape
    must not let a freeform task break out of their namespaces."""
    out = pila._sanitize_slug("../../etc/passwd")
    assert ".." not in out
    assert "/" not in out
    assert out == "etc-passwd"


def test_sanitize_slug_strips_control_chars(pila):
    """Control characters and newlines collapse to dashes."""
    out = pila._sanitize_slug("task\nwith\tcontrol\x00chars")
    assert out == "task-with-control-chars"


def test_sanitize_slug_rejects_only_symbols(pila):
    """All-symbols input has no usable characters — returns 'task' as a
    safe non-empty fallback rather than raising."""
    assert pila._sanitize_slug("!!!@@@###") == "task"


def test_sanitize_slug_empty_string(pila):
    assert pila._sanitize_slug("") == "task"


def test_sanitize_slug_none_safe(pila):
    """Defensive: None coerces to '' rather than crashing."""
    assert pila._sanitize_slug(None) == "task"


def test_sanitize_slug_truncates_on_word_boundary(pila):
    """Long input gets cut at the last `-` within max_len, so we never
    slice a word in half."""
    out = pila._sanitize_slug(
        "add a very long task description that exceeds the limit"
    )
    assert len(out) <= 30
    # Should NOT end mid-word.
    assert not out.endswith("-")
    # Common words should be intact.
    assert "description" not in out or out.endswith("description")


def test_sanitize_slug_truncates_dashless_input(pila):
    """If the input has no '-' within max_len (e.g., a single long token
    with all non-ASCII), the hard-truncate fallback applies."""
    out = pila._sanitize_slug("a" * 80)
    assert len(out) <= 30


def test_sanitize_slug_respects_custom_max_len(pila):
    """The `max_len` parameter can be overridden."""
    out = pila._sanitize_slug("a-very-long-multi-word-task", max_len=10)
    assert len(out) <= 10


def test_sanitize_slug_unicode_becomes_dashes(pila):
    """Non-ASCII characters are not transliterated — they're replaced
    with dashes (and then collapsed). Predictable over fancy."""
    out = pila._sanitize_slug("héllo wörld")
    # h, llo, w, rld — each unicode char becomes -
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in out)
    assert "h" in out and "llo" in out


# --- compute_run_id --------------------------------------------------------

def test_compute_run_id_deterministic(pila):
    """Same inputs → same id, every time. Foundational property."""
    a = pila.compute_run_id(
        ["feature-implementation"], "add telemetry", "2026-05-26T14:31:22.847291+00:00"
    )
    b = pila.compute_run_id(
        ["feature-implementation"], "add telemetry", "2026-05-26T14:31:22.847291+00:00"
    )
    assert a == b


def test_compute_run_id_shape(pila):
    """Format: <abbrev>-<slug>-<6 hex>. Components separated by `-`."""
    rid = pila.compute_run_id(
        ["bug-fixing"], "fix the login timeout", "2026-05-26T10:00:00+00:00"
    )
    assert rid.startswith("fix-")
    parts = rid.rsplit("-", 1)
    # Last segment is 6 hex chars.
    assert len(parts[1]) == 6
    assert all(c in "0123456789abcdef" for c in parts[1])


def test_compute_run_id_uses_first_category(pila):
    """If multiple categories are returned, the first one decides the abbrev."""
    rid = pila.compute_run_id(
        ["refactoring", "testing"], "x", "2026-05-26T00:00:00+00:00"
    )
    assert rid.startswith("refactor-")


def test_compute_run_id_skips_unknown_categories(pila):
    """If the first category isn't recognized, the next one is used."""
    rid = pila.compute_run_id(
        ["not-a-category", "testing"], "x", "2026-05-26T00:00:00+00:00"
    )
    assert rid.startswith("test-")


def test_compute_run_id_falls_back_to_misc(pila):
    """No recognized category at all → 'misc'. Defensive — phase_classify
    is supposed to enforce non-empty before this function is reached."""
    rid = pila.compute_run_id([], "x", "2026-05-26T00:00:00+00:00")
    assert rid.startswith("misc-")
    rid = pila.compute_run_id(["bogus"], "x", "2026-05-26T00:00:00+00:00")
    assert rid.startswith("misc-")


def test_compute_run_id_different_timestamps_different_ids(pila):
    """Two invocations with the same task at different microseconds get
    different shortids. This is the primary collision-avoidance mechanism."""
    a = pila.compute_run_id(["testing"], "x", "2026-05-26T14:31:22.847291+00:00")
    b = pila.compute_run_id(["testing"], "x", "2026-05-26T14:31:22.847292+00:00")
    assert a != b


def test_compute_run_id_shortid_stable_per_timestamp(pila):
    """The shortid is sha1(started_at)[:6] — deterministic given the
    timestamp string. Pin the exact value to catch unintentional changes
    to the hash function."""
    import hashlib
    ts = "2026-05-26T14:31:22.847291+00:00"
    expected_shortid = hashlib.sha1(ts.encode()).hexdigest()[:6]
    rid = pila.compute_run_id(["testing"], "x", ts)
    assert rid.endswith(f"-{expected_shortid}")


def test_compute_run_id_handles_empty_timestamp(pila):
    """Defensive: an empty string still produces a valid run_id (hashes to
    sha1(b'')[:6]). In practice started_at is always set."""
    rid = pila.compute_run_id(["testing"], "x", "")
    parts = rid.rsplit("-", 1)
    assert len(parts[1]) == 6


# --- compute_run_branch ----------------------------------------------------

def test_compute_run_branch_shape(pila):
    assert pila.compute_run_branch("feat-foo-abc123") == "pila/runs/feat-foo-abc123"


def test_compute_run_branch_is_pure(pila):
    """Same input → same output. Trivial but pinning the contract."""
    rid = "fix-bar-def456"
    assert pila.compute_run_branch(rid) == pila.compute_run_branch(rid)


def test_compute_subtask_branch_shape(pila):
    assert (pila.compute_subtask_branch("feat-foo-abc123", "feat-001")
            == "pila/subtasks/feat-foo-abc123/feat-001")


def test_compute_subtask_branch_is_pure(pila):
    rid, sid = "fix-bar-def456", "fix-002"
    assert (pila.compute_subtask_branch(rid, sid)
            == pila.compute_subtask_branch(rid, sid))
