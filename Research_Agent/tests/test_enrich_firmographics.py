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
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from csg import discover
from csg.discovery import research
from csg.models import Company
from csg import usage_tracker


def _company(domain: str = "acme.com") -> Company:
    return Company(domain=domain, name="Acme", status="discovered")


@pytest.fixture(autouse=True)
def capture_provider_usage(monkeypatch):
    rows = []
    monkeypatch.setattr(
        usage_tracker,
        "_record_async",
        lambda usage, latency_ms: rows.append({
            "provider": usage.provider, "call_kind": usage.call_kind,
            "domain": usage.domain, "success": usage.success,
            "status_code": usage.status_code, "error_kind": usage.error_kind,
            "retry_count": usage.retry_count,
        }),
    )
    return rows


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://acme.com"))],
    ))])


_FIRMOGRAPHICS_JSON = (
    '{"official_website": "https://acme.com", "official_website_source_url": "https://acme.com", '
    '"official_website_excerpt": "Acme official website", '
    '"headquarters": {"city": "Austin", "state": "TX", "country": "United States"}, '
    '"headquarters_source_url": "https://acme.com", "headquarters_excerpt": "Headquartered in Austin, TX", '
    '"company_type": "smb", "company_type_source_url": "https://acme.com", '
    '"company_type_excerpt": "Acme is a small business", "employee_count": 500, '
    '"employee_count_source_url": "https://acme.com", "employee_count_excerpt": "Acme has 500 employees", '
    '"revenue": "$50M", "revenue_source_url": "https://acme.com", '
    '"revenue_excerpt": "Annual revenue is $50M", "funding": null, '
    '"industry": "Software", "description": "A company.", '
    '"hq_location": "Austin, TX", "founded_year": 2015}'
)


def _tavily_groq_transport(handler):
    return httpx.MockTransport(handler)


def _patch_httpx_client(monkeypatch, handler):
    """Route async fallback requests through a MockTransport."""
    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(research.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        research, "make_ipv4_async_httpx_client",
        lambda: httpx.AsyncClient(transport=_tavily_groq_transport(handler)),
    )


def _successful_fallback_handler(request: httpx.Request) -> httpx.Response:
    if "tavily.com" in str(request.url):
        return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": (
            "Acme official website. Headquartered in Austin, TX. Acme is a small business. "
            "Acme has 500 employees. Annual revenue is $50M."
        )}]})
    if "groq.com" in str(request.url):
        return httpx.Response(200, json={"choices": [{"message": {"content": _FIRMOGRAPHICS_JSON}}]})
    return httpx.Response(404)


@pytest.fixture
def gemini_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-1")
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")


