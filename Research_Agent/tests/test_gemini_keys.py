"""Gemini multi-key failover -- pure logic, no network and no real keys.

Covers the bug this was built to avoid: a single exhausted GEMINI_API_KEY
failing the whole discovery/enrichment/qualify run, when a second key in
.env still has quota. See src/csg/discovery/gemini_keys.py.
"""

from __future__ import annotations

import pytest

from csg.discovery.gemini_keys import GeminiKeyPool, is_daily_quota_exhausted


class _FakeError(Exception):
    """Stands in for google.genai.errors.APIError / the raw ClientError this
    SDK actually raises -- both expose message text; the real APIError also
    exposes .code/.details, which some tests attach directly."""


def _daily_quota_error() -> Exception:
    # The exact shape observed live: a plain ClientError whose message embeds
    # the JSON body, with no structured .details attribute.
    return _FakeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
        "'You exceeded your current quota, please check your plan and "
        "billing details.', 'status': 'RESOURCE_EXHAUSTED'}}"
    )


def _structured_daily_quota_error() -> Exception:
    exc = _FakeError("429 quota")
    exc.code = 429
    exc.details = {
        "error": {"details": [{"violations": [{"quotaId": "GenerateContentPerDayPerProjectPerModel"}]}]}
    }
    return exc


def _structured_per_minute_error() -> Exception:
    exc = _FakeError("429 rate limit")
    exc.code = 429
    exc.details = {
        "error": {"details": [{"violations": [{"quotaId": "GenerateContentPerMinutePerProjectPerModel"}]}]}
    }
    return exc


def _live_daily_quota_error() -> Exception:
    """The exact shape google.genai.errors.APIError.details takes for a real
    daily-quota 429: no QuotaFailure/violations block at all, just a generic
    rpc.Help link. Captured from a live failed run -- see the bug this
    guards: is_daily_quota_exhausted() used to read an empty-but-truthy
    error.details list as "structured details present, no PerDay violation,
    therefore per-minute" and refuse to rotate keys, so a real daily-quota
    error never triggered failover."""
    exc = _FakeError("429 RESOURCE_EXHAUSTED. ...")
    exc.code = 429
    exc.details = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check your "
                       "plan and billing details.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.Help",
                "links": [{"description": "Learn more about Gemini API quotas",
                          "url": "https://ai.google.dev/gemini-api/docs/rate-limits"}],
            }],
        }
    }
    return exc


def _network_error() -> Exception:
    return _FakeError("ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]")


def _server_error() -> Exception:
    exc = _FakeError("500 internal error")
    exc.code = 500
    return exc


# --- is_daily_quota_exhausted ---------------------------------------------

def test_recognizes_the_live_error_shape():
    """The exact ClientError text this SDK raised in a real quota-exhausted run."""
    assert is_daily_quota_exhausted(_daily_quota_error())


def test_recognizes_structured_per_day_violation():
    assert is_daily_quota_exhausted(_structured_daily_quota_error())


def test_does_not_flag_structured_per_minute_violation():
    """A per-minute cap recovers in seconds -- must not burn through the key
    pool for something the caller's own backoff already handles."""
    assert not is_daily_quota_exhausted(_structured_per_minute_error())


def test_recognizes_live_daily_quota_error_with_no_violations_block():
    """Regression: a real 429 whose error.details is just a generic
    rpc.Help link (no QuotaFailure/violations) was misread as "structured
    details present, no PerDay violation -> must be per-minute" and never
    rotated to the next key. This is the actual shape Gemini returned in a
    live quota-exhausted discovery run."""
    assert is_daily_quota_exhausted(_live_daily_quota_error())


def test_does_not_flag_network_errors():
    assert not is_daily_quota_exhausted(_network_error())


def test_does_not_flag_server_errors():
    assert not is_daily_quota_exhausted(_server_error())


# --- GeminiKeyPool ----------------------------------------------------------

def test_from_env_discovers_numbered_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key-one")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-two")
    monkeypatch.setenv("GEMINI_API_KEY_3", "key-three")
    pool = GeminiKeyPool.from_env()
    assert pool.keys == ["key-one", "key-two", "key-three"]


def test_from_env_handles_single_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "only-key")
    pool = GeminiKeyPool.from_env()
    assert pool.keys == ["only-key"]


def test_from_env_empty_when_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    pool = GeminiKeyPool.from_env()
    assert not pool
    assert len(pool) == 0


