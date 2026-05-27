"""Asserts that the judge-llm-batch and llm-self-heal SKILL.md files exist
with the correct frontmatter name slugs."""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).parent.parent


def _parse_frontmatter_name(path: Path) -> str:
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing opening ---")
    end = text.index("---", 3)
    block = text[3:end]
    for line in block.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise ValueError(f"{path}: no name: field in frontmatter")


def test_judge_llm_batch_skill_exists():
    path = _REPO_ROOT / "skills" / "judge-llm-batch" / "SKILL.md"
    assert path.is_file(), f"Missing: {path}"


def test_llm_self_heal_skill_exists():
    path = _REPO_ROOT / "skills" / "llm-self-heal" / "SKILL.md"
    assert path.is_file(), f"Missing: {path}"


def test_judge_llm_batch_frontmatter_name():
    path = _REPO_ROOT / "skills" / "judge-llm-batch" / "SKILL.md"
    assert _parse_frontmatter_name(path) == "judge-llm-batch"


def test_llm_self_heal_frontmatter_name():
    path = _REPO_ROOT / "skills" / "llm-self-heal" / "SKILL.md"
    assert _parse_frontmatter_name(path) == "llm-self-heal"


def test_judge_llm_batch_has_nonempty_body():
    path = _REPO_ROOT / "skills" / "judge-llm-batch" / "SKILL.md"
    text = path.read_text()
    # find the closing --- of frontmatter
    second_dash = text.index("---", 3)
    body = text[second_dash + 3:].strip()
    assert body, "skills/judge-llm-batch/SKILL.md has an empty body"


def test_llm_self_heal_has_nonempty_body():
    path = _REPO_ROOT / "skills" / "llm-self-heal" / "SKILL.md"
    text = path.read_text()
    second_dash = text.index("---", 3)
    body = text[second_dash + 3:].strip()
    assert body, "skills/llm-self-heal/SKILL.md has an empty body"


def test_judge_llm_batch_has_description():
    path = _REPO_ROOT / "skills" / "judge-llm-batch" / "SKILL.md"
    text = path.read_text()
    end = text.index("---", 3)
    block = text[3:end]
    has_desc = any(
        line.lstrip().startswith("description:") for line in block.splitlines()
    )
    assert has_desc, "skills/judge-llm-batch/SKILL.md frontmatter missing description:"


def test_llm_self_heal_has_description():
    path = _REPO_ROOT / "skills" / "llm-self-heal" / "SKILL.md"
    text = path.read_text()
    end = text.index("---", 3)
    block = text[3:end]
    has_desc = any(
        line.lstrip().startswith("description:") for line in block.splitlines()
    )
    assert has_desc, "skills/llm-self-heal/SKILL.md frontmatter missing description:"