def test_timed_out_gemini_response_cannot_mutate_company_later(gemini_keys, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class FakeModels:
        @staticmethod
        def generate_content(**_kwargs):
            started.set()
            release.wait(2)
            finished.set()
            return _fake_response(_FIRMOGRAPHICS_JSON)

    class FakeClient:
        models = FakeModels()

    class EmptyConn:
        def select(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(
        "csg.discovery.gemini_keys.make_gemini_client", lambda _key: FakeClient(),
    )
    monkeypatch.setattr(discover, "ENRICH_TIMEOUT_SECONDS", 0.02)

    async def run_enrichment():
        def release_after_timeout(stage, done, _total, _detail):
            if stage == "enrich" and done == 1:
                release.set()

        pairs, skipped, failures = await discover._enrich_all(
            EmptyConn(), [SimpleNamespace(domain="slow.example", name="Slow", industry=None)],
            "GA", release_after_timeout,
        )
        assert skipped == 0
        assert failures == {"timeout": 1}
        assert await asyncio.to_thread(finished.wait, 1)
        return pairs[0][1]

    company = asyncio.run(run_enrichment())
    assert started.is_set()
    assert company.employee_count is None
    assert company.website is None


def test_non_quota_gemini_failure_falls_back_to_tavily_groq(
    gemini_keys, monkeypatch, capture_provider_usage,
):
    """The bug this test locks in: a generic/non-quota exception from the
    Gemini call must still try the fallback, not go straight to
    request_error."""
    class FakeClient:
        def __init__(self, api_key, **kwargs):
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
    assert company.employee_count.value == 500
    assert company.employee_count.source == "groq_tavily_search"
    assert [row["provider"] for row in capture_provider_usage].count("gemini") == 1
    assert any(row["provider"] == "tavily" and row["success"] for row in capture_provider_usage)
    assert any(row["provider"] == "groq" and row["success"] for row in capture_provider_usage)


def test_fallback_passes_tavily_raw_page_content_to_groq(gemini_keys, monkeypatch):
    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("controlled Gemini failure")

    monkeypatch.setattr("google.genai.Client", FakeClient)
    received = {"tavily_raw_requested": False, "groq_received_raw": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            body = json.loads(request.content)
            received["tavily_raw_requested"] = body.get("include_raw_content") == "markdown"
            return httpx.Response(200, json={"results": [{
                "title": "Acme company profile",
                "url": "https://acme.com/about",
                "content": "short search snippet",
                "raw_content": "FULL PAGE CONTENT: Acme is headquartered in Austin.",
            }]})
        if "groq.com" in str(request.url):
            prompt = json.loads(request.content)["messages"][0]["content"]
            received["groq_received_raw"] = (
                "FULL PAGE CONTENT: Acme is headquartered in Austin." in prompt
                and "short search snippet" not in prompt
            )
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, handler)
    company = _company()

    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    assert received == {"tavily_raw_requested": True, "groq_received_raw": True}


def test_quota_exhausted_still_falls_back_to_tavily_groq(gemini_keys, monkeypatch, capture_provider_usage):
    """Same fallback path the quota-exhaustion case already used before this
    fix -- must still work identically afterwards."""
    class QuotaError(Exception):
        code = 429
        def __str__(self):
            return "RESOURCE_EXHAUSTED: quota exceeded"

    class FakeClient:
        def __init__(self, api_key, **kwargs):
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
    assert company.employee_count.value == 500
    assert company.employee_count.value == 500
    gemini_usage = [row for row in capture_provider_usage if row["provider"] == "gemini"]
    assert len(gemini_usage) == 1
    assert gemini_usage[0]["success"] is False
    assert gemini_usage[0]["status_code"] == 429
    assert gemini_usage[0]["error_kind"] == "quota_exhausted"


def test_gemini_success_never_touches_fallback(gemini_keys, monkeypatch):
    """A normal, successful Gemini call must not go anywhere near Tavily/Groq."""
    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
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


def test_gemini_ungrounded_claim_triggers_fallback(gemini_keys, monkeypatch):
    response = SimpleNamespace(
        text=(
            '{"official_website": "https://acme.com", "official_website_source_url": '
            '"https://not-grounded.example/about", "headquarters": {"city": "Austin", "state": "TX", "country": "United States"}, '
            '"headquarters_source_url": "https://not-grounded.example/about", "company_type": "smb", '
            '"company_type_source_url": "https://not-grounded.example/about", "employee_count": 500, '
            '"employee_count_source_url": "https://not-grounded.example/about"}'
        ),
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://acme.com"))],
        ))],
    )

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)

    called = {"count": 0}

    async def fake_fallback(company):
        called["count"] += 1
        return None

    monkeypatch.setattr(research, "_enrich_via_groq_tavily", fake_fallback)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert called["count"] == 1


@pytest.mark.parametrize("response_text", ["", "not json"])
def test_empty_or_malformed_gemini_response_triggers_fallback(gemini_keys, monkeypatch, response_text):
    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                return SimpleNamespace(text=response_text, candidates=[])

    monkeypatch.setattr("google.genai.Client", FakeClient)
    called = {"count": 0}

    async def fake_fallback(company):
        called["count"] += 1
        return None

    monkeypatch.setattr(research, "_enrich_via_groq_tavily", fake_fallback)
    company = _company()

    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    assert called["count"] == 1
    assert company.employee_count is None


