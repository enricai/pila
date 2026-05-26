"""Tests for the per-run State refactor — `State.__init__` taking
(centella_root, run_id), and `State.rename_to()` for the bootstrap →
final-run-id rename.

Covers:
- State paths are correctly anchored under runs/<run-id>/.
- Two State instances with different run_ids have disjoint storage.
- save() writes to the right path; load() reads from the right path.
- rename_to() atomically moves the run dir, updates path/run_dir/run_id.
- rename_to() dies on collision (target dir exists).
"""
from __future__ import annotations

import pytest


def test_state_path_is_per_run(centella, tmp_path):
    """state.json lives at centella_root/runs/<run-id>/state.json."""
    st = centella.State(tmp_path, "feat-foo-abc123")
    assert st.path == tmp_path / "runs" / "feat-foo-abc123" / "state.json"
    assert st.run_dir == tmp_path / "runs" / "feat-foo-abc123"
    assert st.run_id == "feat-foo-abc123"
    assert st.centella_root == tmp_path


def test_state_save_writes_to_per_run_dir(centella, tmp_path):
    """save() writes the JSON under runs/<run-id>/."""
    (tmp_path / "runs" / "feat-foo-abc123").mkdir(parents=True)
    st = centella.State(tmp_path, "feat-foo-abc123")
    st.data = {"task": "x"}
    st.save()
    saved = tmp_path / "runs" / "feat-foo-abc123" / "state.json"
    assert saved.exists()


def test_state_load_reads_from_per_run_dir(centella, tmp_path):
    import json
    rd = tmp_path / "runs" / "feat-foo-abc123"
    rd.mkdir(parents=True)
    (rd / "state.json").write_text(json.dumps({"task": "loaded"}))
    st = centella.State(tmp_path, "feat-foo-abc123")
    assert st.load() is True
    assert st.data["task"] == "loaded"


def test_state_load_returns_false_when_absent(centella, tmp_path):
    """Per-run dir without a state.json (fresh bootstrap) loads to False
    rather than raising."""
    (tmp_path / "runs" / "feat-foo-abc123").mkdir(parents=True)
    st = centella.State(tmp_path, "feat-foo-abc123")
    assert st.load() is False


def test_two_states_disjoint_paths(centella, tmp_path):
    """Two State instances with different run_ids have completely
    disjoint storage — the central property the per-run refactor must
    guarantee."""
    sa = centella.State(tmp_path, "feat-a-aaaaaa")
    sb = centella.State(tmp_path, "fix-b-bbbbbb")
    assert sa.path != sb.path
    assert sa.run_dir != sb.run_dir
    # The directories must not share any path component below
    # centella_root — they're siblings.
    assert sa.run_dir.parent == sb.run_dir.parent == tmp_path / "runs"


def test_two_states_save_independently(centella, tmp_path):
    """Save one State; the other's path stays empty."""
    (tmp_path / "runs" / "feat-a-aaaaaa").mkdir(parents=True)
    (tmp_path / "runs" / "fix-b-bbbbbb").mkdir(parents=True)
    sa = centella.State(tmp_path, "feat-a-aaaaaa")
    sb = centella.State(tmp_path, "fix-b-bbbbbb")
    sa.data = {"task": "a"}
    sa.save()
    assert sa.path.exists()
    assert not sb.path.exists()


# --- rename_to ------------------------------------------------------------

def test_rename_to_moves_run_dir(centella, tmp_path):
    """rename_to() moves the on-disk run dir and updates State's
    attributes to point at the new location."""
    (tmp_path / "runs" / "_bootstrap-abcdef").mkdir(parents=True)
    st = centella.State(tmp_path, "_bootstrap-abcdef")
    st.data = {"task": "x"}
    st.save()
    bootstrap_path = st.path
    assert bootstrap_path.exists()

    st.rename_to("feat-final-xyz999")

    # State attributes now point at the new dir.
    assert st.run_id == "feat-final-xyz999"
    assert st.run_dir == tmp_path / "runs" / "feat-final-xyz999"
    assert st.path == tmp_path / "runs" / "feat-final-xyz999" / "state.json"
    # On-disk: old path is gone, new path exists.
    assert not bootstrap_path.exists()
    assert st.path.exists()


def test_rename_to_preserves_state_data(centella, tmp_path):
    """state.json content survives the rename — it's just a directory
    rename, not a new write."""
    import json
    (tmp_path / "runs" / "_bootstrap-abcdef").mkdir(parents=True)
    st = centella.State(tmp_path, "_bootstrap-abcdef")
    st.data = {"task": "must-survive", "categories": ["feature-implementation"]}
    st.save()

    st.rename_to("feat-final-xyz999")
    # Read directly from the new path to confirm contents survived.
    loaded = json.loads(st.path.read_text())
    assert loaded["task"] == "must-survive"
    assert loaded["categories"] == ["feature-implementation"]


def test_rename_to_dies_on_collision(centella, tmp_path):
    """If the target directory already exists, rename_to() dies rather
    than silently overwriting. The collision is extraordinarily unlikely
    (microsecond-precision sha1) but caught as a hard error."""
    (tmp_path / "runs" / "_bootstrap-abcdef").mkdir(parents=True)
    (tmp_path / "runs" / "feat-existing-xyz999").mkdir(parents=True)
    st = centella.State(tmp_path, "_bootstrap-abcdef")
    with pytest.raises(SystemExit):
        st.rename_to("feat-existing-xyz999")


def test_rename_to_preserves_sub_directories(centella, tmp_path):
    """Subdirectories under the run dir (criteria/, logs/, etc.) move
    with the rename — they're inside the dir being renamed."""
    rd = tmp_path / "runs" / "_bootstrap-abcdef"
    rd.mkdir(parents=True)
    (rd / "criteria").mkdir()
    (rd / "criteria" / "feat-001.md").write_text("# stuff")
    (rd / "logs").mkdir()
    (rd / "logs" / "classifier.log").write_text("event 1\n")
    st = centella.State(tmp_path, "_bootstrap-abcdef")
    st.data = {"task": "x"}
    st.save()

    st.rename_to("feat-final-xyz999")

    new_rd = tmp_path / "runs" / "feat-final-xyz999"
    assert (new_rd / "criteria" / "feat-001.md").exists()
    assert (new_rd / "logs" / "classifier.log").exists()
    assert (new_rd / "logs" / "classifier.log").read_text() == "event 1\n"
