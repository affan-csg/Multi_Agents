"""personalize._call_llm -- three independent tiers (every configured Gemini
key, then Anthropic, then Groq), each a genuine fallback on any failure
(quota, timeout, outage, no billing credit) rather than only when the next
tier's key is entirely absent.

Regression coverage for a real production incident: Gemini's free-tier
quota was exhausted mid-testing while Anthropic was configured and healthy,
and drafting silently failed instead of using the working provider. A
second incident during the same testing surfaced the multi-key case: a
second Gemini key (GEMINI_API_KEY_2) had untouched quota the whole time and
nothing in the code path ever tried it. A third incident -- Gemini quota
exhausted AND the Anthropic account out of credit at the same time -- is
what Groq exists to survive: a provider billed and rate-limited completely
independently of the other two."""

from __future__ import annotations

import pytest

from outreach import personalize
from outreach.gemini_keys import GeminiKeyPool, is_daily_quota_exhausted


class _QuotaExhausted(Exception):
    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED: You exceeded your current quota")


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")


# ---------------------------------------------------------------------------
# _call_llm -- Gemini(-pool) -> Anthropic -> Groq
# ---------------------------------------------------------------------------

def test_uses_gemini_when_it_succeeds(monkeypatch):
    monkeypatch.setattr(personalize, "_call_gemini_with_key_pool", lambda p: "gemini response")
    monkeypatch.setattr(
        personalize, "_call_anthropic",
        lambda p: pytest.fail("Anthropic must not be called when Gemini succeeds"),
    )

    assert personalize._call_llm("prompt") == "gemini response"


def test_falls_back_to_anthropic_when_gemini_raises(monkeypatch):
    """The original bug: a Gemini exception (429 quota exhausted, a timeout,
    any transient failure) must not be treated as 'no LLM available' when
    Anthropic is configured and working."""
    def gemini_fails(p):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(personalize, "_call_gemini_with_key_pool", gemini_fails)
    monkeypatch.setattr(personalize, "_call_anthropic", lambda p: "anthropic response")

    assert personalize._call_llm("prompt") == "anthropic response"


def test_falls_back_to_anthropic_when_gemini_returns_empty(monkeypatch):
    """An empty/falsy response (not an exception) from Gemini must also
    trigger the fallback, not just a raised error."""
    monkeypatch.setattr(personalize, "_call_gemini_with_key_pool", lambda p: "")
    monkeypatch.setattr(personalize, "_call_anthropic", lambda p: "anthropic response")

    assert personalize._call_llm("prompt") == "anthropic response"


def test_falls_back_to_groq_when_gemini_and_anthropic_both_fail(monkeypatch):
    """The third incident this chain exists for: Gemini's quota exhausted
    AND the Anthropic account out of credit, at the same time -- Groq, being
    billed and rate-limited completely independently of both, is what keeps
    drafting working when it's still up."""
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(
        personalize, "_call_gemini_with_key_pool",
        lambda p: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")),
    )
    monkeypatch.setattr(
        personalize, "_call_anthropic",
        lambda p: (_ for _ in ()).throw(RuntimeError("400 credit balance too low")),
    )
    monkeypatch.setattr(personalize, "_call_groq_plain", lambda p: "groq response")

    assert personalize._call_llm("prompt") == "groq response"