@pytest.mark.parametrize(
    ("source_url", "excerpt", "source_content", "allowed_urls", "reason"),
    [
        ("https://unprovided.example/about", "Employees: 500", {}, {"https://acme.com/about"}, "source URL not present"),
        ("javascript:alert(1)", "Employees: 500", {"javascript:alert(1)": "Employees: 500"}, {"javascript:alert(1)"}, "valid HTTP(S) URL"),
        ("https://acme.com/about", "Employees: 500", {"https://acme.com/about": "Employees: 50"}, {"https://acme.com/about"}, "excerpt not found"),
        ("https://acme.com/about", "", {"https://acme.com/about": "Employees: 500"}, {"https://acme.com/about"}, "claim or supporting excerpt is missing"),
    ],
)
def test_fallback_evidence_rejects_bad_citations_and_excerpts(
    source_url, excerpt, source_content, allowed_urls, reason,
):
    company = _company()
    research._apply_firmographics(
        company,
        {"research_evidence": [{
            "field": "services", "criterion": "company_services",
            "claim": {"services": "Software services"}, "source_url": source_url,
            "excerpt": excerpt,
        }]},
        source="groq_tavily_search", allowed_source_urls=allowed_urls,
        source_content=source_content, searched_fields={"services"},
    )

    assert company.research_evidence == []
    outcome = company.research_outcomes["services"]
    assert outcome["status"] == "found_but_rejected"
    assert reason in outcome["reason"]


def test_fallback_evidence_normalizes_unicode_and_whitespace_but_requires_verbatim_excerpt():
    company = _company()
    source_url = "https://acme.com/services"
    research._apply_firmographics(
        company,
        {"research_evidence": [{
            "field": "services", "criterion": "company_services",
            "claim": {"services": "software implementation"}, "source_url": source_url,
            "excerpt": "We provide software implementation services.",
        }]},
        source="groq_tavily_search", allowed_source_urls={source_url},
        source_content={source_url: "We provide software  implementation services."},
        searched_fields={"services"},
    )
    assert len(company.research_evidence) == 1

    unsupported = _company()
    research._apply_firmographics(
        unsupported,
        {"research_evidence": [{
            "field": "services", "criterion": "company_services",
            "claim": {"services": "software implementation"}, "source_url": source_url,
            "excerpt": "We deliver enterprise-grade software implementation.",
        }]},
        source="groq_tavily_search", allowed_source_urls={source_url},
        source_content={source_url: "We provide software implementation services."},
        searched_fields={"services"},
    )
    assert unsupported.research_evidence == []
    assert "excerpt not found" in unsupported.research_outcomes["services"]["reason"]


def test_conflicting_fallback_claims_are_rejected_as_a_group():
    company = _company()
    source_url = "https://acme.com/services"
    research._apply_firmographics(
        company,
        {"research_evidence": [
            {"field": "services", "criterion": "company_services", "claim": {"services": "A"},
             "source_url": source_url, "excerpt": "We provide service A."},
            {"field": "services", "criterion": "company_services", "claim": {"services": "B"},
             "source_url": source_url, "excerpt": "We provide service B."},
        ]},
        source="groq_tavily_search", allowed_source_urls={source_url},
        source_content={source_url: "We provide service A. We provide service B."},
        searched_fields={"services"},
    )
    assert company.research_evidence == []
    assert company.research_outcomes["services"]["status"] == "found_but_rejected"
    assert company.research_outcomes["services"]["reason"] == "conflicting claims supplied for the same field"


