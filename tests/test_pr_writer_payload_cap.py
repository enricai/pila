"""Tests for `_cap_text()` — the byte-budgeted truncator used to keep
the pr_writer worker's user_prompt under Linux ARG_MAX (~128 KB on
the pila container).

Critical properties:
- Strings under the cap pass through unchanged.
- Strings over the cap get a single-line in-band sentinel marker.
- Multi-byte UTF-8 is never split mid-codepoint (decoding the result
  must succeed without errors).
- Empty / falsy inputs short-circuit.
"""
from __future__ import annotations


def test_cap_text_short_input_unchanged(pila):
    text = "small commit log\nwith two lines\n"
    out, truncated = pila._cap_text(text, max_bytes=10_000, label="commit log")
    assert out == text
    assert truncated is False


def test_cap_text_at_exact_boundary_unchanged(pila):
    text = "x" * 100
    out, truncated = pila._cap_text(text, max_bytes=100, label="commit log")
    assert out == text
    assert truncated is False


def test_cap_text_over_boundary_truncates_with_sentinel(pila):
    text = "abcdefghij" * 100  # 1000 bytes
    out, truncated = pila._cap_text(text, max_bytes=200, label="commit log")
    assert truncated is True
    assert len(out.encode("utf-8")) < len(text.encode("utf-8"))
    # Sentinel must mention the label and the truncation
    assert "commit log truncated" in out
    assert "remainder omitted" in out


def test_cap_text_preserves_utf8_codepoints(pila):
    # 4-byte codepoint (rocket emoji is U+1F680, encoded as 4 bytes in UTF-8)
    # Pack lots of them so the cap *must* land mid-codepoint when naively
    # sliced; our helper backs off to a safe boundary.
    text = "🚀" * 100  # 400 bytes total
    # max_bytes=51 would naively land 3 bytes into the 13th rocket
    out, truncated = pila._cap_text(text, max_bytes=51, label="diff")
    assert truncated is True
    # Strip the sentinel and confirm the kept prefix is valid UTF-8
    kept = out.split("\n... [")[0]
    # Must round-trip through UTF-8 without raising
    kept.encode("utf-8").decode("utf-8")
    # And contain only complete rocket emojis (no replacement chars)
    assert "�" not in kept


def test_cap_text_empty_string_short_circuit(pila):
    out, truncated = pila._cap_text("", max_bytes=100, label="x")
    assert out == ""
    assert truncated is False


def test_cap_text_label_appears_in_sentinel(pila):
    text = "x" * 5000
    out, _ = pila._cap_text(text, max_bytes=100, label="PR template")
    assert "PR template truncated" in out


def test_cap_text_sentinel_size_in_kb(pila):
    # Sentinel reports approximate KB so a 80_000-byte cap reports ~80 KB
    text = "x" * 200_000
    out, _ = pila._cap_text(text, max_bytes=80_000, label="commit log")
    assert "~80 KB" in out


def test_pr_writer_byte_budgets_defined(pila):
    """Pin the byte budgets so accidentally bumping them up past
    ARG_MAX (~128 KB on Debian containers) gets caught in code review.
    The sum of all caps + JSON overhead + the system prompt must stay
    under ~128 KB."""
    assert pila.PR_WRITER_COMMIT_LOG_MAX_BYTES == 80_000
    assert pila.PR_WRITER_TEMPLATE_MAX_BYTES == 32_000
    assert pila.PR_WRITER_DIFF_SAMPLE_MAX_LINES == 500
    total = (pila.PR_WRITER_COMMIT_LOG_MAX_BYTES
             + pila.PR_WRITER_TEMPLATE_MAX_BYTES)
    assert total < 128_000, (
        f"large payload fields sum to {total} bytes — too close to "
        f"Linux ARG_MAX (128 KB). Reduce one of the caps."
    )


def test_truncate_diff_sample_short_passthrough(pila):
    text = "line1\nline2\nline3"
    out, truncated = pila._truncate_diff_sample(text, max_lines=10)
    assert out == text
    assert truncated is False


def test_truncate_diff_sample_truncates_at_limit(pila):
    text = "\n".join(f"line{i}" for i in range(200))
    out, truncated = pila._truncate_diff_sample(text, max_lines=50)
    assert truncated is True
    lines = out.split("\n")
    # 50 original lines + 1 sentinel
    assert len(lines) == 51
    assert "diff sample truncated at 50 lines" in lines[-1]