def test_from_env_drops_duplicate_keys(monkeypatch):
    """Pointing two vars at the same key must not double-count it in attempts."""
    monkeypatch.setenv("GEMINI_API_KEY", "same-key")
    monkeypatch.setenv("GEMINI_API_KEY_2", "same-key")
    pool = GeminiKeyPool.from_env()
    assert pool.keys == ["same-key"]


def test_attempts_yields_keys_in_order():
    pool = GeminiKeyPool(keys=["a", "b", "c"])
    attempts = list(pool.attempts())
    assert [a.key for a in attempts] == ["a", "b", "c"]
    assert [a.index for a in attempts] == [1, 2, 3]
    assert all(a.total == 3 for a in attempts)


def test_mark_exhausted_skips_key_on_next_attempts_call():
    """Simulates the real failover loop: first pass finds key 'a' exhausted,
    marks it, and a retry (e.g. the next prompt in a discovery run) must not
    waste a call on it again."""
    pool = GeminiKeyPool(keys=["a", "b"])
    first_pass = list(pool.attempts())
    assert [a.key for a in first_pass] == ["a", "b"]

    pool.mark_exhausted("a")
    second_pass = list(pool.attempts())
    assert [a.key for a in second_pass] == ["b"]


def test_should_advance_true_for_daily_quota():
    pool = GeminiKeyPool(keys=["a"])
    assert pool.should_advance(_daily_quota_error())


def test_should_advance_false_for_real_errors():
    """A genuine error (bad request, auth failure, etc.) must propagate
    instead of being swallowed as if it were exhausted quota."""
    pool = GeminiKeyPool(keys=["a"])
    assert not pool.should_advance(_network_error())
    assert not pool.should_advance(_server_error())


def test_exhausted_message_mentions_key_count():
    pool = GeminiKeyPool(keys=["a", "b"])
    msg = pool.exhausted_message()
    assert "2" in msg
    assert "GEMINI_API_KEY_3" in msg  # tells the user which var to add next


def test_exhausted_message_when_no_keys_configured():
    pool = GeminiKeyPool(keys=[])
    msg = pool.exhausted_message()
    assert "not set" in msg.lower()


def test_full_failover_simulation():
    """End-to-end of the actual loop shape used in web_search.py/research.py/
    qualify.py: iterate attempts, mark exhausted on quota error, continue to
    the next key, succeed on the second."""
    pool = GeminiKeyPool(keys=["exhausted-key", "working-key"])
    succeeded_with = None

    for attempt in pool.attempts():
        if attempt.key == "exhausted-key":
            exc = _daily_quota_error()
            assert pool.should_advance(exc)
            pool.mark_exhausted(attempt.key)
            continue
        succeeded_with = attempt.key
        break

    assert succeeded_with == "working-key"


def test_all_keys_exhausted_raises_with_clear_message():
    pool = GeminiKeyPool(keys=["a", "b"])
    for attempt in pool.attempts():
        pool.mark_exhausted(attempt.key)

    remaining = list(pool.attempts())
    assert remaining == []
    assert "2 configured Gemini key" in pool.exhausted_message()


# --- connection-error classification (gemini_keys.py, shared) --------------

def test_network_errors_are_distinguished_from_quota_errors():
    """A TLS handshake drop is not a quota problem -- it should retry the
    same key, not rotate to a different one. Shared by web_search.py,
    research.py, and qualify.py so the classification can't drift between
    them."""
    import httpx
    from csg.discovery.gemini_keys import is_connection_error

    assert is_connection_error(httpx.ConnectError("SSL EOF"))
    assert is_connection_error(httpx.ConnectTimeout("timed out"))
    assert is_connection_error(httpx.ReadTimeout("timed out"))
    assert is_connection_error(httpx.RemoteProtocolError("reset"))
    assert not is_connection_error(_daily_quota_error())
    assert not is_connection_error(ValueError("not a network error"))


def test_ssl_eof_message_without_httpx_type_is_still_a_connection_error():
    """The google-genai SDK can wrap the underlying httpx exception rather
    than let it propagate directly -- the message-text fallback must still
    catch the specific SSL failure mode this fix targets."""
    from csg.discovery.gemini_keys import is_connection_error

    assert is_connection_error(
        Exception("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    )
    assert not is_connection_error(Exception("400 Bad Request: invalid argument"))