def test_fallback_failure_preserves_existing_values_evidence_and_reports_fields(gemini_keys, monkeypatch):
    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("controlled Gemini failure")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def empty_search(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            return httpx.Response(200, json={"results": []})
        pytest.fail("Groq must not be called when Tavily returned no sources")

    _patch_httpx_client(monkeypatch, empty_search)
    original_evidence = [{"evidence_key": "verified-1", "field": "services", "claim": {"services": "Known"}}]
    company = Company(
        domain="acme.com", name="Acme",
        website={"value": "https://acme.com", "source": "operator_approved", "method": "reported", "confidence": "high"},
        employee_count={"value": 42, "source": "human_verified", "method": "reported", "confidence": "high"},
        research_evidence=original_evidence,
        research_outcomes={"services": {"status": "accepted_and_mapped", "criterion": "company_services"}},
    )
    before = company.model_dump(mode="json")

    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    after = company.model_dump(mode="json")
    assert after["website"] == before["website"]
    assert after["employee_count"] == before["employee_count"]
    assert after["research_evidence"] == before["research_evidence"]
    assert company.research_outcomes["services"]["reason"] == "Tavily returned no source URLs for this field"
    assert company.research_outcomes["services"]["status"] == "searched_but_not_found"


def test_empty_groq_response_preserves_data_and_reports_parse_failure(gemini_keys, monkeypatch):
    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("controlled Gemini failure")

    monkeypatch.setattr("google.genai.Client", FakeClient)
    source_url = "https://acme.com/about"

    def empty_groq(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            return httpx.Response(200, json={"results": [{
                "title": "Acme", "url": source_url,
                "content": "Acme has 42 employees.", "raw_content": "Acme has 42 employees.",
            }]})
        if "groq.com" in str(request.url):
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, empty_groq)
    company = Company(
        domain="acme.com", name="Acme",
        employee_count={"value": 42, "source": "manual", "method": "reported", "confidence": "high"},
    )

    assert asyncio.run(research.enrich_company_firmographics(company)) == "parse_error"
    assert company.employee_count.value == 42
    assert company.research_outcomes["employee_range"]["reason"] == "provider response could not be parsed"


def test_firmographic_source_urls_must_match_returned_sources():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "employee_count": 500,
            "employee_count_source_url": "https://acme.com/about",
            "revenue": "$50M",
            "revenue_source_url": "https://fabricated.example/revenue",
            "funding": None,
        },
        source="test_search",
        allowed_source_urls={"https://acme.com/about"},
    )

    assert company.employee_count.url == "https://acme.com/about"
    assert company.employee_count.fetched_at is not None
    assert company.revenue is None
    assert company.research_outcomes["employee_range"]["status"] == "accepted_and_mapped"


def test_identity_facts_require_returned_sources_and_validate_official_domain():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "official_website": "https://fabricated.example",
            "official_website_source_url": "https://acme.com/about",
            "headquarters": {"city": "Austin", "state": "TX", "country": "United States"},
            "headquarters_source_url": "https://acme.com/about",
            "company_type": "system_integrator",
            "company_type_source_url": "https://acme.com/about",
        },
        source="test_search",
        allowed_source_urls={"https://acme.com/about"},
    )

    assert company.website is None
    assert company.headquarters.value["city"] == "Austin"
    assert company.headquarters.url == "https://acme.com/about"
    assert company.company_type.value == "system_integrator"


def test_identity_facts_stay_unavailable_when_search_has_no_supporting_sources():
    company = _company()
    research._apply_firmographics(
        company,
        {"official_website": None, "headquarters": None, "company_type": None},
        source="test_search",
        allowed_source_urls=set(),
    )

    assert company.website is None
    assert company.headquarters is None
    assert company.company_type is None


