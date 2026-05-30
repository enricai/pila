"""Tests for `_is_auth_or_quota_failure` — the classifier that routes
`claude -p` envelopes into the auth/quota backoff loop in `claude_p()`.

The integration of the backoff loop itself (tenacity's AsyncRetrying
wrapping `_spawn`) is not unit-tested here — per CLAUDE.md the worker
invocation path stays integration-tested. We cover the classifier so
the routing decision is locked down independently.
"""
from __future__ import annotations

import pytest


# --- positives: should classify as auth/quota -----------------------------

@pytest.mark.parametrize("envelope", [
    {"api_error_status": 401, "result": "Failed to authenticate."},
    {"api_error_status": "401", "result": ""},
    {"api_error_status": 429, "result": "Too Many Requests"},
    {"api_error_status": "429", "result": ""},
    {"api_error_status": None,
     "result": "API Error: 401 Invalid authentication credentials"},
    {"api_error_status": None,
     "result": "rate limit exceeded; try again later"},
    {"api_error_status": None,
     "result": "Anthropic returned a rate-limit error."},
    # mixed case — classifier lowercases the result message
    {"api_error_status": None,
     "result": "INVALID AUTHENTICATION provided"},
])
def test_auth_or_quota_envelopes_match(pila, envelope):
    assert pila._is_auth_or_quota_failure(envelope) is True


# --- negatives: should NOT classify as auth/quota -------------------------

@pytest.mark.parametrize("envelope", [
    # plain success envelope
    {"api_error_status": None, "result": "ok",
     "structured_output": {"status": "ready"}},
    # generic error that isn't auth/quota
    {"api_error_status": 500, "result": "Internal server error"},
    {"api_error_status": "500", "result": ""},
    # missing fields entirely
    {},
    # message mentions "auth" but not the specific markers we key on
    {"api_error_status": None, "result": "build failed: unauthorized?"},
    # schema-error class — handled by the existing 2-attempt loop
    {"api_error_status": None,
     "result": "the run produced no structured_output"},
])
def test_non_auth_envelopes_do_not_match(pila, envelope):
    assert pila._is_auth_or_quota_failure(envelope) is False


def test_classifier_tolerates_non_string_result(pila):
    """`result` is normally a string, but `str(None)` is `'None'` — the
    classifier coerces via str() so a missing key never raises."""
    assert pila._is_auth_or_quota_failure({"result": None}) is False


# --- cap is wired into DEFAULT_CAPS ---------------------------------------

def test_auth_retry_max_sec_is_in_default_caps(pila):
    """The backoff budget lives in DEFAULT_CAPS per CLAUDE.md
    'caps are real Python counters' rule."""
    assert "auth_retry_max_sec" in pila.DEFAULT_CAPS
    assert isinstance(pila.DEFAULT_CAPS["auth_retry_max_sec"], int)
    assert pila.DEFAULT_CAPS["auth_retry_max_sec"] > 0
