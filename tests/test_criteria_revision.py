"""Tests for the proposal-only criteria revision channel (DESIGN §9).

Covers _proposal_structurally_valid (the orchestrator's
structural-minimum check), apply_criteria_revision (writes file + updates
lock), and record_criteria_revision (append-only audit log).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def setup(centella, tmp_path):
    """A tmp per-run dir with criteria/sid.md present and a worktree dir
    containing a real file the evidence can cite. State writes to
    `.centella/runs/<run-id>/state.json` under the new layout; tests
    treat `centella_dir` as the per-run dir."""
    centella_root = tmp_path / ".centella"
    run_id = "test-run-aaa111"
    centella_dir = centella_root / "runs" / run_id
    (centella_dir / "criteria").mkdir(parents=True)
    sid = "feat-x"
    (centella_dir / "criteria" / f"{sid}.md").write_text(
        "# Criteria\n- [ ] tests pass\n")
    worktree = tmp_path / "worktrees" / sid
    worktree.mkdir(parents=True)
    (worktree / "src.py").write_text("def f(): pass\n")
    st = centella.State(centella_root, run_id)
    st.data = {"task": "test"}
    return centella, centella_dir, st, sid, str(worktree)


# --- _proposal_structurally_valid ----------------------------------------

def test_empty_proposed_text_rejected(setup):
    c, _, _, _, wt = setup
    err = c._proposal_structurally_valid(
        {"proposed_text": "  ", "evidence": "src.py:1 has a bug"}, wt)
    assert err is not None
    assert "proposed_text" in err


def test_empty_evidence_rejected(setup):
    c, _, _, _, wt = setup
    err = c._proposal_structurally_valid(
        {"proposed_text": "# New criteria\n", "evidence": "  "}, wt)
    assert err is not None
    assert "evidence" in err


def test_evidence_with_no_real_path_rejected(setup):
    c, _, _, _, wt = setup
    err = c._proposal_structurally_valid(
        {"proposed_text": "# New criteria\n",
         "evidence": "the criteria are vibes-based, trust me"}, wt)
    assert err is not None
    assert "no path that exists" in err


def test_evidence_with_real_path_accepted(setup):
    c, _, _, _, wt = setup
    err = c._proposal_structurally_valid(
        {"proposed_text": "# New criteria\n- [ ] X\n",
         "evidence": "see src.py:1 — the original criteria don't cover it"}, wt)
    assert err is None


# --- apply_criteria_revision ---------------------------------------------

def test_apply_overwrites_file_and_updates_lock(setup):
    c, cdir, st, sid, _ = setup
    # seed the lock as if lock_criteria had been called on the original
    c.lock_criteria(sid, cdir, st)
    original_hash = st.data["criteria_locks"][sid]

    new_text = "# Criteria v2\n- [ ] new check\n"
    old_hash, new_hash = c.apply_criteria_revision(sid, cdir, st, new_text)

    assert old_hash == original_hash
    assert new_hash != old_hash
    # file on disk reflects the new text
    assert (cdir / "criteria" / f"{sid}.md").read_text() == new_text
    # lock was updated to the new hash so verify_criteria_lock won't fire
    assert st.data["criteria_locks"][sid] == new_hash
    # verify_criteria_lock now passes (does not raise)
    c.verify_criteria_lock(sid, cdir, st)


# --- record_criteria_revision --------------------------------------------

def test_record_appends_approved_entry(setup):
    c, _, st, sid, _ = setup
    c.record_criteria_revision(sid, st, "src.py:5 is the issue",
                                "approved", "abc12345", "def67890")
    revs = st.data["criteria_revisions"]
    assert len(revs) == 1
    assert revs[0]["sid"] == sid
    assert revs[0]["status"] == "approved"
    assert revs[0]["old_hash"] == "abc12345"
    assert revs[0]["new_hash"] == "def67890"
    assert "rejection_reason" not in revs[0]


def test_record_appends_rejected_entry(setup):
    c, _, st, sid, _ = setup
    c.record_criteria_revision(sid, st, "trust me", "rejected", None, None,
                                rejection_reason="evidence is empty")
    revs = st.data["criteria_revisions"]
    assert len(revs) == 1
    assert revs[0]["status"] == "rejected"
    assert revs[0]["rejection_reason"] == "evidence is empty"
    assert "old_hash" not in revs[0]
    assert "new_hash" not in revs[0]


def test_record_is_append_only(setup):
    c, _, st, sid, _ = setup
    c.record_criteria_revision(sid, st, "e1", "rejected", None, None,
                                rejection_reason="r1")
    c.record_criteria_revision(sid, st, "e2", "approved", "h1", "h2")
    assert len(st.data["criteria_revisions"]) == 2
    assert st.data["criteria_revisions"][0]["evidence"] == "e1"
    assert st.data["criteria_revisions"][1]["evidence"] == "e2"