def test_real_sailpoint_result_preserves_public_company_type_and_provenance():
    company = Company(domain="sailpoint.com", name="SailPoint")
    research._apply_firmographics(
        company,
        {
            "official_website": "https://www.sailpoint.com",
            "official_website_source_url": "https://www.globaldata.com/company-profile/sailpoint-technologies-inc",
            "official_website_excerpt": "SailPoint official website https://www.sailpoint.com",
            "headquarters": {"city": "Austin", "state": "Texas", "country": "United States"},
            "headquarters_source_url": "https://www.globaldata.com/company-profile/sailpoint-technologies-inc",
            "headquarters_excerpt": "Headquartered in Austin, Texas, United States",
            "company_type": "Public Company",
            "company_type_source_url": "https://tavily.com/knowledge/company/dMCtx7o7Rxbq",
            "company_type_excerpt": "SailPoint is a publicly traded company",
        },
        source="groq_tavily_search",
        allowed_source_urls={
            "https://www.globaldata.com/company-profile/sailpoint-technologies-inc",
            "https://tavily.com/knowledge/company/dMCtx7o7Rxbq",
        },
        source_content={
            "https://www.globaldata.com/company-profile/sailpoint-technologies-inc": (
                "SailPoint official website https://www.sailpoint.com. "
                "Headquartered in Austin, Texas, United States."
            ),
            "https://tavily.com/knowledge/company/dMCtx7o7Rxbq": "SailPoint is a publicly traded company",
        },
    )

    assert company.website.value == "https://www.sailpoint.com"
    assert company.headquarters.value["city"] == "Austin"
    assert company.company_type.value == "Public Company"


def test_gemini_grounding_url_is_saved_with_fact_provenance(gemini_keys, monkeypatch):
    source_url = "https://acme.com/about"
    response = SimpleNamespace(
        text=(
            '{"employee_count": 500, "employee_count_source_url": "' + source_url + '", '
            '"revenue": null, "funding": null, "industry": "Software", '
            '"description": "A company.", "hq_location": null, "founded_year": null}'
        ),
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri=source_url))],
        ))],
    )

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.employee_count.url == source_url
    assert company.employee_count.fetched_at


def test_transient_connection_error_retries_same_key_before_fallback(gemini_keys, monkeypatch):
    """A single connection blip (the IPv6/TLS failure mode this whole fix
    targets) should retry the same Gemini key once before falling through to
    Tavily+Groq -- not treat a one-off drop as a full outage."""
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
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
    assert company.employee_count.source == "gemini_grounded_search"


def test_non_quota_failure_then_fallback_also_fails_reports_request_error(
    gemini_keys, monkeypatch, capture_provider_usage,
):
    """If both Gemini and the fallback fail, the company is still reported as
    a genuine failure -- just after both real attempts, not silently."""
    class FakeClient:
        def __init__(self, api_key, **kwargs):
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
    gemini_rows = [row for row in capture_provider_usage if row["provider"] == "gemini"]
    tavily_rows = [row for row in capture_provider_usage if row["provider"] == "tavily"]
    assert gemini_rows and gemini_rows[0]["success"] is False
    assert tavily_rows and tavily_rows[0]["success"] is False
    assert tavily_rows[0]["status_code"] == 500


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
        def __init__(self, api_key, **kwargs):
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
        def __init__(self, api_key, **kwargs):
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
    assert company.employee_count.source == "groq_tavily_search"


def test_no_gemini_and_no_fallback_keys_reports_quota_exhausted(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for i in range(2, 6):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "quota_exhausted"


def test_tavily_432_stops_later_fallback_calls(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(432, request=request, json={"detail": {"error": "plan limit exceeded"}})

    _patch_httpx_client(monkeypatch, handler)
    limited = asyncio.Event()

    first_reason = asyncio.run(research._enrich_via_groq_tavily(_company("first.example"), limited))
    second_reason = asyncio.run(research._enrich_via_groq_tavily(_company("second.example"), limited))

    assert first_reason == "quota_exhausted"
    assert second_reason == "quota_exhausted"
    assert limited.is_set()
    assert len(requests) == 1


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
        def __init__(self, api_key, **kwargs):
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
            return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": (
                "Acme official website. Headquartered in Austin, TX. Acme is a small business. "
                "Acme has 500 employees. Annual revenue is $50M."
            )}]})
        if "groq.com" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {"content": _FIRMOGRAPHICS_JSON}}]})
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    # One retry for the first targeted search plus six other field searches.
    assert calls["tavily"] == 8
    assert company.employee_count.source == "groq_tavily_search"


