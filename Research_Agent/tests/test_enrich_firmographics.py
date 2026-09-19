"""Tests for enrich_company_firmographics's Gemini -> Tavily+Groq fallback
control flow (discovery/research.py).

Regression: a non-quota Gemini failure (network blip, malformed/empty
grounded-search response, transient 5xx, safety-filter block, ...) used to
`raise` straight out of the key-attempt loop and land on "request_error"
without ever trying the Tavily+Groq fallback -- only daily-quota exhaustion
triggered it. Seen live as "Enrichment skipped for 1 company: enrichment
request failed (1)" in a research run: one company's Gemini call failed for
a reason unrelated to quota, and the fallback that exists specifically to
cover this never fired. Fixed so ANY Gemini call failure (not just quota
exhaustion) falls through to the same Tavily+Groq fallback, same as the
existing "all keys exhausted" path already did.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from csg.discovery import research
from csg.models import Company


def _company(domain: str = "acme.com") -> Company:
    return Company(domain=domain, name="Acme", status="discovered")


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


_FIRMOGRAPHICS_JSON = (
    '{"employee_count": 500, "revenue": "$50M", "funding": null, '
    '"industry": "Software", "description": "A company.", '
    '"hq_location": "Austin, TX", "founded_year": 2015}'
)


def _tavily_groq_transport(handler):
    return httpx.MockTransport(handler)


def _patch_httpx_client(monkeypatch, handler):
    """_enrich_via_groq_tavily builds its own httpx.Client() with no injection
    point, so patch httpx.Client globally for the duration of the test to
    route through a MockTransport instead of the network."""
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=_tavily_groq_transport(handler))

    monkeypatch.setattr(research.httpx, "Client", fake_client)


def _successful_fallback_handler(request: httpx.Request) -> httpx.Response:
    if "tavily.com" in str(request.url):
        return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": "info"}]})
    if "groq.com" in str(request.url):
        return httpx.Response(200, json={"choices": [{"message": {"content": _FIRMOGRAPHICS_JSON}}]})
    return httpx.Response(404)


@pytest.fixture
def gemini_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-1")
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")


def test_non_quota_gemini_failure_falls_back_to_tavily_groq(gemini_keys, monkeypatch):
    """The bug this test locks in: a generic/non-quota exception from the
    Gemini call must still try the fallback, not go straight to
    request_error."""
    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")  # not a 429/quota error

    monkeypatch.setattr("google.genai.Client", FakeClient)
    _patch_httpx_client(monkeypatch, _successful_fallback_handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None  # fallback succeeded -> enrichment overall succeeded
    assert company.employee_count["value"] == 500
    assert company.employee_count["source"] == "groq_tavily_search"


def test_quota_exhausted_still_falls_back_to_tavily_groq(gemini_keys, monkeypatch):
    """Same fallback path the quota-exhaustion case already used before this
    fix -- must still work identically afterwards."""
    class QuotaError(Exception):
        code = 429
        def __str__(self):
            return "RESOURCE_EXHAUSTED: quota exceeded"

    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise QuotaError()

    monkeypatch.setattr("google.genai.Client", FakeClient)
    _patch_httpx_client(monkeypatch, _successful_fallback_handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.employee_count["value"] == 500


def test_gemini_success_never_touches_fallback(gemini_keys, monkeypatch):
    """A normal, successful Gemini call must not go anywhere near Tavily/Groq."""
    class FakeClient:
        def __init__(self, api_key, http_options=None):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                return _fake_response(_FIRMOGRAPHICS_JSON)

    monkeypatch.setattr("google.genai.Client", FakeClient)

    async def fail_if_called(*a, **kw):
        raise AssertionError("Tavily/Groq fallback must not be called on Gemini success")

    # Patch the fallback function itself rather than httpx.Client: both the
    # real fallback (_enrich_via_groq_tavily) and make_gemini_client's own
    # IPv4-pinning setup (gemini_keys.py) legitimately construct httpx.Client
    # instances now, so patching the shared httpx module would falsely trip
    # on the latter too.
    monkeypatch.setattr(research, "_enrich_via_groq_tavily", fail_if_called)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.employee_count["value"] == 500
    assert company.employee_count["source"] == "gemini_grounded_search"


def test_transient_connection_error_retries_same_key_before_fallback(gemini_keys, monkeypatch):
    """A single connection blip (the IPv6/TLS failure mode this whole fix
    targets) should retry the same Gemini key once before falling through to
    Tavily+Groq -- not treat a one-off drop as a full outage."""
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, api_key, http_options=None):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
                return _fake_response(_FIRMOGRAPHICS_JSON)

    monkeypatch.setattr("google.genai.Client", FakeClient)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    async def fail_if_called(*a, **kw):
        raise AssertionError("Tavily/Groq fallback must not fire when the retry recovers")

    monkeypatch.setattr(research, "_enrich_via_groq_tavily", fail_if_called)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert calls["n"] == 2
    assert company.employee_count["source"] == "gemini_grounded_search"


def test_non_quota_failure_then_fallback_also_fails_reports_request_error(gemini_keys, monkeypatch):
    """If both Gemini and the fallback fail, the company is still reported as
    a genuine failure -- just after both real attempts, not silently."""
    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def always_fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _patch_httpx_client(monkeypatch, always_fail)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "request_error"


def test_fallback_rate_limit_exhausted_reports_rate_limited_not_request_error(gemini_keys, monkeypatch):
    """Regression for the vague-error-message ask: when Tavily/Groq's own
    429-retry budget (see test_tavily_rate_limit_retries_before_succeeding)
    is exhausted, the company's failure reason must say "rate_limited", not
    the generic "request_error" -- so a run-level summary like "Enrichment
    skipped for N companies" tells the user it was a rate limit (transient,
    likely to succeed on its own next run) rather than an unspecified
    request failure."""
    monkeypatch.setattr("csg.discovery.research._time.sleep", lambda *_: None)

    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    _patch_httpx_client(monkeypatch, always_429)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "rate_limited"


def test_fallback_connection_error_reports_connection_error_not_request_error(gemini_keys, monkeypatch):
    """Regression: a network/TLS failure reaching Tavily or Groq must report
    "connection_error", not the generic "request_error" -- this is the exact
    distinction (IPv6/TLS routing issue vs. an ordinary API error) the
    IPv4-pinning fix earlier in this file exists to make diagnosable."""
    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")

    _patch_httpx_client(monkeypatch, raise_connect_error)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "connection_error"


def test_no_gemini_key_goes_straight_to_fallback(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for i in range(2, 6):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")

    _patch_httpx_client(monkeypatch, _successful_fallback_handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.employee_count["source"] == "groq_tavily_search"


def test_no_gemini_and_no_fallback_keys_reports_quota_exhausted(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for i in range(2, 6):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "quota_exhausted"


def test_tavily_rate_limit_retries_before_succeeding(gemini_keys, monkeypatch):
    """Regression: seen live as 2 companies reported "enrichment request
    failed" in a run where Gemini was fully quota-exhausted for every
    company (confirmed by every successful company coming back tagged
    groq_tavily_search) -- so those 2 companies hit the Tavily+Groq fallback
    with no retry of its own, and a single transient 429 from Tavily (its
    free tier is metered per-minute, and ENRICH_CONCURRENCY runs several
    companies through the fallback at once) immediately failed the company.
    The fallback must retry a 429 like GroqTavilyProvider's discovery path
    already does, not fail on the first hit."""
    monkeypatch.setattr("csg.discovery.research._time.sleep", lambda *_: None)

    calls = {"tavily": 0}

    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def handler(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            calls["tavily"] += 1
            if calls["tavily"] == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": "info"}]})
        if "groq.com" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {"content": _FIRMOGRAPHICS_JSON}}]})
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert calls["tavily"] == 2
    assert company.employee_count["source"] == "groq_tavily_search"


def test_groq_rate_limit_retries_before_succeeding(gemini_keys, monkeypatch):
    """Same regression as above, but the 429 comes from Groq's parse call
    instead of Tavily's search call."""
    monkeypatch.setattr("csg.discovery.research._time.sleep", lambda *_: None)

    calls = {"groq": 0}

    class FakeClient:
        def __init__(self, api_key):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def handler(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": "info"}]})
        if "groq.com" in str(request.url):
            calls["groq"] += 1
            if calls["groq"] == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": _FIRMOGRAPHICS_JSON}}]})
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert calls["groq"] == 2
    assert company.employee_count["source"] == "groq_tavily_search"
