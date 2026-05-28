"""Tests for _infer_build_lint_test() — best-effort discovery of the
repo's build/lint/test commands. The conformer (DESIGN §9 *Post-work
conformance*) is told these inferred commands as a starting point;
inference doesn't have to be exhaustive, but it must cover the common
package-manager families.
"""
from __future__ import annotations


def _infer(pila, tmp_path):
    return pila._infer_build_lint_test(tmp_path)


def test_empty_repo_returns_all_empty(pila, tmp_path):
    blt = _infer(pila, tmp_path)
    assert blt == {"build": "", "lint": "", "test": ""}


def test_pyproject_only_infers_pytest(pila, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool]\nname='x'\n")
    blt = _infer(pila, tmp_path)
    assert blt["test"] == "pytest"
    assert blt["build"] == ""
    assert blt["lint"] == ""


def test_package_json_infers_npm_build_and_test(pila, tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}')
    blt = _infer(pila, tmp_path)
    assert blt["build"] == "npm run build"
    assert blt["test"] == "npm test"


def test_cargo_infers_cargo_commands(pila, tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    blt = _infer(pila, tmp_path)
    assert blt["build"] == "cargo build"
    assert blt["test"] == "cargo test"


def test_go_mod_infers_go_commands(pila, tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    blt = _infer(pila, tmp_path)
    assert blt["build"] == "go build ./..."
    assert blt["test"] == "go test ./..."


def test_eslintrc_classic_infers_eslint(pila, tmp_path):
    (tmp_path / ".eslintrc").write_text("{}")
    blt = _infer(pila, tmp_path)
    assert blt["lint"] == "npx eslint ."


def test_eslintrc_json_infers_eslint(pila, tmp_path):
    (tmp_path / ".eslintrc.json").write_text("{}")
    assert _infer(pila, tmp_path)["lint"] == "npx eslint ."


def test_eslintrc_js_infers_eslint(pila, tmp_path):
    (tmp_path / ".eslintrc.js").write_text("module.exports = {};")
    assert _infer(pila, tmp_path)["lint"] == "npx eslint ."


def test_eslintrc_cjs_infers_eslint(pila, tmp_path):
    """Third-pass audit follow-up — .eslintrc.cjs was missed by the
    original allowlist; many modern Node projects use it."""
    (tmp_path / ".eslintrc.cjs").write_text("module.exports = {};")
    assert _infer(pila, tmp_path)["lint"] == "npx eslint ."


def test_eslintrc_yaml_infers_eslint(pila, tmp_path):
    """Third-pass audit follow-up — .eslintrc.yaml variant."""
    (tmp_path / ".eslintrc.yaml").write_text("env:\n  node: true\n")
    assert _infer(pila, tmp_path)["lint"] == "npx eslint ."


def test_eslintrc_yml_infers_eslint(pila, tmp_path):
    """Third-pass audit follow-up — .eslintrc.yml variant."""
    (tmp_path / ".eslintrc.yml").write_text("env:\n  node: true\n")
    assert _infer(pila, tmp_path)["lint"] == "npx eslint ."


def test_ruff_toml_infers_ruff(pila, tmp_path):
    (tmp_path / "ruff.toml").write_text("line-length = 100\n")
    assert _infer(pila, tmp_path)["lint"] == "ruff check ."


def test_polyglot_node_python_picks_npm_build(pila, tmp_path):
    """When both package.json and pyproject.toml exist, build should be
    populated (npm wins because it's checked first); test gets a value
    too — npm test is set by package.json, not overridden by pyproject."""
    (tmp_path / "package.json").write_text('{"name":"x"}')
    (tmp_path / "pyproject.toml").write_text("[tool]\nname='x'\n")
    blt = _infer(pila, tmp_path)
    assert blt["build"] == "npm run build"
    # `out["test"] or "..."` short-circuits — npm test (from package.json,
    # checked first) wins over pytest.
    assert blt["test"] == "npm test"


def test_makefile_infers_make(pila, tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\techo ok\n")
    assert _infer(pila, tmp_path)["build"] == "make"