def test_groq_rate_limit_retries_before_succeeding(gemini_keys, monkeypatch):
    """Same regression as above, but the 429 comes from Groq's parse call
    instead of Tavily's search call."""
    monkeypatch.setattr("csg.discovery.research._time.sleep", lambda *_: None)

    calls = {"groq": 0}

    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("upstream connection reset")

    monkeypatch.setattr("google.genai.Client", FakeClient)

    def handler(request: httpx.Request) -> httpx.Response:
        if "tavily.com" in str(request.url):
            return httpx.Response(200, json={"results": [{"title": "Acme", "url": "https://acme.com", "content": (
                "Acme official website. Headquartered in Austin, TX. Acme is a small business. "
                "Acme has 500 employees. Annual revenue is $50M."
            )}]})
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
    assert company.employee_count.source == "groq_tavily_search"


def test_gemini_transient_429_retries_once_on_same_key(gemini_keys, monkeypatch):
    monkeypatch.setattr(research._time, "sleep", lambda *_: None)
    calls = {"count": 0}

    class RateLimitError(Exception):
        code = 429

        def __str__(self):
            return "429 per-minute rate limit"

    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RateLimitError()
                return _fake_response(_FIRMOGRAPHICS_JSON)

    monkeypatch.setattr("google.genai.Client", FakeClient)
    monkeypatch.setattr(research, "_enrich_via_groq_tavily", lambda *_: pytest.fail("unexpected fallback"))

    company = _company()
    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    assert calls["count"] == 2
    assert company.employee_count.value == 500


