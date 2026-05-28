"""Tests for `detect_session_limit()` — the Claude Code subscription
session-limit / rate-limit message detector.

The detector is the single load-bearing surface for the rate-limit
auto-resume contract (DESIGN §6 *Cleanup on abnormal exit*): if it
returns a `RateLimitedExit` with a parseable `reset_at`, main() will
sleep until that moment and `os.execvp` into `--resume`. A wrong
parse here would produce a wrong-time sleep — strictly worse than no
auto-resume — so the detector must be conservative: only return a
non-None `reset_at` when every step of the parse (regex match,
integer conversion, AM/PM normalization, ZoneInfo lookup, range
checks) succeeds.

Empirical anchor: the verbatim message text observed identical across
three independent runs on 2026-05-27 is:

    "You've hit your session limit · resets 3:10am (America/Bogota)"
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# The verbatim observed message — the load-bearing test anchor.
VERBATIM = "You've hit your session limit · resets 3:10am (America/Bogota)"


# --- positive cases -------------------------------------------------------

def test_verbatim_message_returns_exit_with_parsed_reset_at(pila):
    exc = pila.detect_session_limit(VERBATIM)
    assert exc is not None
    assert exc.raw_message == VERBATIM
    assert exc.reset_at is not None
    # 3:10am in America/Bogota — assertable independent of "now"
    assert exc.reset_at.hour == 3
    assert exc.reset_at.minute == 10
    assert exc.reset_at.tzinfo.key == "America/Bogota"


def test_pm_time_normalizes_correctly(pila):
    text = "You've hit your session limit · resets 7:30pm (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at.hour == 19
    assert exc.reset_at.minute == 30


def test_midnight_12am_normalizes_to_hour_zero(pila):
    text = "You've hit your session limit · resets 12:00am (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at.hour == 0


def test_noon_12pm_stays_hour_twelve(pila):
    text = "You've hit your session limit · resets 12:00pm (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at.hour == 12


def test_case_insensitive_prefix(pila):
    text = "YOU'VE HIT YOUR SESSION LIMIT · resets 3:10am (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None


def test_case_insensitive_ampm(pila):
    text = "You've hit your session limit · resets 3:10AM (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at.hour == 3


def test_reset_time_in_past_rolls_to_tomorrow(pila, monkeypatch):
    """If the parsed reset time is earlier than now (or equal), it's
    tomorrow. Without this the auto-resume would sleep for a negative
    duration and skip entirely."""
    tz = ZoneInfo("UTC")
    fixed_now = datetime(2026, 5, 27, 23, 0, 0, tzinfo=tz)

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)
    monkeypatch.setattr(pila, "datetime", _FrozenDateTime)

    text = "You've hit your session limit · resets 1:00am (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at is not None
    # Tomorrow's 1am, not today's
    assert exc.reset_at.date() == (fixed_now + timedelta(days=1)).date()
    assert exc.reset_at.hour == 1


# --- negative cases — must NOT match --------------------------------------

def test_empty_text_returns_none(pila):
    assert pila.detect_session_limit("") is None


def test_unrelated_text_returns_none(pila):
    assert pila.detect_session_limit("The user asked a question.") is None


def test_workers_discussing_rate_limit_code_returns_none(pila):
    """The barnacle false-positive case I found in worker logs — a
    legitimate assistant text discussing rate-limit handling in code.
    Must NOT match. The detector's load-bearing requirement is that
    broader 'rate-limit' / 'rate-limited' patterns are NOT used; only
    the literal Claude Code marketing-copy prefix counts."""
    text = ('Now let me also downgrade the duplicate pino log lines to '
            'debug. `logger.warn` at line ~211: "hot path rate-limited '
            'for ${siteId}... not falling back" — this duplicates the '
            'event content.')
    assert pila.detect_session_limit(text) is None


def test_general_rate_limit_mention_returns_none(pila):
    text = "The API was rate-limited, but we caught the 429 and retried."
    assert pila.detect_session_limit(text) is None


# --- parse-failure cases — must match but with reset_at=None --------------

def test_unknown_timezone_returns_exit_with_none_reset(pila):
    """An unparseable timezone name must produce a clean fallback to
    manual --resume, not a wrong-time sleep."""
    text = "You've hit your session limit · resets 3:10am (Mars/Olympus)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at is None


def test_no_reset_clause_returns_exit_with_none_reset(pila):
    """The prefix matches but there's no `resets ...` clause."""
    text = "You've hit your session limit — please try again later."
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at is None


def test_malformed_time_returns_exit_with_none_reset(pila):
    """Hour out of range (25:xx) must fall back to None, not crash."""
    text = "You've hit your session limit · resets 25:00am (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at is None


def test_malformed_minute_returns_exit_with_none_reset(pila):
    text = "You've hit your session limit · resets 3:99am (UTC)"
    exc = pila.detect_session_limit(text)
    assert exc is not None
    assert exc.reset_at is None


# --- exception shape ------------------------------------------------------

def test_rate_limited_exit_is_baseexception(pila):
    """Must subclass BaseException (not Exception) so the broad
    `except Exception` handlers inside orchestrate() don't swallow it
    — same pattern as InterruptedBySignal."""
    assert issubclass(pila.RateLimitedExit, BaseException)
    assert not issubclass(pila.RateLimitedExit, Exception)


def test_rate_limited_exit_carries_fields(pila):
    """The exit's `reset_at` and `raw_message` are how main()'s
    handler decides between auto-resume and manual-resume — they
    must be set as attributes, not just constructor args."""
    exc = pila.RateLimitedExit(reset_at=None, raw_message="hi")
    assert exc.reset_at is None
    assert exc.raw_message == "hi"
    assert str(exc) == "hi"


# --- main() arm integration ------------------------------------------------

def test_main_has_rate_limited_exit_arm():
    """Pin that main() catches RateLimitedExit. Without this arm the
    exception would fall through to the catch-all BaseException
    handler and the auto-resume path wouldn't run."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "orchestrator" / "pila.py").read_text()
    assert "except RateLimitedExit" in src


def test_main_rate_limit_arm_appears_before_keyboard_interrupt():
    """except RateLimitedExit must be matched BEFORE except
    KeyboardInterrupt — both inherit BaseException, but the more
    specific one needs to come first or it never fires."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "orchestrator" / "pila.py").read_text()
    rl_pos = src.find("except RateLimitedExit")
    ki_pos = src.find("except KeyboardInterrupt")
    assert rl_pos != -1
    assert ki_pos != -1
    assert rl_pos < ki_pos