def test_groq_not_called_when_gemini_succeeds(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(personalize, "_call_gemini_with_key_pool", lambda p: "gemini response")
    monkeypatch.setattr(
        personalize, "_call_groq_plain",
        lambda p: pytest.fail("Groq must not be called when an earlier tier succeeds"),
    )

    assert personalize._call_llm("prompt") == "gemini response"


def test_groq_skipped_without_a_key(monkeypatch):
    """No GROQ_API_KEY -- the chain ends at Anthropic's failure, same as
    before Groq existed, rather than raising or hanging on a missing key."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(
        personalize, "_call_gemini_with_key_pool",
        lambda p: (_ for _ in ()).throw(RuntimeError("down")),
    )
    monkeypatch.setattr(personalize, "_call_anthropic", lambda p: (_ for _ in ()).throw(RuntimeError("down too")))

    assert personalize._call_llm("prompt") is None


def test_returns_none_when_all_three_fail(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setattr(
        personalize, "_call_gemini_with_key_pool",
        lambda p: (_ for _ in ()).throw(RuntimeError("down")),
    )
    monkeypatch.setattr(personalize, "_call_anthropic", lambda p: (_ for _ in ()).throw(RuntimeError("down too")))
    monkeypatch.setattr(personalize, "_call_groq_plain", lambda p: None)

    assert personalize._call_llm("prompt") is None


def test_anthropic_only_when_gemini_key_absent(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        personalize, "_call_gemini_with_key_pool",
        lambda p: pytest.fail("Gemini must not be called with no key configured"),
    )
    monkeypatch.setattr(personalize, "_call_anthropic", lambda p: "anthropic response")

    assert personalize._call_llm("prompt") == "anthropic response"


def test_none_when_no_provider_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert personalize._call_llm("prompt") is None


# ---------------------------------------------------------------------------
# _call_gemini_with_key_pool -- rotating through multiple Gemini keys
# ---------------------------------------------------------------------------

def test_second_key_used_when_first_key_quota_exhausted(monkeypatch):
    """The actual second incident: GEMINI_API_KEY's daily quota was gone,
    but GEMINI_API_KEY_2 (configured, untouched) had capacity the whole
    time -- this is what makes that key actually get tried."""
    monkeypatch.setenv("GEMINI_API_KEY_2", "fake-gemini-key-2")

    calls: list[str] = []

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = self

        def generate_content(self, model, contents):
            calls.append(self.api_key)
            if self.api_key == "fake-gemini-key":
                raise _QuotaExhausted()
            resp = type("Resp", (), {"text": "drafted via second key"})()
            return resp

    import google.genai as genai_module
    monkeypatch.setattr(genai_module, "Client", FakeClient)

    result = personalize._call_gemini_with_key_pool("prompt")

    assert result == "drafted via second key"
    assert calls == ["fake-gemini-key", "fake-gemini-key-2"]


def test_returns_none_when_every_key_quota_exhausted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_2", "fake-gemini-key-2")

    class FakeClient:
        def __init__(self, api_key):
            self.models = self

        def generate_content(self, model, contents):
            raise _QuotaExhausted()

    import google.genai as genai_module
    monkeypatch.setattr(genai_module, "Client", FakeClient)

    assert personalize._call_gemini_with_key_pool("prompt") is None


def test_non_quota_error_is_not_caught_by_the_pool(monkeypatch):
    """A real error (bad request, safety block, malformed response) must
    propagate immediately, not be swallowed as if it were a quota issue --
    _call_llm's own except/fallback is what's meant to handle it."""
    monkeypatch.setenv("GEMINI_API_KEY_2", "fake-gemini-key-2")

    calls: list[str] = []

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = self

        def generate_content(self, model, contents):
            calls.append(self.api_key)
            raise ValueError("malformed request")

    import google.genai as genai_module
    monkeypatch.setattr(genai_module, "Client", FakeClient)

    with pytest.raises(ValueError):
        personalize._call_gemini_with_key_pool("prompt")

    # Only the first key was tried -- a non-quota error must not advance
    # through the pool.
    assert calls == ["fake-gemini-key"]


# ---------------------------------------------------------------------------
# gemini_keys.py -- key discovery and quota classification
# ---------------------------------------------------------------------------

def test_pool_discovers_numbered_keys_in_order(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    monkeypatch.setenv("GEMINI_API_KEY_3", "k3")

    pool = GeminiKeyPool.from_env()

    assert [a.key for a in pool.attempts()] == ["k1", "k2", "k3"]


def test_pool_drops_duplicate_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "same-key")
    monkeypatch.setenv("GEMINI_API_KEY_2", "same-key")

    pool = GeminiKeyPool.from_env()

    assert [a.key for a in pool.attempts()] == ["same-key"]


def test_pool_empty_without_any_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

    pool = GeminiKeyPool.from_env()

    assert bool(pool) is False
    assert list(pool.attempts()) == []


def test_daily_quota_exhausted_detected_from_message_text():
    """The real 429 response format seen during testing sometimes lacks a
    structured violations block, so the message text is the fallback
    signal -- confirmed against a live 429 (see gemini_keys.py)."""
    exc = RuntimeError(
        "ClientError 429 RESOURCE_EXHAUSTED. {'error': {'message': "
        "'You exceeded your current quota, please check your plan and billing details.'}}"
    )
    assert is_daily_quota_exhausted(exc) is True


def test_per_minute_rate_limit_is_not_daily_quota_exhausted():
    exc = type("Exc", (Exception,), {"details": {
        "error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]},
    }})()
    assert is_daily_quota_exhausted(exc) is False


def test_unrelated_error_is_not_daily_quota_exhausted():
    assert is_daily_quota_exhausted(ValueError("malformed prompt")) is False