def test_daily_quota_advances_key_without_retrying_exhausted_key(gemini_keys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_2", "test-key-2")
    calls = []

    class QuotaError(Exception):
        code = 429

        def __str__(self):
            return "RESOURCE_EXHAUSTED: exceeded your current quota"

    class FakeClient:
        def __init__(self, api_key, **kwargs):
            calls.append(api_key)

        class models:
            @staticmethod
            def generate_content(**kwargs):
                if calls[-1] == "test-key-1":
                    raise QuotaError()
                return _fake_response(_FIRMOGRAPHICS_JSON)

    monkeypatch.setattr("google.genai.Client", FakeClient)

    company = _company()
    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    assert calls == ["test-key-1", "test-key-2"]
    assert company.employee_count.value == 500


def test_field_evidence_outcomes_preserve_manual_facts_and_deduplicate():
    company = Company(
        domain="acme.com", name="Acme",
        website={"value": "https://acme.com", "source": "manual", "method": "reported", "confidence": "high"},
        employee_count={"value": 42, "source": "manual", "method": "reported", "confidence": "high"},
        industry="Operator-verified industry",
        description="Operator-verified description.",
    )
    source_url = "https://acme.com/about"
    data = {
        "official_website": "https://acme.com",
        "official_website_source_url": source_url,
        "employee_count": 99,
        "employee_count_source_url": source_url,
        "classification_evidence": [{
            "category": "startup", "supports": True, "fact": "The company describes itself as a startup.",
            "why": "Explicit identity statement.", "source_url": source_url,
            "source_name": "About", "excerpt": "The company describes itself as a startup.",
            "evidence_type": "direct", "confidence": "high",
        }],
        "research_evidence": [
            {"field": "leadership", "criterion": "company_leadership",
             "claim": {"name": "A. Example", "title": "CEO"}, "source_url": source_url,
             "excerpt": "A. Example, CEO, Chief Executive Officer", "evidence_type": "direct"},
            {"field": "services", "criterion": "company_services",
             "claim": {"services": "software implementation"}, "source_url": source_url,
             "excerpt": "We provide software implementation services.", "evidence_type": "direct"},
            {"field": "hiring_service_fit", "criterion": "company_hiring_fit",
             "claim": {"hiring_signal": "engineering roles listed"}, "source_url": source_url,
             "excerpt": "Engineering roles listed: open engineering positions", "evidence_type": "direct"},
        ],
    }
    allowed = {source_url}

    research._apply_firmographics(company, data, "test_provider", allowed)
    initial_count = len(company.research_evidence)
    research._apply_firmographics(company, data, "test_provider", allowed)

    assert company.website.value == "https://acme.com"
    assert company.website.source == "manual"
    assert company.employee_count.value == 42
    assert company.employee_count.source == "manual"
    assert company.industry == "Operator-verified industry"
    assert company.description == "Operator-verified description."
    assert company.research_outcomes["official_website"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["headquarters"]["status"] == "searched_but_not_found"
    assert company.research_outcomes["employee_range"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["company_classification"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["leadership"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["services"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["hiring_service_fit"]["status"] == "accepted_and_mapped"
    assert len(company.research_evidence) == initial_count == 3
    assert len(company.classification_evidence) == 1


def test_conflicting_classification_evidence_is_rejected():
    company = _company()
    source_url = "https://acme.com/about"
    research._apply_firmographics(
        company,
        {"classification_evidence": [
            {"category": "startup", "supports": True, "fact": "Acme is a startup.",
             "why": "Explicit statement.", "source_url": source_url,
             "excerpt": "Acme is a startup.", "evidence_type": "direct"},
            {"category": "startup", "supports": False, "fact": "Acme is not a startup.",
             "why": "Contradictory statement.", "source_url": source_url,
             "excerpt": "Acme is not a startup.", "evidence_type": "direct"},
        ]},
        source="groq_tavily_search", allowed_source_urls={source_url},
        source_content={source_url: "Acme is a startup. Acme is not a startup."},
        searched_fields={"company_classification"},
    )
    assert company.classification_evidence == []
    outcome = company.research_outcomes["company_classification"]
    assert outcome["status"] == "found_but_rejected"
    assert "conflicting classification" in outcome["reason"]


def test_missing_classification_citation_has_specific_rejection_reason():
    company = _company()
    research._apply_firmographics(
        company,
        {"classification_evidence": [{
            "category": "startup", "supports": True, "fact": "Acme is a startup.",
            "why": "Explicit statement.", "excerpt": "Acme is a startup.",
            "evidence_type": "direct",
        }]},
        source="groq_tavily_search", allowed_source_urls=set(),
        searched_fields={"company_classification"},
    )
    outcome = company.research_outcomes["company_classification"]
    assert outcome["status"] == "found_but_rejected"
    assert outcome["reason"] == "classification evidence source URL is missing"


def test_top_level_and_structured_fallback_claim_conflict_is_rejected():
    company = _company()
    source_url = "https://acme.com/services"
    research._apply_firmographics(
        company,
        {
            "services": "Managed hosting",
            "services_source_url": source_url,
            "services_excerpt": "Acme provides managed hosting.",
            "research_evidence": [{
                "field": "services", "criterion": "company_services",
                "claim": {"services": "Software implementation"},
                "source_url": source_url,
                "excerpt": "Acme provides software implementation.",
            }],
        },
        source="groq_tavily_search", allowed_source_urls={source_url},
        source_content={source_url: "Acme provides managed hosting. Acme provides software implementation."},
        searched_fields={"services"},
    )
    assert company.research_evidence == []
    outcome = company.research_outcomes["services"]
    assert outcome["status"] == "found_but_rejected"
    assert outcome["reason"] == "conflicting claims supplied for the same field"


def test_found_but_rejected_records_reason_and_bad_mapping():
    company = _company()
    research._apply_firmographics(
        company,
        {"services": "Implementation services", "research_evidence": [{
            "field": "services", "criterion": "company_hiring_fit",
            "claim": {"services": "Implementation services"},
            "source_url": "https://acme.com/services", "excerpt": "We implement software.",
        }]},
        source="test_provider", allowed_source_urls={"https://acme.com/services"},
        searched_fields={"services"},
    )

    outcome = company.research_outcomes["services"]
    assert outcome["status"] == "found_but_rejected"
    assert outcome["reason"] == "evidence criterion does not match the field mapping"
    assert company.research_evidence == []


def test_fallback_query_matrix_covers_each_requested_research_field():
    assert {field for field, _ in research._FALLBACK_QUERIES} == set(research._FIELD_CRITERIA)
