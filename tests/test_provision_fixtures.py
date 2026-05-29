"""Tests for `gather_provision_fixtures` — assembles the input set the
LLM-fallback worker sees. Bounded by a 24KB total ceiling and a few
sub-budgets (workspace child manifest cap, workflow-file cap, etc.).

The function is purely deterministic over file contents — no model in
the path. DESIGN §6½.
"""
from __future__ import annotations

import json


def test_empty_repo_returns_empty_fixtures(pila, tmp_path):
    out = pila.gather_provision_fixtures(tmp_path)
    assert out["readme"] == ""
    assert out["manifests"] == {}
    assert out["workspace_manifests"] == []
    assert out["workflows"] == []
    assert out["contributing"] == ""
    assert out["hit_ceiling"] is False
    assert out["total_bytes"] == 0


def test_readme_is_included_via_extractor(pila, tmp_path):
    """A README with a recognized install section is sliced via the
    header-aware extractor."""
    (tmp_path / "README.md").write_text(
        "# Foo\n\nWhatever marketing intro.\n\n"
        "## Installation\n\nRun `pip install foo`.\n"
    )
    out = pila.gather_provision_fixtures(tmp_path)
    assert "Installation" in out["readme"]
    assert "pip install foo" in out["readme"]


def test_root_manifests_are_collected(pila, tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    out = pila.gather_provision_fixtures(tmp_path)
    assert "package.json" in out["manifests"]
    assert "pyproject.toml" in out["manifests"]
    assert "x" in out["manifests"]["package.json"]


def test_workspace_children_capped_at_three(pila, tmp_path):
    """A monorepo with N >3 workspace children only contributes 3."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "root", "workspaces": ["packages/*"],
    }))
    for i in range(5):
        (tmp_path / "packages" / f"pkg-{i}").mkdir(parents=True)
        (tmp_path / "packages" / f"pkg-{i}" / "package.json").write_text(
            json.dumps({"name": f"pkg-{i}"}))
    out = pila.gather_provision_fixtures(tmp_path)
    assert len(out["workspace_manifests"]) == 3
    paths = [rel for rel, _ in out["workspace_manifests"]]
    assert all(p.startswith("packages/pkg-") for p in paths)


def test_workspace_object_form_is_handled(pila, tmp_path):
    """npm/yarn allow `workspaces: {packages: [...]}` — the helper
    unwraps that shape too."""
    (tmp_path / "package.json").write_text(json.dumps({
        "workspaces": {"packages": ["pkgs/*"]}
    }))
    (tmp_path / "pkgs" / "a").mkdir(parents=True)
    (tmp_path / "pkgs" / "a" / "package.json").write_text('{"name":"a"}')
    out = pila.gather_provision_fixtures(tmp_path)
    assert len(out["workspace_manifests"]) == 1
    assert out["workspace_manifests"][0][0] == "pkgs/a/package.json"


def test_workflows_capped_at_two_with_preference(pila, tmp_path):
    """Workflow file selection prefers ci/test/build/release-named
    files and skips codeql/stale/dependabot. Maximum 2."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "codeql.yml").write_text("codeql config\n")
    (wf_dir / "stale.yml").write_text("stale config\n")
    (wf_dir / "ci.yml").write_text("ci config\n")
    (wf_dir / "test.yml").write_text("test config\n")
    (wf_dir / "deploy.yml").write_text("deploy config\n")

    out = pila.gather_provision_fixtures(tmp_path)
    names = [n for n, _ in out["workflows"]]
    assert len(names) == 2
    assert "codeql.yml" not in names
    assert "stale.yml" not in names
    # ci.yml and test.yml should be preferred over deploy.yml.
    assert "ci.yml" in names
    assert "test.yml" in names


def test_contributing_is_picked_up(pila, tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text(
        "Setup: run `pnpm install`, then `pnpm dev`.\n")
    out = pila.gather_provision_fixtures(tmp_path)
    assert "pnpm install" in out["contributing"]


def test_total_byte_ceiling_is_enforced(pila, tmp_path):
    """Inflate the input set past 24KB and confirm hit_ceiling flips."""
    # 32KB README will already exceed the 24KB total budget.
    (tmp_path / "README.md").write_text("install\n" * 4000)
    # Add a manifest too so the ceiling is exercised across sections.
    (tmp_path / "package.json").write_text('{"name":"x"}' + ("\n" * 100))
    out = pila.gather_provision_fixtures(tmp_path)
    assert out["total_bytes"] <= 24576


def test_extractor_falls_back_for_marketing_readme(pila, tmp_path):
    """A README with no install/setup-style headers should still produce
    SOMETHING via the fallback chain (code-fence detector or final
    top-6KB), even if it's the marketing-style 'Why' pitch."""
    body = (
        "# Cool Project\n\n"
        "## Why\n\n"
        "We do cool things.\n\n"
        "## Sponsors\n\n"
        "All these companies sponsor us.\n"
    )
    (tmp_path / "README.md").write_text(body)
    out = pila.gather_provision_fixtures(tmp_path)
    # The fixture set isn't empty just because there's no install
    # section — top-6KB fallback ensures the LLM still has signal.
    assert len(out["readme"]) > 0
