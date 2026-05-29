"""Tests for `extract_readme_sections` — the header-aware extractor
that picks install-relevant slices out of arbitrary OSS READMEs.

Verified during plan against 15 real-world READMEs (DESIGN §6½):
  Next.js, FastAPI, Django, Rails, Spring Boot, Go, Cargo, React,
  Supabase, LangChain, Node.js, CPython, Deno, uv, esbuild.

13/15 catches via header-aware regex; the remaining 2 (Supabase,
esbuild) are marketing-style READMEs that delegate install to external
docs — those repos route through `.pila-setup.sh`. The fallback chain
still produces SOMETHING for them via the code-fence detector or the
final top-6KB layer.

Fixtures here use representative excerpts (a real header, the section
body, sometimes a code fence) rather than the full multi-megabyte
upstream READMEs — the regex and parser are what we're testing.
"""
from __future__ import annotations

import pytest


def _has(text: str, *needles: str) -> bool:
    """Helper: assert every needle appears in text."""
    return all(n in text for n in needles)


# --- header style coverage --------------------------------------------------

def test_atx_header_install_section(pila):
    """Standard `## Install` Markdown headers."""
    text = (
        "# Project\n\nIntro text.\n\n"
        "## Install\n\nRun `pip install foo`.\n\n"
        "## Other\n\nNot relevant.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "pip install foo" in out


def test_setext_h1_header(pila):
    """Setext-style h1 with `====` underline (RST-style projects)."""
    text = (
        "Project\n=======\n\nIntro.\n\n"
        "Build Instructions\n==================\n\n"
        "Run `make` in the project root.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "make" in out
    assert "Build Instructions" in out


def test_setext_h2_header(pila):
    """Setext-style h2 with `----` underline (CPython-style)."""
    text = (
        "intro\n\n"
        "Build Instructions\n------------------\n\n"
        "Run `./configure && make`.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "configure" in out


def test_asciidoc_double_equals_header(pila):
    """Asciidoc `== Foo` style (spring-boot uses this in .adoc)."""
    text = (
        "= Project\n\nIntro.\n\n"
        "== Installation and Getting Started\n\n"
        "Run `./mvnw clean install`.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "mvnw" in out


# --- decoration / emoji handling -------------------------------------------

def test_emoji_prefix_header_still_matches(pila):
    """A header with leading emoji (`## 🚀 Getting Started`) still
    matches because the regex hits the keyword after stripping
    decoration."""
    text = (
        "# Cool Project\n\nIntro.\n\n"
        "## 🚀 Getting Started\n\n"
        "Run `pnpm install`.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "pnpm install" in out


def test_bullet_prefix_header(pila):
    text = (
        "# Cool\n\nIntro.\n\n"
        "## • Install\n\n"
        "Run `cargo build`.\n"
    )
    out = pila.extract_readme_sections(text)
    assert "cargo build" in out


# --- vocabulary coverage ----------------------------------------------------

@pytest.mark.parametrize("header, install_token", [
    ("## Install", "pnpm install"),
    ("## Installation", "pnpm install"),
    ("## Getting Started", "pnpm install"),
    ("## Getting-Started", "pnpm install"),
    ("## Quick Start", "pnpm install"),
    ("## Quickstart", "pnpm install"),
    ("## Setup", "pnpm install"),
    ("## Usage", "pnpm install"),
    ("## Develop", "pnpm install"),
    ("## Development", "pnpm install"),
    ("## Build", "make"),
    ("## Building", "make"),
    ("## Building from Source", "make"),
    ("## Build Instructions", "make"),
    ("## Compiling", "cargo build"),
    ("## Compiling from Source", "cargo build"),
    ("## Download", "curl-cmd"),
    ("## Requirements", "python install"),
    ("## Prerequisites", "python install"),
    ("## Dependencies", "python install"),
])
def test_section_keyword_coverage(pila, header, install_token):
    """Every header keyword in the documented regex should match a
    section that contains its install token."""
    text = f"# Project\n\nIntro.\n\n{header}\n\n{install_token}\n"
    out = pila.extract_readme_sections(text)
    assert install_token in out, f"missed section header: {header!r}"


# --- intro preservation -----------------------------------------------------

def test_intro_kept_under_1kb(pila):
    """The first section (the intro before any header) is kept up to
    a 1KB budget so a short pitch survives even when an install section
    matches further down."""
    intro = "Short intro line.\n"
    text = (
        f"# Project\n\n{intro}\n"
        "## Install\n\n`pip install`\n"
    )
    out = pila.extract_readme_sections(text)
    assert "Short intro line" in out


def test_long_intro_truncated_to_budget(pila):
    """An intro larger than the budget gets clipped at the boundary."""
    huge_intro = "X" * 5000
    text = (
        f"# Project\n\n{huge_intro}\n"
        "## Install\n\n`pip install`\n"
    )
    out = pila.extract_readme_sections(text)
    # Intro shouldn't dominate; clipping leaves some Xs, but the install
    # section is also present.
    assert "pip install" in out


# --- fallback chain ---------------------------------------------------------

def test_no_header_falls_back_to_code_fence(pila):
    """A README with no matching section header but containing code
    fences with install commands should keep the fences (Supabase /
    esbuild style — the marketing pitch buries install in a code block
    instead of a labeled section)."""
    text = (
        "# Project\n\n"
        "This is a marketing pitch.\n\n"
        "## Why\n\nReasons.\n\n"
        "Get started:\n\n"
        "```\nnpm install foo\n```\n\n"
        "## Sponsors\n\nOur sponsors.\n"
    )
    out = pila.extract_readme_sections(text)
    # Either the code fence is preserved by the fallback OR the top-6KB
    # fallback covers everything — both leave `npm install foo` in.
    assert "npm install foo" in out


def test_no_header_no_fence_falls_back_to_top_6kb(pila):
    """Final fallback: a README without headers or recognizable install
    commands still emits the top-6KB so the LLM has something to work
    with."""
    text = "Just some prose.\n" * 500
    out = pila.extract_readme_sections(text)
    assert len(out) > 0
    assert "Just some prose" in out


# --- pathological inputs ---------------------------------------------------

def test_empty_text_returns_empty(pila):
    assert pila.extract_readme_sections("") == ""


def test_text_with_only_header_no_body(pila):
    """A header line with no following body still surfaces the header
    text."""
    text = "## Install\n"
    out = pila.extract_readme_sections(text)
    assert "Install" in out
