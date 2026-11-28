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

from csg import discover, gemini_queue
from csg.discovery import research
from csg.models import Company, ProvenanceValue
from csg import usage_tracker


def _company(domain: str = "acme.com") -> Company:
    return Company(domain=domain, name="Acme", status="discovered")


@pytest.fixture(autouse=True)
def capture_provider_usage(monkeypatch):
    rows = []
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {
        "wait_seconds": 0, "current_rpm": 1, "queue_depth": 0, "blocked_reason": None,
    })
    monkeypatch.setattr(gemini_queue, "_record", lambda *_args, **_kwargs: {
        "current_rpm": 1, "queue_depth": 0,
    })
    monkeypatch.setattr(gemini_queue.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gemini_queue, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(research, "log_event", lambda *_args, **_kwargs: None)
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
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://acme.com/about"))],
        grounding_supports=[SimpleNamespace(
            segment=SimpleNamespace(text=text), grounding_chunk_indices=[0],
        )],
    ))])


_FIRMOGRAPHICS_JSON = (
    '{"official_website": "https://acme.com", "official_website_source_url": "https://acme.com/about", '
    '"official_website_excerpt": "Acme official website is https://acme.com.", '
    '"headquarters": {"city": "Austin", "state": "TX", "country": "United States"}, '
    '"headquarters_source_url": "https://acme.com/about", '
    '"headquarters_excerpt": "Headquartered in Austin TX United States", '
    '"company_type": "small business", "company_type_source_url": "https://acme.com/about", '
    '"company_type_excerpt": "Acme is a small business", '
    '"business_category": "SaaS", "business_category_source_url": "https://acme.com/about", '
    '"business_category_excerpt": "Acme is a SaaS company", "employee_count": 500, '
    '"employee_count_source_url": "https://acme.com/about", "employee_count_excerpt": "Acme has 500 employees", '
    '"revenue": "$50M", "revenue_source_url": "https://acme.com/about", '
    '"revenue_excerpt": "Annual revenue is $50M", "funding": null, '
    '"funding_source_url": null, "funding_excerpt": null, '
    '"industry": "Software", "industry_source_url": "https://acme.com/about", '
    '"industry_excerpt": "Acme is a software company", '
    '"description": "Acme provides workflow software", "description_source_url": "https://acme.com/about", '
    '"description_excerpt": "Acme provides workflow software", '
    '"employee_range": "500 employees", "employee_range_source_url": "https://acme.com/about", '
    '"employee_range_excerpt": "Acme has 500 employees", '
    '"services": "Acme provides software implementation services", '
    '"services_source_url": "https://acme.com/about", '
    '"services_excerpt": "Acme provides software implementation services", '
    '"leadership": [], "hiring_service_fit": null, "hiring_service_fit_source_url": null, '
    '"hiring_service_fit_excerpt": null, "classification_evidence": [], "research_evidence": []}'
)


def _fully_populated_response_text() -> str:
    data = json.loads(_FIRMOGRAPHICS_JSON)
    data.update({
        "funding": "$10M Series A",
        "funding_source_url": "https://acme.com/about",
        "funding_excerpt": "Acme raised a $10M Series A round",
        "leadership": [{
            "name": "A. Example", "title": "CEO",
            "source_url": "https://acme.com/about",
            "excerpt": "A. Example is the CEO of Acme",
        }],
        "hiring_service_fit": "Acme lists engineering roles",
        "hiring_service_fit_source_url": "https://acme.com/about",
        "hiring_service_fit_excerpt": "Acme lists engineering roles",
    })
    return json.dumps(data)


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
            "Acme is a SaaS company. Acme has 500 employees. Annual revenue is $50M."
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
    monkeypatch.setattr(discover, "LATE_RESPONSE_GRACE_SECONDS", 0.01)

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

    assert reason == "request_error"
    assert company.employee_count is None
    assert [row["provider"] for row in capture_provider_usage].count("gemini") == 1
    assert not any(row["provider"] in {"tavily", "groq"} for row in capture_provider_usage)


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

    assert asyncio.run(research.enrich_company_firmographics(company)) == "request_error"
    assert received == {"tavily_raw_requested": False, "groq_received_raw": False}


def test_quota_exhaustion_blocks_without_fallback(gemini_keys, monkeypatch, capture_provider_usage):
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
    monkeypatch.setattr(research, "_enrich_via_groq_tavily", lambda *_: pytest.fail("fallback called"))

    company = _company()
    with pytest.raises(Exception, match="quota-blocked"):
        asyncio.run(research.enrich_company_firmographics(company))
    assert company.employee_count is None
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
    assert company.research_outcomes["_summary"]["status"] == "complete_with_gaps"
    assert "funding" in company.research_outcomes["_summary"]["missing_fields"]


def test_full_grounded_response_is_complete(gemini_keys, monkeypatch):
    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return _fake_response(_fully_populated_response_text())

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    reason = asyncio.run(research.enrich_company_firmographics(company, run_id="run-full"))

    assert reason is None
    assert company.research_outcomes["_summary"]["status"] == "complete"
    assert company.research_outcomes["_missing_response_fields"] == {"missing": []}
    assert company.employee_count.value == 500
    assert company.revenue.value == "$50M"
    assert company.funding.value == "$10M Series A"
    assert company.website.url == "https://acme.com/about"
    assert company.employee_count.url == "https://acme.com/about"
    assert company.headquarters.url == "https://acme.com/about"
    assert company.revenue.url == "https://acme.com/about"


def test_omitted_required_key_is_partial_and_keeps_valid_facts(gemini_keys, monkeypatch):
    data = json.loads(_fully_populated_response_text())
    data.pop("leadership")

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return _fake_response(json.dumps(data))

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    reason = asyncio.run(research.enrich_company_firmographics(company, run_id="run-partial"))

    assert reason is None
    assert company.employee_count.value == 500
    assert company.research_outcomes["_summary"]["status"] == "partial"
    assert company.research_outcomes["_missing_response_fields"] == {
        "missing": ["leadership"],
    }
    assert "response_schema" in company.research_outcomes["_summary"]["blocking_fields"]


def test_explicit_null_is_reported_as_terminal_missing_information(gemini_keys, monkeypatch):
    data = json.loads(_fully_populated_response_text())
    data.update({
        "funding": None, "funding_source_url": None, "funding_excerpt": None,
        "employee_count": None, "employee_count_source_url": None, "employee_count_excerpt": None,
        "employee_range": None, "employee_range_source_url": None, "employee_range_excerpt": None,
        "revenue": None, "revenue_source_url": None, "revenue_excerpt": None,
    })

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return _fake_response(json.dumps(data))

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.research_outcomes["funding"]["status"] == "searched_but_not_found"
    assert "explicitly returned no reliable information" in company.research_outcomes["funding"]["reason"]
    assert company.research_outcomes["employee_range"]["reason"] == (
        "Gemini returned no supported employee count or employee-range claim"
    )
    assert company.research_outcomes["revenue"]["reason"] == "Gemini returned no supported revenue claim"
    assert company.research_outcomes["_summary"]["status"] == "complete_with_gaps"


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
    assert company.research_outcomes["_summary"]["status"] == "needs_review"
    assert "failure_reason" not in company.research_outcomes["_summary"]
    assert called["count"] == 0


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

    reason = asyncio.run(research.enrich_company_firmographics(company))
    # No usable provider output is a neutral research outcome, not a fabricated
    # success and not a raw execution-failure code: an empty response records
    # research_incomplete, while unparseable text is still a parse failure.
    assert reason in {None, "parse_error"}
    assert called["count"] == 0
    assert company.employee_count is None
    if not response_text:
        assert reason is None
        assert company.research_outcomes["_summary"]["status"] == "research_incomplete"
        assert company.research_outcomes["employee_range"]["status"] == "not_returned"
        assert company.research_outcomes["revenue"]["status"] == "not_returned"
        assert company.research_outcomes["employee_range"]["reason"] == "Gemini returned no text content"


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


def test_multiple_grounded_fallback_service_claims_are_retained():
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
    assert len(company.research_evidence) == 2
    assert company.research_outcomes["services"]["status"] == "accepted_and_mapped"


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

    assert asyncio.run(research.enrich_company_firmographics(company)) == "request_error"
    after = company.model_dump(mode="json")
    assert after["website"] == before["website"]
    assert after["employee_count"] == before["employee_count"]
    assert after["research_evidence"] == before["research_evidence"]
    assert company.research_outcomes["services"] == before["research_outcomes"]["services"]


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

    assert asyncio.run(research.enrich_company_firmographics(company)) == "request_error"
    assert company.employee_count.value == 42
    assert company.research_outcomes["employee_range"]["reason"] == (
        "provider call has not returned usable research results"
    )


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


def test_grounding_metadata_accepts_object_and_dictionary_shapes():
    object_response = SimpleNamespace(
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://www.acme.com:443/about/"))],
            grounding_supports=[SimpleNamespace(
                segment=SimpleNamespace(text="Acme has 500 employees."), grounding_chunk_indices=[0],
            )],
        ))],
    )
    dict_response = {
        "candidates": [{"groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://acme.com/about"}},
        ], "groundingSupports": [{
            "segment": {"text": "Acme has 500 employees."}, "groundingChunkIndices": [0],
        }]}}],
    }

    object_urls = research._extract_gemini_grounding_urls(object_response)
    dict_urls = research._extract_gemini_grounding_urls(dict_response)

    assert "https://acme.com/about" in object_urls
    assert "https://acme.com/about" in dict_urls
    assert research._normalize_grounding_url("https://www.acme.com:443/about/") == "https://acme.com/about"
    assert research._extract_gemini_grounding_supports(object_response) == {
        "https://acme.com/about": ["Acme has 500 employees."],
    }
    assert research._extract_gemini_grounding_supports(dict_response) == {
        "https://acme.com/about": ["Acme has 500 employees."],
    }


def test_grounding_metadata_accepts_installed_google_genai_response_models():
    from google.genai import types

    metadata = types.GroundingMetadata(
        grounding_chunks=[types.GroundingChunk(
            web=types.GroundingChunkWeb(uri="https://acme.com/about"),
        )],
        grounding_supports=[types.GroundingSupport(
            segment=types.Segment(text="Acme employs 500 people."),
            grounding_chunk_indices=[0],
        )],
    )
    response = types.GenerateContentResponse(candidates=[
        types.Candidate(grounding_metadata=metadata),
    ])

    assert research._extract_gemini_grounding_urls(response) == {
        "https://acme.com/about": "https://acme.com/about",
    }
    assert research._extract_gemini_grounding_supports(response) == {
        "https://acme.com/about": ["Acme employs 500 people."],
    }
    assert research._gemini_grounding_shape(response) == {
        "response_type": "GenerateContentResponse",
        "candidate_count": 1,
        "grounding_metadata_count": 1,
        "grounding_metadata_present": True,
        "grounding_metadata_empty": False,
        "grounding_metadata_types": ["GroundingMetadata"],
        "grounding_metadata_keys": ["grounding_chunks", "grounding_supports"],
        "grounding_chunk_count": 1,
        "grounding_support_count": 1,
        "grounding_support_segment_count": 1,
        "grounding_supported_url_count": 1,
        "web_search_query_count": 0,
    }


def test_multiple_grounded_services_and_leaders_are_not_conflicts():
    url = "https://acme.com/about"
    leadership = [
        {"name": "Alex Kim", "title": "CEO", "source_url": url,
         "excerpt": "Alex Kim is Acme's CEO."},
        {"name": "Sam Lee", "title": "CTO", "source_url": url,
         "excerpt": "Sam Lee is Acme's CTO."},
    ]
    data = {
        "services": ["identity security", "compliance automation"],
        "services_source_url": url,
        "services_excerpt": "Acme provides identity security and compliance automation.",
        "leadership": leadership,
        "research_evidence": [
            {
                "field": "services", "criterion": "company_services",
                "claim": {"service": "identity security"}, "source_url": url,
                "excerpt": "Acme provides identity security.",
            },
            {
                "field": "services", "criterion": "company_services",
                "claim": {"service": "compliance automation"}, "source_url": url,
                "excerpt": "Acme provides compliance automation.",
            },
        ],
    }
    supports = {url: [
        data["services_excerpt"],
        "Acme provides identity security.",
        "Acme provides compliance automation.",
        "Alex Kim is Acme's CEO.",
        "Sam Lee is Acme's CTO.",
    ]}
    company = _company()

    research._apply_firmographics(
        company, data, "gemini_grounded_search", {url}, grounding_supports=supports,
    )

    assert company.research_outcomes["services"]["status"] == "accepted_and_mapped"
    assert company.research_outcomes["leadership"]["status"] == "accepted_and_mapped"
    assert len([item for item in company.research_evidence if item["field"] == "services"]) == 2
    assert len([item for item in company.research_evidence if item["field"] == "leadership"]) == 2


@pytest.mark.parametrize(
    ("evidence_range", "evidence_excerpt", "expected_status"),
    [
        ("400-600 employees", "Acme employs 400-600 employees.", "accepted_and_mapped"),
        ("1,200 employees", "Acme employs 1,200 people.", "found_but_rejected"),
    ],
)
def test_employee_range_claims_use_numeric_compatibility(
    evidence_range, evidence_excerpt, expected_status,
):
    url = "https://acme.com/about"
    data = {
        "employee_count": 500,
        "employee_count_source_url": url,
        "employee_count_excerpt": "Acme employs 500 people.",
        "employee_range": "500 employees",
        "employee_range_source_url": url,
        "employee_range_excerpt": "Acme employs 500 people.",
        "research_evidence": [{
            "field": "employee_range", "criterion": "automatic_company_size",
            "claim": {"employee_range": evidence_range}, "source_url": url,
            "excerpt": evidence_excerpt,
        }],
    }
    company = _company()

    research._apply_firmographics(
        company, data, "gemini_grounded_search", {url},
        grounding_supports={url: [data["employee_count_excerpt"], evidence_excerpt]},
    )

    assert company.research_outcomes["employee_range"]["status"] == expected_status
    if expected_status == "found_but_rejected":
        assert company.employee_count is None
        assert company.research_outcomes["employee_range"]["reason"] == (
            "conflicting claims supplied for the same field"
        )
    else:
        assert company.employee_count.value == 500
        assert company.employee_count.url == url


def test_opposed_hiring_service_fit_claims_remain_conflicts():
    url = "https://acme.com/careers"
    company = _company()
    research._apply_firmographics(
        company,
        {
            "hiring_service_fit": "Acme is open to external contractors.",
            "research_evidence": [{
                "field": "hiring_service_fit", "criterion": "company_hiring_fit",
                "claim": {"hiring_service_fit": "Acme does not use external contractors."},
                "source_url": url,
                "excerpt": "Acme does not use external contractors.",
            }],
        },
        source="test_provider", allowed_source_urls={url},
        searched_fields={"hiring_service_fit"},
    )

    outcome = company.research_outcomes["hiring_service_fit"]
    assert outcome["status"] == "found_but_rejected"
    assert outcome["reason"] == "conflicting claims supplied for the same field"


def test_completeness_keeps_missing_and_conflicting_fields_company_local():
    company = _company()
    company.employee_count = ProvenanceValue(
        value=500, source="gemini_grounded_search", url="https://acme.com/about",
        method="reported", confidence="high",
    )
    company.research_outcomes = {
        field: {"status": "accepted_and_mapped", "criterion": criterion}
        for field, criterion in research._FIELD_CRITERIA.items()
    }
    company.research_outcomes["revenue"] = {
        "status": "found_but_rejected",
        "reason": "conflicting claims supplied for the same field",
    }

    summary = research.enrichment_completeness(company)

    assert summary["status"] == "needs_review"
    assert "conflicting claims" in summary["review_fields"]["revenue"]
    assert company.employee_count.value == 500

    company.research_outcomes["revenue"] = {
        "status": "searched_but_not_found",
        "reason": "No reliable revenue information was returned",
    }
    summary = research.enrichment_completeness(company)

    assert summary["status"] == "complete_with_gaps"
    assert summary["missing_fields"]["revenue"] == "No reliable revenue information was returned"
    assert summary["blocking_fields"] == {}
    assert company.employee_count.value == 500


def test_historical_failed_summary_is_presented_from_field_evidence():
    company = _company()
    company.employee_count = ProvenanceValue(
        value=500, source="gemini_grounded_search", url="https://acme.com/about",
        method="reported", confidence="high",
    )
    outcomes = {
        "employee_range": {"status": "accepted_and_mapped"},
        "revenue": {"status": "found_but_rejected", "reason": "supporting URL missing"},
        "funding": {"status": "searched_but_not_found", "reason": "No reliable information was returned"},
        "_summary": {"status": "failed", "failure_reason": "incomplete_result", "missing_fields": {}, "blocking_fields": {}},
    }

    presented = research.present_stored_enrichment_summary(outcomes)

    assert presented["_summary"]["status"] == "needs_review"
    assert "failure_reason" not in presented["_summary"]
    assert outcomes["_summary"]["status"] == "failed"
    assert company.employee_count.value == 500
    quota = research.present_stored_enrichment_summary({
        "_summary": {"status": "failed", "failure_reason": "quota_exhausted"},
    })
    assert quota["_summary"]["status"] == "failed"
    assert quota["_summary"]["failure_reason"] == "quota_exhausted"


def test_missing_supporting_url_is_field_review_not_company_failure():
    company = _company()
    research._apply_firmographics(
        company,
        {"revenue": "$50M"},
        source="gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"revenue"},
    )

    outcome = company.research_outcomes["revenue"]
    summary = research.enrichment_completeness(company)

    assert outcome["status"] == "found_but_rejected"
    # With no provider grounding support there is no URL that could be checked;
    # the reason names the actual cause rather than blaming a missing URL.
    assert "grounding support was unavailable" in outcome["reason"]
    assert summary["status"] == "needs_review"
    assert "revenue" in summary["review_fields"]


def test_grounding_rejects_model_url_without_metadata(gemini_keys, monkeypatch):
    response = SimpleNamespace(
        text='{"employee_count": 500, "employee_count_source_url": "https://model-only.example/about"}',
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri="https://acme.com/about"))],
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
    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    assert company.research_outcomes["_summary"]["status"] == "needs_review"
    assert company.employee_count is None


def test_grounding_url_without_support_association_is_rejected(gemini_keys, monkeypatch):
    response = _fake_response(_fully_populated_response_text())
    response.candidates[0].grounding_metadata.grounding_supports = []

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)
    events = []
    monkeypatch.setattr(research, "log_event", lambda event, **fields: events.append((event, fields)))
    company = _company()

    result = asyncio.run(research.enrich_company_firmographics(company))

    assert result is None
    assert company.research_outcomes["_summary"]["status"] == "needs_review"
    assert company.employee_count is None
    assert company.revenue is None
    assert all(
        outcome["status"] != "accepted_and_mapped"
        for field, outcome in company.research_outcomes.items()
        if field in research._FIELD_CRITERIA
    )
    outcomes = next(fields for event, fields in events if event == "gemini_enrichment_outcomes")
    assert outcomes["status"] == "needs_review"
    assert len(outcomes["rejected_fields"]) > 0


def test_mixed_grounded_and_ungrounded_claims_are_reported_as_partial(gemini_keys, monkeypatch, capsys):
    data = json.loads(_fully_populated_response_text())
    response_text = json.dumps(data)
    response = _fake_response(response_text)
    supported_excerpts = [
        data[key]
        for key in (
            "official_website_excerpt", "headquarters_excerpt", "company_type_excerpt",
            "employee_count_excerpt", "funding_excerpt", "industry_excerpt",
            "description_excerpt", "employee_range_excerpt", "services_excerpt",
            "hiring_service_fit_excerpt",
        )
    ]
    supported_excerpts.extend(person["excerpt"] for person in data["leadership"])
    response.candidates[0].grounding_metadata.grounding_supports = [
        SimpleNamespace(segment=SimpleNamespace(text=excerpt), grounding_chunk_indices=[0])
        for excerpt in supported_excerpts
    ]
    events = []
    monkeypatch.setattr(research, "log_event", lambda event, **fields: events.append((event, fields)))

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.research_outcomes["_summary"]["status"] == "needs_review"
    assert company.employee_count.value == 500
    assert company.revenue is None
    assert company.research_outcomes["revenue"]["status"] == "found_but_rejected"
    assert "grounding" in company.research_outcomes["revenue"]["reason"]
    outcomes = next(fields for event, fields in events if event == "gemini_enrichment_outcomes")
    assert outcomes["status"] == "needs_review"
    assert outcomes["field_outcomes"]["employee_range"]["status"] == "accepted"
    assert outcomes["field_outcomes"]["revenue"]["status"] == "rejected"
    assert "[OK] Enriched" not in capsys.readouterr().out


def test_empty_response_records_candidate_and_block_diagnostics(gemini_keys, monkeypatch):
    response = SimpleNamespace(
        text="", candidates=[SimpleNamespace(finish_reason="SAFETY")],
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
    )
    events = []
    monkeypatch.setattr(research, "log_event", lambda event, **fields: events.append((event, fields)))

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    assert asyncio.run(research.enrich_company_firmographics(company)) is None
    diagnostic = next(fields for event, fields in events if event == "gemini_empty_response")
    assert diagnostic["candidate_count"] == 1
    assert diagnostic["finish_reasons"] == ["SAFETY"]
    assert diagnostic["prompt_block_reason"] == "SAFETY"
    assert diagnostic["text_length"] == 0
    assert company.research_outcomes["_summary"]["status"] == "research_incomplete"
    assert "failure_reason" not in company.research_outcomes["_summary"]


def test_gemini_grounding_url_is_saved_with_fact_provenance(gemini_keys, monkeypatch):
    source_url = "https://acme.com/about"
    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**kwargs):
                return _fake_response(_FIRMOGRAPHICS_JSON)

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
    assert not tavily_rows


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

    assert reason == "request_error"


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

    assert reason == "request_error"


def test_no_gemini_key_goes_straight_to_fallback(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for i in range(2, 6):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")

    _patch_httpx_client(monkeypatch, _successful_fallback_handler)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "no_api_key"
    assert company.employee_count is None


def test_no_gemini_and_no_fallback_keys_reports_quota_exhausted(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for i in range(2, 6):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    company = _company()
    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason == "no_api_key"


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

    assert first_reason == "fallback_disabled"
    assert second_reason == "fallback_disabled"
    assert limited.is_set() is False
    assert requests == []


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

    assert reason == "request_error"
    assert calls["tavily"] == 0


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

    assert reason == "request_error"
    assert calls["groq"] == 0


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
    with pytest.raises(Exception, match="quota-blocked"):
        asyncio.run(research.enrich_company_firmographics(company))
    assert calls == ["test-key-1"]
    assert company.employee_count is None


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
    # Manual values are retained, but they are not this run's research.
    assert company.research_outcomes["official_website"]["status"] == "carried_forward"
    assert company.research_outcomes["headquarters"]["status"] == "searched_but_not_found"
    assert company.research_outcomes["employee_range"]["status"] == "carried_forward"
    # A bare industry string has no provenance, so it is a hint until verified.
    assert company.research_outcomes["industry"]["status"] == "discovery_hint_unverified"
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


def test_top_level_and_structured_fallback_service_claims_coexist():
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
    assert len(company.research_evidence) == 1
    outcome = company.research_outcomes["services"]
    assert outcome["status"] == "accepted_and_mapped"


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


def test_legacy_run_without_company_summaries_shows_neutral_counts_only():
    """A stored run from before per-company summaries must not leak raw codes."""
    presented = research.present_discover_result({
        "discovered": 4,
        "scored": 4,
        "enrich_failures": {"timeout": 1, "ungrounded_response": 3},
    })

    assert presented["enrich_failures"] == {"timeout": 1}
    assert presented["enrich_needs_review"] == 3
    assert "ungrounded_response" not in json.dumps(presented)
    assert "ungrounded_response" not in json.dumps(presented.get("enrich_failures"))


def test_run_summary_presents_validation_outcomes_as_review_not_failure():
    result = {
        "discovered": 4,
        "scored": 4,
        "enrichment_summary": {
            "wiz.io": {
                "status": "failed", "failure_reason": "ungrounded_response",
                "missing_fields": {"funding": "supporting URL missing or not present in provider results"},
                "blocking_fields": {"funding": "supporting URL missing or not present in provider results"},
                "review_fields": {"funding": "supporting URL missing or not present in provider results"},
            },
            "snyk.io": {
                "status": "failed", "failure_reason": "incomplete_result",
                "missing_fields": {"revenue": "No reliable information was returned"},
                "blocking_fields": {},
            },
            "other.io": {
                "status": "needs_review",
                "missing_fields": {}, "blocking_fields": {}, "review_fields": {},
            },
            "slow.io": {
                "status": "failed", "failure_reason": "timeout",
                "missing_fields": {}, "blocking_fields": {},
            },
        },
        "enrich_failures": {"timeout": 1, "ungrounded_response": 3},
    }

    presented = research.present_discover_result(result)
    summary = presented["enrichment_summary"]

    assert summary["wiz.io"]["status"] == "needs_review"
    assert summary["snyk.io"]["status"] == "partial"
    assert summary["other.io"]["status"] == "needs_review"
    assert summary["slow.io"]["status"] == "failed"
    assert summary["slow.io"]["failure_reason"] == "timeout"
    assert summary["wiz.io"]["missing_fields"] == {}
    assert summary["wiz.io"]["blocking_fields"] == {}
    assert "failure_reason" not in summary["wiz.io"]
    assert presented["enrich_failures"] == {"timeout": 1}
    assert presented["enrich_needs_review"] == 2
    assert presented["enrich_partial"] == 1
    assert "supporting URL" not in json.dumps(presented)
    # Stored records are not rewritten by presentation.
    assert result["enrichment_summary"]["wiz.io"]["status"] == "failed"


def test_operational_failure_is_not_relabelled_by_field_validation_text():
    result = {
        "enrichment_summary": {
            "slow.io": {
                "status": "failed",
                "failure_reason": "timeout",
                "missing_fields": {
                    "funding": "supporting URL missing or not present in provider results",
                },
                "blocking_fields": {},
                "review_fields": {},
            },
        },
        "enrich_failures": {"timeout": 1},
    }

    summary = research.present_discover_result(result)["enrichment_summary"]["slow.io"]

    assert summary["status"] == "failed"
    assert summary["failure_reason"] == "timeout"
    assert summary["missing_fields"] == {}
    assert summary["review_fields"] == {}


def _legacy_checkpoint(status: str, reason: str | None, outcomes: dict) -> dict:
    return {
        "status": status,
        "payload": {
            "reason": reason,
            "company": {"domain": "acme.com", "research_outcomes": outcomes},
            "completeness": outcomes.get("_summary") or {},
        },
    }


def test_legacy_ungrounded_checkpoint_without_usable_research_is_retried():
    outcomes = {
        "funding": {"status": "found_but_rejected", "reason": "supporting URL missing"},
        "_summary": {"status": "failed", "failure_reason": "ungrounded_response"},
    }

    assert discover._enrichment_checkpoint_is_reusable(
        _legacy_checkpoint("failed", "ungrounded_response", outcomes)
    ) is False


def test_legacy_ungrounded_checkpoint_with_usable_research_is_reused():
    outcomes = {
        "industry": {"status": "accepted_and_mapped"},
        "funding": {"status": "found_but_rejected", "reason": "supporting URL missing"},
        "_summary": {"status": "failed", "failure_reason": "ungrounded_response"},
    }

    assert discover._enrichment_checkpoint_is_reusable(
        _legacy_checkpoint("failed", "ungrounded_response", outcomes)
    ) is True


def test_ungrounded_checkpoint_with_classified_summary_is_reused():
    outcomes = {
        "employee_range": {"status": "accepted_and_mapped", "criterion": "automatic_company_size"},
        "revenue": {"status": "found_but_rejected", "reason": "supporting URL missing"},
        "_summary": {"status": "needs_review"},
    }

    assert discover._enrichment_checkpoint_is_reusable(
        _legacy_checkpoint("failed", "ungrounded_response", outcomes)
    ) is True


def test_research_incomplete_checkpoint_is_never_reused():
    outcomes = {"_summary": {"status": "research_incomplete"}}

    assert discover._enrichment_checkpoint_is_reusable(
        _legacy_checkpoint("done", None, outcomes)
    ) is False


def test_empty_response_checkpoint_carries_no_operational_failure_reason():
    outcomes = {"_summary": {"status": "research_incomplete"}}
    checkpoint = _legacy_checkpoint("done", None, outcomes)

    assert checkpoint["payload"]["completeness"]["status"] == "research_incomplete"
    assert discover._enrichment_checkpoint_is_reusable(checkpoint) is False


# --- Prose findings + grounding supports (Fix 1 and Fix 2) ------------------

def _prose_payload() -> dict:
    return {
        "official_website": "https://acme.com",
        "official_website_source_url": "https://acme.com",
        "official_website_excerpt": "Acme's official website is https://acme.com",
        "headquarters": {"city": "Austin", "state": "TX", "country": "United States"},
        "headquarters_source_url": "https://acme.com/contact",
        "headquarters_excerpt": "Acme is headquartered in Austin, TX, United States",
        "company_type": "Private Company",
        "company_type_source_url": "https://acme.com/about",
        "company_type_excerpt": "Acme is a privately held company",
        "business_category": "Cybersecurity",
        "business_category_source_url": "https://acme.com/about",
        "business_category_excerpt": "Acme is a cybersecurity company",
        "employee_count": 1200,
        "employee_count_source_url": "https://acme.com/about",
        "employee_count_excerpt": "Acme has approximately 1,200 employees",
        "revenue": "$50M",
        "revenue_source_url": "https://acme.com/investors",
        "revenue_excerpt": "Acme reports annual revenue of $50M",
        "funding": None, "funding_source_url": None, "funding_excerpt": None,
        "industry": "Cybersecurity",
        "industry_source_url": "https://acme.com/about",
        "industry_excerpt": "Acme operates in the cybersecurity industry",
        "description": "Acme provides workflow software for operations teams",
        "description_source_url": "https://acme.com/about",
        "description_excerpt": "Acme provides workflow software for operations teams",
        "employee_range": "1,200 employees",
        "employee_range_source_url": "https://acme.com/about",
        "employee_range_excerpt": "Acme has approximately 1,200 employees",
        "services": None, "services_source_url": None, "services_excerpt": None,
        "leadership": [],
        "hiring_service_fit": None,
        "hiring_service_fit_source_url": None,
        "hiring_service_fit_excerpt": None,
        "classification_evidence": [],
        "research_evidence": [],
    }


_PROSE_SENTENCES = (
    ("Official website: Acme's official website is https://acme.com (https://acme.com).", "https://acme.com"),
    (
        "Headquarters: Acme is headquartered in Austin, TX, United States "
        "(https://acme.com/contact).",
        "https://acme.com/contact",
    ),
    (
        "Company type: Acme is a privately held company (https://acme.com/about). "
        "Business category: Acme is a cybersecurity company (https://acme.com/about). "
        "Industry: Acme operates in the cybersecurity industry (https://acme.com/about).",
        "https://acme.com/about",
    ),
    (
        "Employee count: Acme has approximately 1,200 employees (https://acme.com/about).",
        "https://acme.com/about",
    ),
    (
        "Revenue: Acme reports annual revenue of $50M (https://acme.com/investors).",
        "https://acme.com/investors",
    ),
    (
        "Description: Acme provides workflow software for operations teams (https://acme.com/about).",
        "https://acme.com/about",
    ),
)


def _prose_response(payload: dict, sentences=_PROSE_SENTENCES) -> SimpleNamespace:
    """A response shaped like the prompt asks for: prose findings, then JSON."""
    text = "FINDINGS\n" + "\n".join(sentence for sentence, _ in sentences) + "\n\n" + json.dumps(payload)
    chunks: list[SimpleNamespace] = []
    index_by_url: dict[str, int] = {}
    supports: list[SimpleNamespace] = []
    for sentence, url in sentences:
        if url not in index_by_url:
            index_by_url[url] = len(chunks)
            chunks.append(SimpleNamespace(web=SimpleNamespace(uri=url)))
        supports.append(SimpleNamespace(
            segment=SimpleNamespace(text=sentence),
            grounding_chunk_indices=[index_by_url[url]],
        ))
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(
        grounding_metadata=SimpleNamespace(
            grounding_chunks=chunks, grounding_supports=supports,
        ),
    )])


def test_prose_findings_before_json_are_grounded_and_accepted(gemini_keys, monkeypatch):
    """The prompt's prose findings give Gemini text to attach supports to."""
    payload = _prose_payload()
    response = _prose_response(payload)

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)
    company = _company()

    reason = asyncio.run(research.enrich_company_firmographics(company))

    assert reason is None
    assert company.industry == "Cybersecurity"
    assert company.business_category.value == "Cybersecurity"
    assert company.business_category.url == "https://acme.com/about"
    assert company.company_type.value == "Private Company"
    assert company.employee_count.value == 1200
    assert company.revenue.value == "$50M"
    for field in ("industry", "business_category", "company_classification",
                  "headquarters", "official_website", "employee_range", "revenue"):
        assert company.research_outcomes[field]["status"] == "accepted_and_mapped", field
    assert research._extract_json_object(response.text)["business_category"] == "Cybersecurity"


def test_claim_is_stored_from_the_grounded_source_that_states_it():
    """The model's citation is not treated as the only possible source.

    When Google associates the claim with a different grounded source than the
    one the model cited, the claim is stored against the source that actually
    states it -- never against a page that does not.
    """
    company = _company()
    payload = {
        "business_category": "Cybersecurity",
        "business_category_source_url": "https://acme.com/about",
        "business_category_excerpt": "Acme is a cybersecurity company",
    }
    research._apply_firmographics(
        company, payload, "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about", "https://other.example/news"},
        searched_fields={"business_category"},
        grounding_supports={
            # The claim-bearing sentence is attached to a different source.
            "https://other.example/news": ["Acme is a cybersecurity company"],
            "https://acme.com/about": ["Acme publishes an annual security report"],
        },
    )

    assert company.business_category.value == "Cybersecurity"
    assert company.business_category.url == "https://other.example/news"
    outcome = company.research_outcomes["business_category"]
    assert outcome["status"] == "accepted_and_mapped"
    assert outcome["source_tier"] == 4


def test_negated_support_sentence_does_not_verify_a_claim():
    company = _company()
    payload = {
        "business_category": "Cybersecurity",
        "business_category_source_url": "https://acme.com/about",
        "business_category_excerpt": "Acme is a cybersecurity company",
    }
    research._apply_firmographics(
        company, payload, "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"business_category"},
        grounding_supports={
            "https://acme.com/about": ["Acme is not a cybersecurity company"],
        },
    )

    assert company.business_category is None
    assert company.research_outcomes["business_category"]["status"] == "found_but_rejected"


def test_a_value_inside_a_longer_word_does_not_support_a_claim():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "business_category": "AI",
            "business_category_source_url": "https://acme.com/about",
            "business_category_excerpt": "Acme runs AIServices for logistics",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"business_category"},
        grounding_supports={"https://acme.com/about": ["Acme runs AIServices for logistics"]},
    )

    assert company.business_category is None
    assert "provider grounding text does not state the claim" in company.research_outcomes["business_category"]["reason"]

# --- Carried-forward vs newly researched (Fix 3) ----------------------------

def test_prior_verified_value_is_carried_forward_not_accepted():
    company = Company(
        domain="acme.com", name="Acme",
        company_type={
            "value": "public company", "source": "gemini_grounded_search",
            "url": "https://acme.com/about", "method": "reported", "confidence": "high",
        },
    )
    research._apply_firmographics(
        company, {"company_type": None}, "gemini_grounded_search",
        allowed_source_urls=set(), searched_fields={"company_classification"},
    )

    outcome = company.research_outcomes["company_classification"]
    assert outcome["status"] == "carried_forward"
    assert company.company_type.value == "public company"
    summary = research.enrichment_completeness(company)
    assert summary["counts"]["verified"] == 0
    assert summary["counts"]["carried_forward"] == 1
    assert "company_classification" not in summary["missing_fields"]


def test_discovery_industry_hint_is_unverified_and_preserved_when_replaced():
    hint_only = Company(domain="acme.com", name="Acme", industry="Software")
    research._apply_firmographics(
        hint_only, {}, "gemini_grounded_search",
        allowed_source_urls=set(), searched_fields={"industry"},
    )

    outcome = hint_only.research_outcomes["industry"]
    assert outcome["status"] == "discovery_hint_unverified"
    assert outcome["discovery_hint"] == "Software"
    assert hint_only.industry == "Software"
    summary = research.enrichment_completeness(hint_only)
    assert summary["hint_fields"]["industry"].startswith("Discovery hint retained")
    assert summary["counts"]["verified"] == 0
    assert "industry" in summary["missing_fields"]

    verified = Company(domain="acme.com", name="Acme", industry="Software")
    research._apply_firmographics(
        verified,
        {
            "industry": "Cybersecurity",
            "industry_source_url": "https://acme.com/about",
            "industry_excerpt": "Acme operates in the cybersecurity industry",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"industry"},
        grounding_supports={"https://acme.com/about": ["Acme operates in the cybersecurity industry"]},
    )

    assert verified.industry == "Cybersecurity"
    assert verified.research_outcomes["industry"]["status"] == "accepted_and_mapped"
    assert verified.research_outcomes["industry"]["discovery_hint"] == "Software"
    assert research.enrichment_completeness(verified)["counts"]["verified"] == 1


def test_completeness_separates_verified_carried_and_hints():
    company = _company()
    company.research_outcomes = {
        "official_website": {"status": "accepted_and_mapped", "criterion": "automatic_website"},
        "industry": {"status": "discovery_hint_unverified", "discovery_hint": "Software"},
        "revenue": {"status": "carried_forward"},
        "funding": {"status": "searched_but_not_found", "reason": "not public"},
        "headquarters": {"status": "found_but_rejected", "reason": "supporting URL missing"},
    }

    summary = research.enrichment_completeness(company)

    assert summary["counts"]["verified"] == 1
    assert summary["counts"]["carried_forward"] == 1
    assert summary["counts"]["discovery_hints"] == 1
    assert summary["counts"]["searched_but_not_found"] == 1
    assert summary["counts"]["found_but_rejected"] == 1
    # Every field with no outcome recorded is blocking, not a research result.
    assert summary["counts"]["blocking"] == len(research._FIELD_CRITERIA) - 4
    assert summary["status"] == "needs_review"
    assert summary["missing_fields"]["funding"] == "not public"
# --- Writes must never silently drop research fields (Fix 5) ----------------

class _WriteConn:
    """Minimal PostgREST stand-in that fails the first N company writes."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        self.written: list[dict] = []
        self.attempts = 0

    def select(self, *_args, **_kwargs):
        return []

    def upsert(self, _table, rows, on_conflict):
        from csg.db import SupabaseError
        self.attempts += 1
        if self.errors:
            raise SupabaseError(self.errors.pop(0))
        self.written.extend(rows)
        return rows


_CHECK_CONSTRAINT_ERROR = (
    'POST /companies -> 400: {"code":"23514","message":"new row for relation companies '
    'violates check constraint companies_research_outcomes_check"}'
)
_MISSING_COLUMN_ERROR = (
    'POST /companies -> 404: {"code":"PGRST204","message":"Could not find the '
    "'research_outcomes' column of 'companies' in the schema cache\"}"
)


def test_company_write_does_not_drop_the_ledger_on_a_check_constraint_error(monkeypatch):
    from csg import db

    events: list[str] = []
    monkeypatch.setattr(db, "log_event", lambda event, **_fields: events.append(event))
    company = _company()
    company.research_outcomes = {"industry": {"status": "accepted_and_mapped"}}
    conn = _WriteConn([_CHECK_CONSTRAINT_ERROR])

    with pytest.raises(db.SupabaseError):
        db.upsert_company(conn, company)

    assert conn.attempts == 1
    assert conn.written == []
    assert events == []


def test_company_write_drops_a_genuinely_missing_column_but_reports_it(monkeypatch):
    from csg import db

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(db, "log_event", lambda event, **fields: events.append((event, fields)))
    conn = _WriteConn([_MISSING_COLUMN_ERROR])

    db.upsert_company(conn, _company())

    assert conn.attempts == 2
    assert len(conn.written) == 1
    assert "research_outcomes" not in conn.written[0]
    assert events[0][0] == "company_write_column_unavailable"
    assert events[0][1]["field"] == "research_outcomes"


def test_company_write_raises_instead_of_returning_after_failed_retries(monkeypatch):
    from csg import db

    monkeypatch.setattr(db, "log_event", lambda *_args, **_kwargs: None)
    conn = _WriteConn([_MISSING_COLUMN_ERROR] * 4)

    with pytest.raises(db.SupabaseError):
        db.upsert_company(conn, _company())

    assert conn.written == []

def test_real_source_url_is_accepted_through_its_resolved_grounding_chunk():
    """Gemini cites the chunk's real page; provenance stores that page, and the
    claim is still tied to the chunk Google attached it to."""
    company = _company()
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-a"
    real = "https://acme.com/about"
    research._apply_firmographics(
        company,
        {
            "business_category": "Cybersecurity",
            "business_category_source_url": real,
            "business_category_excerpt": "Acme is a cybersecurity company",
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk},
        searched_fields={"business_category"},
        grounding_supports={
            chunk: ["Business category: Acme is a cybersecurity company (grounding redirect)."],
        },
        grounding_redirects={chunk: real},
    )

    assert company.business_category.value == "Cybersecurity"
    assert company.business_category.url == real
    assert company.research_outcomes["business_category"]["status"] == "accepted_and_mapped"


def test_cited_url_is_rejected_when_the_grounded_chunk_resolves_elsewhere():
    company = _company()
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-b"
    research._apply_firmographics(
        company,
        {
            "business_category": "Cybersecurity",
            "business_category_source_url": "https://acme.com/about",
            "business_category_excerpt": "Acme is a cybersecurity company",
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk},
        searched_fields={"business_category"},
        grounding_supports={
            chunk: ["Business category: Acme is a cybersecurity company (grounding redirect)."],
        },
        grounding_redirects={chunk: "https://unrelated.example/news"},
    )

    assert company.business_category is None
    outcome = company.research_outcomes["business_category"]
    assert outcome["status"] == "found_but_rejected"
    # The cited page is not the chunk's resolved target, so it is not a provider source.
    assert "not present in provider results" in outcome["reason"]

def test_response_text_cannot_ground_a_claim_without_metadata(gemini_keys, monkeypatch):
    """A redirect URL the model writes in its own prose is not provider evidence.

    Google sometimes returns no grounding metadata. The model's FINDINGS line is
    generated text; it cannot establish which source supports which claim. The
    response must store nothing and ask for review instead.
    """
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-c"
    text = (
        "FINDINGS\n"
        f"Industry: Acme operates in the cybersecurity industry ({redirect}).\n"
        f"Headquarters: Acme is headquartered in Austin, TX ({redirect}).\n\n"
        + _fully_populated_response_text()
    )
    response = SimpleNamespace(text=text, candidates=[SimpleNamespace()])

    class FakeClient:
        def __init__(self, api_key, http_options=None, **kwargs):
            pass

        class models:
            @staticmethod
            def generate_content(**_kwargs):
                return response

    monkeypatch.setattr("google.genai.Client", FakeClient)
    events = []
    monkeypatch.setattr(research, "log_event", lambda event, **fields: events.append((event, fields)))
    company = _company()

    assert asyncio.run(research.enrich_company_firmographics(company)) is None

    assert company.industry is None
    assert company.headquarters is None
    assert company.employee_count is None
    assert company.revenue is None
    assert company.research_outcomes["_summary"]["status"] == "needs_review"
    for field in ("industry", "headquarters", "employee_range"):
        outcome = company.research_outcomes[field]
        assert outcome["status"] == "found_but_rejected"
        assert "grounding support was unavailable" in outcome["reason"]
    assert any(event == "gemini_grounding_unavailable" for event, _ in events)


def test_cited_deep_link_on_the_grounded_chunk_host_is_accepted():
    """The model often cites a deep page while the chunk resolves to the site
    root. Same site + claim in that chunk's support sentence is still grounded."""
    company = _company()
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-e"
    research._apply_firmographics(
        company,
        {
            "industry": "Cybersecurity",
            "industry_source_url": "https://www.acme.com/solutions/security",
            "industry_excerpt": "Acme operates in the cybersecurity industry",
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk},
        searched_fields={"industry"},
        grounding_supports={
            chunk: ["Industry: Acme operates in the cybersecurity industry (grounding redirect)."],
        },
        grounding_redirects={chunk: "https://acme.com"},
    )

    assert company.industry == "Cybersecurity"
    assert company.research_outcomes["industry"]["status"] == "accepted_and_mapped"
    # Provenance is the provider chunk's resolved page, not the model's deep link.
    assert company.research_evidence[0]["source_url"] == "https://acme.com"


def test_same_host_still_requires_the_claim_in_the_supporting_sentence():
    company = _company()
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-f"
    research._apply_firmographics(
        company,
        {
            "industry": "Cybersecurity",
            "industry_source_url": "https://www.acme.com/solutions/security",
            "industry_excerpt": "Acme operates in the cybersecurity industry",
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk},
        searched_fields={"industry"},
        grounding_supports={chunk: ["Acme publishes an annual threat report."]},
        grounding_redirects={chunk: "https://acme.com"},
    )

    assert company.industry is None
    assert "provider grounding text does not state the claim" in company.research_outcomes["industry"]["reason"]


def test_accepted_top_level_claim_persists_evidence_and_drops_stale_reason():
    """A rejected model evidence item must not strip provenance from a claim
    that passed the top-level check."""
    company = _company()
    source_url = "https://acme.com/about"
    research._apply_firmographics(
        company,
        {
            "industry": "Cybersecurity",
            "industry_source_url": source_url,
            "industry_excerpt": "Acme operates in the cybersecurity industry",
            "research_evidence": [{
                "field": "industry", "criterion": "company_industry",
                "claim": {"industry": "Cybersecurity"},
                "source_url": source_url,
                "excerpt": "Acme sells a completely different thing.",
            }],
        },
        "gemini_grounded_search",
        allowed_source_urls={source_url},
        searched_fields={"industry"},
        grounding_supports={
            source_url: ["Industry: Acme operates in the cybersecurity industry."],
        },
    )

    assert company.industry == "Cybersecurity"
    outcome = company.research_outcomes["industry"]
    assert outcome["status"] == "accepted_and_mapped"
    assert "reason" not in outcome
    industry_evidence = [item for item in company.research_evidence if item["field"] == "industry"]
    assert len(industry_evidence) == 1
    assert industry_evidence[0]["source_url"] == source_url
    # Evidence uses the provider segment that states the claim, not the
    # model's (here deliberately unrelated) excerpt.
    assert industry_evidence[0]["excerpt"] == "Industry: Acme operates in the cybersecurity industry."


def test_provider_segment_replaces_a_paraphrased_model_excerpt():
    """The model's excerpt is not required to be a verbatim quote when the
    provider's grounding segment itself states the claim. The stored evidence
    must be that provider segment, not the paraphrase."""
    company = _company()
    research._apply_firmographics(
        company,
        {
            "employee_count": 500,
            "employee_count_source_url": "https://acme.com/about",
            "employee_count_excerpt": "Acme's headcount is about five hundred people",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"employee_range"},
        grounding_supports={
            "https://acme.com/about": ["Acme has approximately 500 employees."],
        },
    )

    assert company.employee_count.value == 500
    assert company.research_outcomes["employee_range"]["status"] == "accepted_and_mapped"
    evidence = [item for item in company.research_evidence if item["field"] == "employee_range"]
    assert evidence and evidence[0]["excerpt"] == "Acme has approximately 500 employees."


def test_claim_without_provider_or_model_support_is_rejected():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "employee_count": 500,
            "employee_count_source_url": "https://acme.com/about",
            "employee_count_excerpt": "Acme's headcount is about five hundred people",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"employee_range"},
        grounding_supports={
            "https://acme.com/about": ["Acme publishes an annual security report."],
        },
    )

    assert company.employee_count is None
    assert company.research_outcomes["employee_range"]["status"] == "found_but_rejected"
    assert "provider grounding text does not state the claim" in (
        company.research_outcomes["employee_range"]["reason"]
    )


# --- text encoding guard ------------------------------------------------------

def test_replacement_character_headquarters_is_never_stored():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "headquarters": {"city": "S\ufffdo Paulo", "state": "S\ufffdo Paulo"},
            "headquarters_source_url": "https://acme.com/about",
            "headquarters_excerpt": "Acme is headquartered in S\ufffdo Paulo",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"headquarters"},
        grounding_supports={
            "https://acme.com/about": ["Acme is headquartered in S\ufffdo Paulo."],
        },
    )

    assert company.headquarters is None
    assert company.research_outcomes["headquarters"]["status"] == "found_but_rejected"
    assert "malformed or replacement characters" in company.research_outcomes["headquarters"]["reason"]


def test_mojibake_is_detected_but_a_clean_accent_is_not():
    assert research._text_is_malformed("S\ufffdo Paulo")
    assert research._text_is_malformed("S\u00c3\u00a3o Paulo")
    assert not research._text_is_malformed("S\u00e3o Paulo")


def test_clean_accented_headquarters_is_still_accepted():
    company = _company()
    research._apply_firmographics(
        company,
        {
            "headquarters": {"city": "S\u00e3o Paulo", "state": "S\u00e3o Paulo"},
            "headquarters_source_url": "https://acme.com/about",
            "headquarters_excerpt": "Acme is headquartered in S\u00e3o Paulo",
        },
        "gemini_grounded_search",
        allowed_source_urls={"https://acme.com/about"},
        searched_fields={"headquarters"},
        grounding_supports={
            "https://acme.com/about": ["Acme is headquartered in S\u00e3o Paulo."],
        },
    )

    assert company.headquarters.value["city"] == "S\u00e3o Paulo"
    assert company.research_outcomes["headquarters"]["status"] == "accepted_and_mapped"


# --- verified first-party domain aliases --------------------------------------

def test_verified_official_alias_is_tier_one_throughout_the_response():
    company = _company("nubank.com.br")
    disclosure = "https://nubank.com.br/about"
    alias = "https://nu.com"
    research._apply_firmographics(
        company,
        {
            "official_website": alias,
            "official_website_source_url": disclosure,
            "official_website_excerpt": "Nubank's official website is https://nu.com",
            "revenue": "$1.2B",
            "revenue_source_url": "https://nu.com/investors",
            "revenue_excerpt": "Nubank reported revenue of $1.2B",
        },
        "gemini_grounded_search",
        allowed_source_urls={disclosure, "https://nu.com/investors"},
        searched_fields={"official_website", "revenue"},
        grounding_supports={
            disclosure: ["Official website: Nubank's official website is https://nu.com."],
            "https://nu.com/investors": ["Nubank reported revenue of $1.2B."],
        },
    )

    assert company.website.value == alias
    assert company.research_outcomes["official_website"]["source_tier"] == 1
    assert company.research_outcomes["revenue"]["source_tier"] == 1
    evidence = [item for item in company.research_evidence if item["field"] == "revenue"]
    assert evidence and evidence[0]["source_tier"] == 1


def test_alias_is_not_verified_by_a_directory_or_its_own_page():
    company = _company("nubank.com.br")
    directory = "https://directory.example/nubank"
    research._apply_firmographics(
        company,
        {
            "official_website": "https://nu.com",
            "official_website_source_url": directory,
            "official_website_excerpt": "Nubank's website is https://nu.com",
        },
        "gemini_grounded_search",
        allowed_source_urls={directory},
        searched_fields={"official_website"},
        grounding_supports={
            directory: ["Nubank's official website is https://nu.com."],
        },
    )

    assert company.website is None
    assert company.research_outcomes["official_website"]["status"] == "found_but_rejected"
    assert "does not match the company domain" in company.research_outcomes["official_website"]["reason"]


def test_alias_requires_a_provider_segment_that_states_the_url():
    company = _company("nubank.com.br")
    disclosure = "https://nubank.com.br/about"
    research._apply_firmographics(
        company,
        {
            "official_website": "https://nu.com",
            "official_website_source_url": disclosure,
            "official_website_excerpt": "Nubank's official website is https://nu.com",
        },
        "gemini_grounded_search",
        allowed_source_urls={disclosure},
        searched_fields={"official_website"},
        grounding_supports={
            disclosure: ["Nubank publishes an annual report."],
        },
    )

    assert company.website is None
    assert company.research_outcomes["official_website"]["status"] == "found_but_rejected"


# --- source-quality preference ------------------------------------------------

def test_best_tier_grounded_source_wins_for_stored_provenance():
    company = _company("posthog.com")
    aggregator = "https://exa.ai/posthog"
    official = "https://posthog.com/about"
    claim = "PostHog is headquartered in San Francisco, CA, United States."
    research._apply_firmographics(
        company,
        {
            "headquarters": {
                "city": "San Francisco", "state": "CA", "country": "United States",
            },
            "headquarters_source_url": aggregator,
            "headquarters_excerpt": claim,
        },
        "gemini_grounded_search",
        allowed_source_urls={aggregator, official},
        searched_fields={"headquarters"},
        grounding_supports={aggregator: [claim], official: [claim]},
    )

    assert company.headquarters.value["city"] == "San Francisco"
    assert company.headquarters.url == official
    assert company.research_outcomes["headquarters"]["source_tier"] == 1
    evidence = [item for item in company.research_evidence if item["field"] == "headquarters"]
    assert evidence and evidence[0]["source_url"] == official


# --- source-quality tiering ---------------------------------------------------


def test_source_quality_tiers_follow_the_prompt_priority_order():
    assert research._source_quality_tier("https://acme.com/about", "acme.com") == 1
    assert research._source_quality_tier("https://ir.acme.com/news", "acme.com") == 1
    assert research._source_quality_tier("https://www.prnewswire.com/news/acme", "acme.com") == 2
    assert research._source_quality_tier("https://www.sec.gov/Archives/edgar", "acme.com") == 2
    assert research._source_quality_tier("https://www.linkedin.com/company/acme", "acme.com") == 3
    assert research._source_quality_tier("https://www.reuters.com/business/acme", "acme.com") == 3
    assert research._source_quality_tier("https://www.reddit.com/r/cscareerquestions", "acme.com") == 4
    assert research._source_quality_tier("not-a-url", "acme.com") == 4


def test_tier_cap_never_upgrades_a_low_confidence_claim():
    assert research._tier_capped_confidence("high", 1) == "high"
    assert research._tier_capped_confidence("high", 3) == "medium"
    assert research._tier_capped_confidence("high", 4) == "low"
    assert research._tier_capped_confidence("low", 1) == "low"
    assert research._tier_capped_confidence("nonsense", 4) == "low"


def test_evidence_from_an_unclassified_source_is_capped_and_tagged():
    company = _company()
    forum_url = "https://reddit.com/r/sales/comments/acme"
    research._apply_firmographics(
        company,
        {
            "services": "Acme offers implementation services",
            "services_source_url": forum_url,
            "services_excerpt": "Acme offers implementation services for mid-market teams.",
            "research_evidence": [{
                "field": "services", "criterion": "company_services",
                "claim": {"services": "Acme offers implementation services"},
                "source_url": forum_url,
                "excerpt": "Acme offers implementation services for mid-market teams.",
                "confidence": "high", "evidence_type": "direct",
            }],
        },
        "gemini_grounded_search",
        allowed_source_urls={forum_url},
        searched_fields={"services"},
        grounding_supports={
            forum_url: ["Acme offers implementation services for mid-market teams."],
        },
    )

    evidence = [item for item in company.research_evidence if item["field"] == "services"]
    assert len(evidence) == 1
    assert evidence[0]["source_tier"] == 4
    assert evidence[0]["confidence"] == "low"
    assert company.research_outcomes["services"]["source_tier"] == 4


def test_official_source_keeps_high_confidence_and_records_the_tier():
    company = _company()
    official_url = "https://acme.com/team"
    research._apply_firmographics(
        company,
        {
            "leadership": [{
                "name": "Jane Doe", "title": "CEO",
                "source_url": official_url,
                "excerpt": "Jane Doe is the CEO of Acme.",
            }],
            "research_evidence": [{
                "field": "leadership", "criterion": "company_leadership",
                "claim": {"name": "Jane Doe", "title": "CEO"},
                "source_url": official_url,
                "excerpt": "Jane Doe is the CEO of Acme.",
                "confidence": "high", "evidence_type": "direct",
            }],
        },
        "gemini_grounded_search",
        allowed_source_urls={official_url},
        searched_fields={"leadership"},
        grounding_supports={official_url: ["Jane Doe is the CEO of Acme."]},
    )

    leadership_evidence = [
        item for item in company.research_evidence if item["field"] == "leadership"
    ]
    assert leadership_evidence
    assert all(item["source_tier"] == 1 for item in leadership_evidence)
    assert all(item["confidence"] == "high" for item in leadership_evidence)
    assert company.research_outcomes["leadership"]["source_tier"] == 1


# --- financial metric semantics ----------------------------------------------


def test_financial_value_rejects_a_valuation_presented_as_funding():
    valid, reason = research._financial_value_is_valid(
        "funding", "$4.85B", "Acme was valued at $4.85B in its latest round.",
    )
    assert valid is False
    assert "valuation" in reason


def test_financial_value_rejects_a_funding_round_presented_as_revenue():
    valid, reason = research._financial_value_is_valid(
        "revenue", "$50M", "Acme raised $50M in seed funding.",
    )
    assert valid is False
    assert "funding" in reason


def test_financial_value_rejects_unlabelled_arr_as_revenue_but_keeps_labelled_arr():
    rejected, reason = research._financial_value_is_valid(
        "revenue", "$200M", "Acme reports $200M in ARR.",
    )
    assert rejected is False
    assert "annual recurring revenue" in reason

    accepted, _ = research._financial_value_is_valid(
        "revenue", "$200M ARR", "Acme reports $200M ARR.",
    )
    assert accepted is True


def test_financial_value_accepts_matching_metrics_and_plain_amounts():
    assert research._financial_value_is_valid(
        "revenue", "$100M", "Acme reports annual revenue of $100M.",
    ) == (True, "")
    assert research._financial_value_is_valid(
        "funding", "$10M Series A", "Acme raised a $10M Series A round.",
    ) == (True, "")
    assert research._financial_value_is_valid("revenue", "$1.2B", "") == (True, "")


def test_valuation_is_not_persisted_as_funding_and_the_reason_is_recorded():
    company = _company()
    url = "https://acme.com/news"
    research._apply_firmographics(
        company,
        {
            "funding": "$4.85B",
            "funding_source_url": url,
            "funding_excerpt": "Acme was valued at $4.85B in its latest round.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"funding"},
        grounding_supports={url: ["Acme was valued at $4.85B in its latest round."]},
    )

    assert company.funding is None
    outcome = company.research_outcomes["funding"]
    assert outcome["status"] == "found_but_rejected"
    assert "valuation" in outcome["reason"]


def test_funding_round_is_not_persisted_as_revenue_and_the_reason_is_recorded():
    company = _company()
    url = "https://acme.com/press"
    research._apply_firmographics(
        company,
        {
            "revenue": "$50M",
            "revenue_source_url": url,
            "revenue_excerpt": "Acme raised $50M in seed funding.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"revenue"},
        grounding_supports={url: ["Acme raised $50M in seed funding."]},
    )

    assert company.revenue is None
    outcome = company.research_outcomes["revenue"]
    assert outcome["status"] == "found_but_rejected"
    assert "funding" in outcome["reason"]


def test_matching_revenue_claim_is_still_persisted_with_provenance():
    company = _company()
    url = "https://acme.com/investors"
    research._apply_firmographics(
        company,
        {
            "revenue": "$100M",
            "revenue_source_url": url,
            "revenue_excerpt": "Acme reports annual revenue of $100M for fiscal year 2025.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"revenue"},
        grounding_supports={
            url: ["Acme reports annual revenue of $100M for fiscal year 2025."],
        },
    )

    assert company.revenue is not None
    assert company.revenue.value == "$100M"
    assert company.revenue.url == url
    assert company.research_outcomes["revenue"]["status"] == "accepted_and_mapped"


# --- leadership normalization -------------------------------------------------


def test_leadership_objects_keep_only_name_and_title_plus_evidence_fields():
    people = research._normalize_leadership([
        {"name": " Jane Doe ", "title": "CEO", "confidence": "high",
         "source_url": "https://acme.com/team", "excerpt": "Jane Doe is the CEO of Acme."},
    ])
    assert people == [{
        "name": "Jane Doe", "title": "CEO",
        "source_url": "https://acme.com/team",
        "excerpt": "Jane Doe is the CEO of Acme.",
    }]


def test_leadership_drops_entries_missing_a_name_or_a_title():
    people = research._normalize_leadership([
        {"name": "Jane Doe"}, {"title": "CEO"}, {"name": "", "title": "CTO"}, None,
    ])
    assert people == []


def test_leadership_accepts_alternative_keys_and_deduplicates():
    people = research._normalize_leadership([
        {"person": "Jane Doe", "role": "Chief Executive Officer"},
        {"full_name": "jane doe", "position": "chief executive officer"},
        {"name": "John Roe", "title": "CTO"},
    ])
    assert [person["name"] for person in people] == ["Jane Doe", "John Roe"]
    assert [person["title"] for person in people] == ["Chief Executive Officer", "CTO"]


def test_leadership_strings_are_split_only_when_the_title_is_explicit():
    people = research._normalize_leadership([
        "Jane Doe - CEO",
        "John Roe, Chief Technology Officer",
        "Chief Operating Officer - Ann Poe",
        "Jane Doe",
        "Sales Team",
    ])
    assert people == [
        {"name": "Jane Doe", "title": "CEO"},
        {"name": "John Roe", "title": "Chief Technology Officer"},
        {"name": "Ann Poe", "title": "Chief Operating Officer"},
    ]


def test_leadership_evidence_uses_the_normalized_shape():
    company = _company()
    url = "https://acme.com/team"
    research._apply_firmographics(
        company,
        {
            "leadership": [
                {"name": "Jane Doe", "title": "CEO", "source_url": url,
                 "excerpt": "Jane Doe is the CEO of Acme."},
                {"name": "No Title"},
                "John Roe - CTO",
                "Sales Team",
            ],
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"leadership"},
        grounding_supports={url: ["Jane Doe is the CEO of Acme."]},
    )

    claims = [
        item["claim"] for item in company.research_evidence
        if item["field"] == "leadership"
    ]
    assert claims == [{"name": "Jane Doe", "title": "CEO"}]
    assert company.research_outcomes["leadership"]["status"] == "accepted_and_mapped"


# --- evidence synthesis for every accepted field ------------------------------


_EVIDENCE_PAYLOAD = {
    "official_website": "https://acme.com",
    "official_website_source_url": "https://acme.com/about",
    "official_website_excerpt": "Acme's official website is https://acme.com.",
    "headquarters": {"city": "Austin", "state": "TX", "country": "United States"},
    "headquarters_source_url": "https://acme.com/about",
    "headquarters_excerpt": "Acme is headquartered in Austin, TX, United States.",
    "company_type": "private company",
    "company_type_source_url": "https://acme.com/about",
    "company_type_excerpt": "Acme is a private company.",
    "business_category": "SaaS",
    "business_category_source_url": "https://acme.com/about",
    "business_category_excerpt": "Acme is a SaaS company.",
    "employee_count": 500,
    "employee_count_source_url": "https://acme.com/about",
    "employee_count_excerpt": "Acme has 500 employees.",
    "revenue": "$50M",
    "revenue_source_url": "https://acme.com/investors",
    "revenue_excerpt": "Acme reports annual revenue of $50M.",
    "funding": "$10M Series A",
    "funding_source_url": "https://acme.com/news",
    "funding_excerpt": "Acme raised a $10M Series A round.",
    "industry": "Cybersecurity",
    "industry_source_url": "https://acme.com/about",
    "industry_excerpt": "Acme operates in the cybersecurity industry.",
    "description": "Acme provides workflow software for operations teams.",
    "description_source_url": "https://acme.com/about",
    "description_excerpt": "Acme provides workflow software for operations teams.",
    "services": "Acme provides software implementation services",
    "services_source_url": "https://acme.com/services",
    "services_excerpt": "Acme provides software implementation services.",
    "hiring_service_fit": "Acme lists engineering roles on its careers page",
    "hiring_service_fit_source_url": "https://acme.com/careers",
    "hiring_service_fit_excerpt": "Acme lists engineering roles on its careers page.",
    "leadership": [{
        "name": "Jane Doe", "title": "CEO",
        "source_url": "https://acme.com/team",
        "excerpt": "Jane Doe is the CEO of Acme.",
    }],
}

_EVIDENCE_GROUNDING = {
    "https://acme.com/about": [
        "Acme's official website is https://acme.com.",
        "Acme is headquartered in Austin, TX, United States.",
        "Acme is a private company.",
        "Acme is a SaaS company.",
        "Acme has 500 employees.",
        "Acme operates in the cybersecurity industry.",
        "Acme provides workflow software for operations teams.",
    ],
    "https://acme.com/investors": ["Acme reports annual revenue of $50M."],
    "https://acme.com/news": ["Acme raised a $10M Series A round."],
    "https://acme.com/services": ["Acme provides software implementation services."],
    "https://acme.com/careers": ["Acme lists engineering roles on its careers page."],
    "https://acme.com/team": ["Jane Doe is the CEO of Acme."],
}


def _apply_evidence_payload(payload: dict, company: Company | None = None) -> Company:
    company = company or _company()
    research._apply_firmographics(
        company,
        payload,
        "gemini_grounded_search",
        allowed_source_urls=set(_EVIDENCE_GROUNDING),
        searched_fields=set(research._FIELD_CRITERIA),
        grounding_supports=_EVIDENCE_GROUNDING,
    )
    return company


def test_every_accepted_field_persists_evidence_with_source_tier():
    company = _apply_evidence_payload(_EVIDENCE_PAYLOAD)

    evidenced = {item["field"] for item in company.research_evidence}
    assert evidenced == set(research._FIELD_CRITERIA)

    for item in company.research_evidence:
        assert item["source_url"].startswith("https://acme.com/")
        assert item["excerpt"].strip()
        assert item["source_tier"] == 1
        assert item["criterion"] == research._FIELD_CRITERIA[item["field"]]
        outcome = company.research_outcomes[item["field"]]
        assert outcome["status"] in {"accepted_and_mapped", "carried_forward"}
        assert item["evidence_key"] in outcome["evidence_keys"]


def test_rejected_value_does_not_synthesise_evidence_for_that_field():
    company = _company()
    url = "https://acme.com/news"
    research._apply_firmographics(
        company,
        {
            "funding": "$4.85B",
            "funding_source_url": url,
            "funding_excerpt": "Acme was valued at $4.85B in its latest round.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"funding"},
        grounding_supports={url: ["Acme was valued at $4.85B in its latest round."]},
    )

    assert not [item for item in company.research_evidence if item["field"] == "funding"]


def test_carried_value_does_not_accumulate_new_evidence():
    company = _company()
    company.revenue = ProvenanceValue(
        value="$40M", source="gemini_grounded_search",
        url="https://acme.com/investors", confidence="medium",
    )
    url = "https://acme.com/investors"
    research._apply_firmographics(
        company,
        {
            "revenue": "$50M",
            "revenue_source_url": url,
            "revenue_excerpt": "Acme reports annual revenue of $50M.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"revenue"},
        grounding_supports={url: ["Acme reports annual revenue of $50M."]},
    )

    assert company.revenue.value == "$40M"
    assert company.research_outcomes["revenue"]["status"] == "carried_forward"
    assert not [item for item in company.research_evidence if item["field"] == "revenue"]


# --- grounding redirect resolution -------------------------------------------


class _RedirectClient:
    """Minimal stand-in for httpx.Client's redirector calls."""

    instances: list["_RedirectClient"] = []

    def __init__(self, location: str, delay: float = 0.0, status: int = 302):
        self.location = location
        self.delay = delay
        self.status = status
        self.calls: list[str] = []
        _RedirectClient.instances.append(self)

    def _respond(self, url: str):
        self.calls.append(url)
        if self.delay:
            # threading.Event.wait, not time.sleep: the provider-usage fixture
            # patches time.sleep process-wide.
            threading.Event().wait(self.delay)
        return SimpleNamespace(status_code=self.status, headers={"location": self.location})

    def head(self, url):
        return self._respond(url)

    def get(self, url):
        return self._respond(url)

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _clear_grounding_redirect_cache():
    research._GROUNDING_REDIRECT_CACHE.clear()
    _RedirectClient.instances.clear()
    yield
    research._GROUNDING_REDIRECT_CACHE.clear()
    _RedirectClient.instances.clear()


def _install_redirect_client(
    monkeypatch, location: str, delay: float = 0.0, status: int = 302,
) -> None:
    def factory(*_args, **_kwargs):
        return _RedirectClient(location, delay=delay, status=status)

    monkeypatch.setattr(research.httpx, "Client", factory)


def test_grounding_redirect_is_resolved_once_and_reused_from_cache(monkeypatch):
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-cache"
    _install_redirect_client(monkeypatch, "https://acme.com/about")

    first = research._resolve_grounding_urls({chunk})
    second = research._resolve_grounding_urls({chunk})

    assert first == {"https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-cache": "https://acme.com/about"}
    assert second == first
    assert len(_RedirectClient.instances) == 1
    assert len(_RedirectClient.instances[0].calls) == 1


def test_grounding_redirect_urls_are_deduplicated_before_resolution(monkeypatch):
    base = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-dup"
    _install_redirect_client(monkeypatch, "https://acme.com/about")

    resolved = research._resolve_grounding_urls({base, f"{base}/", f"{base}#fragment"})

    assert list(resolved.values()) == ["https://acme.com/about"]
    assert len(_RedirectClient.instances[0].calls) == 1


def test_relative_redirect_location_is_resolved_against_the_redirector(monkeypatch):
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-rel"
    _install_redirect_client(monkeypatch, "/grounding-api-redirect/resolved-page")

    resolved = research._resolve_grounding_urls({chunk})

    assert resolved == {
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-rel":
            "https://vertexaisearch.cloud.google.com/grounding-api-redirect/resolved-page",
    }


def test_redirect_resolution_stops_at_its_total_budget(monkeypatch):
    import time as _real_time

    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-slow"
    _install_redirect_client(monkeypatch, "https://acme.com/about", delay=1.0)
    monkeypatch.setattr(research, "_GROUNDING_RESOLVE_BUDGET_SECONDS", 0.2)

    started = _real_time.monotonic()
    resolved = research._resolve_grounding_urls({chunk})
    elapsed = _real_time.monotonic() - started

    assert resolved == {}
    assert elapsed < 1.0


def test_failed_redirect_resolution_is_not_cached_and_keeps_the_chunk_url(monkeypatch):
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-fail"
    _install_redirect_client(monkeypatch, "", status=404)

    assert research._resolve_grounding_urls({chunk}) == {}
    assert research._GROUNDING_REDIRECT_CACHE == {}


def test_stored_value_confidence_is_capped_by_source_tier():
    """An unclassified source must not leave a stored value labelled high."""
    company = _company()
    url = "https://sacra.com/c/acme"
    research._apply_firmographics(
        company,
        {
            "funding": "$10M Series A",
            "funding_source_url": url,
            "funding_excerpt": "Acme raised a $10M Series A round.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"funding"},
        grounding_supports={url: ["Acme raised a $10M Series A round."]},
    )

    assert company.funding.value == "$10M Series A"
    assert company.funding.confidence == "low"
    assert company.research_outcomes["funding"]["source_tier"] == research.SOURCE_TIER_UNCLASSIFIED


def test_stored_value_confidence_is_kept_for_a_first_party_source():
    company = _company()
    url = "https://acme.com/news"
    research._apply_firmographics(
        company,
        {
            "funding": "$10M Series A",
            "funding_source_url": url,
            "funding_excerpt": "Acme raised a $10M Series A round.",
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"funding"},
        grounding_supports={url: ["Acme raised a $10M Series A round."]},
    )

    assert company.funding.confidence == "high"
    assert company.research_outcomes["funding"]["source_tier"] == research.SOURCE_TIER_OFFICIAL


def test_value_evidence_is_added_when_the_model_cited_a_different_source_for_the_field():
    """A stored number needs evidence for its own source, not just for the
    field's other claims: linear.app's 118 headcount came from a directory while
    the model's employee-range evidence cited a different site."""
    company = _company()
    value_url = "https://getlatka.com/companies/acme"
    other_url = "https://rivalsense.co/intel/acme"
    research._apply_firmographics(
        company,
        {
            "employee_count": 118,
            "employee_count_source_url": value_url,
            "employee_count_excerpt": "Acme has 118 employees.",
            "research_evidence": [{
                "field": "employee_range", "criterion": "automatic_company_size",
                "claim": {"employee_range": "51-200 employees"},
                "source_url": other_url,
                "excerpt": "Acme has 51-200 employees.",
            }],
        },
        "gemini_grounded_search",
        allowed_source_urls={value_url, other_url},
        searched_fields={"employee_range"},
        grounding_supports={
            value_url: ["Acme has 118 employees."],
            other_url: ["Acme has 51-200 employees."],
        },
    )

    assert company.employee_count.value == 118
    claims = [item for item in company.research_evidence if item["field"] == "employee_range"]
    urls = {research._normalize_grounding_url(item["source_url"]) for item in claims}
    assert urls == {
        research._normalize_grounding_url(value_url),
        research._normalize_grounding_url(other_url),
    }
    assert any(
        item["claim"] == {"employee_range": 118}
        and research._normalize_grounding_url(item["source_url"])
        == research._normalize_grounding_url(value_url)
        for item in claims
    )
    outcome = company.research_outcomes["employee_range"]
    assert outcome["status"] == "accepted_and_mapped"
    assert set(outcome["evidence_keys"]) == {item["evidence_key"] for item in claims}


def test_value_evidence_for_the_same_claim_is_not_duplicated():
    company = _company()
    url = "https://acme.com/about"
    item = {
        "field": "official_website", "criterion": "official_website",
        "claim": {"official_website": "https://acme.com"},
        "source_url": url,
        "excerpt": "Acme's official website is https://acme.com.",
    }
    research._apply_firmographics(
        company,
        {
            "official_website": "https://acme.com",
            "official_website_source_url": url,
            "official_website_excerpt": "Acme's official website is https://acme.com.",
            "research_evidence": [dict(item)],
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"official_website"},
        grounding_supports={url: ["Acme's official website is https://acme.com."]},
    )

    assert len([i for i in company.research_evidence if i["field"] == "official_website"]) == 1


def test_evidence_tier_uses_the_resolved_page_not_googles_redirect():
    """linear.app's own pages were graded tier 4 because the tier was computed
    from the opaque grounding redirect the model cited."""
    company = _company()
    chunk = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-tier"
    real = "https://acme.com/about"
    research._apply_firmographics(
        company,
        {
            "business_category": "Cybersecurity",
            "business_category_source_url": chunk,
            "business_category_excerpt": "Acme is a cybersecurity company",
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk},
        searched_fields={"business_category"},
        grounding_supports={
            chunk: ["Business category: Acme is a cybersecurity company (grounding redirect)."],
        },
        grounding_redirects={chunk: real},
    )

    assert company.business_category.url == real
    item = next(
        i for i in company.research_evidence if i["field"] == "business_category"
    )
    assert research._normalize_grounding_url(item["source_url"]) == real
    assert item["source_tier"] == research.SOURCE_TIER_OFFICIAL
    assert company.business_category.confidence != "low"
    assert company.research_outcomes["business_category"]["source_tier"] == research.SOURCE_TIER_OFFICIAL


def test_two_grounding_chunks_for_one_page_store_one_evidence_item():
    """posthog.com stored the same claim twice: Gemini cited two redirect tokens
    that both resolved to one article."""
    company = _company()
    chunk_a = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-a"
    chunk_b = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-b"
    real = "https://contrary.com/company/acme"
    item = {
        "field": "employee_range", "criterion": "automatic_company_size",
        "claim": {"employee_range": "101-250 employees"},
        "source_url": None, "excerpt": "Acme has 101-250 employees.",
    }
    research._apply_firmographics(
        company,
        {
            "employee_count": 240,
            "employee_count_source_url": real,
            "employee_count_excerpt": "Acme has 240 employees, in the 101-250 band.",
            "research_evidence": [dict(item, source_url=chunk_a), dict(item, source_url=chunk_b)],
        },
        "gemini_grounded_search",
        allowed_source_urls={chunk_a, chunk_b},
        searched_fields={"employee_range"},
        grounding_supports={
            chunk_a: [
                "Acme has 240 employees, in the 101-250 band.",
                "Acme has 101-250 employees.",
            ],
            chunk_b: [
                "Acme has 240 employees, in the 101-250 band.",
                "Acme has 101-250 employees.",
            ],
        },
        grounding_redirects={chunk_a: real, chunk_b: real},
    )

    items = [i for i in company.research_evidence if i["field"] == "employee_range"]
    band = [i for i in items if i["claim"] == {"employee_range": "101-250 employees"}]
    assert len(band) == 1
    assert research._normalize_grounding_url(band[0]["source_url"]) == real


def test_call_to_action_claim_is_rejected_as_a_company_description():
    company = _company()
    url = "https://acme.com/careers"
    claim = "Check out acme.com/careers if you want to be part of this journey"
    research._apply_firmographics(
        company,
        {"research_evidence": [{
            "field": "hiring_service_fit", "criterion": "company_hiring_fit",
            "claim": {"hiring_service_fit": claim},
            "source_url": url, "excerpt": claim + ".",
        }]},
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"hiring_service_fit"},
        grounding_supports={url: [claim + "."]},
    )

    outcome = company.research_outcomes["hiring_service_fit"]
    assert outcome["status"] == "found_but_rejected"
    assert "call to action" in outcome["reason"]
    assert not [i for i in company.research_evidence if i["field"] == "hiring_service_fit"]


def test_ampersand_and_and_are_the_same_claim():
    assert research._claim_matches_excerpt(
        {"name": "Ivan Zhao", "title": "Co-Founder & CEO"},
        "Ivan Zhao is the co-founder and CEO of PostHog.",
    )
    assert research._claim_matches_excerpt(
        {"name": "Ivan Zhao", "title": "Co-Founder and CEO"},
        "Ivan Zhao is the co-founder & CEO of PostHog.",
    )


def test_abbreviated_title_matches_its_spelled_out_form():
    assert research._claim_matches_excerpt(
        {"name": "Michael Nathanson", "title": "CEO"},
        "Michael Nathanson, Chief Executive Officer of Semrush.",
    )
    assert research._claim_matches_excerpt(
        {"name": "Aida Sadeghi", "title": "CTO"},
        "Aida Sadeghi serves as chief technology officer.",
    )


def test_leadership_evidence_accepts_an_abbreviated_title():
    company = _company()
    url = "https://acme.com/leadership"
    research._apply_firmographics(
        company,
        {"research_evidence": [{
            "field": "leadership", "criterion": "company_leadership",
            "claim": {"name": "Jane Doe", "title": "CEO"},
            "source_url": url,
            "excerpt": "Jane Doe, Chief Executive Officer of Acme.",
        }]},
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"leadership"},
        grounding_supports={url: ["Jane Doe, Chief Executive Officer of Acme."]},
    )

    outcome = company.research_outcomes["leadership"]
    assert outcome["status"] == "accepted_and_mapped"
    item = next(i for i in company.research_evidence if i["field"] == "leadership")
    assert item["claim"] == {"name": "Jane Doe", "title": "CEO"}


def test_stored_count_keeps_evidence_when_a_range_is_also_returned():
    """linear.app returned both a range string and a count from different
    sources; the stored count needs evidence at its own source, not just the
    range claim's."""
    company = _company()
    count_url = "https://pitchbook.com/profiles/company/123"
    range_url = "https://exa.ai/websets/directory/acme"
    research._apply_firmographics(
        company,
        {
            "employee_range": "51-200 employees",
            "employee_range_source_url": range_url,
            "employee_range_excerpt": "Acme has 51-200 employees.",
            "employee_count": 141,
            "employee_count_source_url": count_url,
            "employee_count_excerpt": "Acme employs 141 people.",
        },
        "gemini_grounded_search",
        allowed_source_urls={count_url, range_url},
        searched_fields={"employee_range"},
        grounding_supports={
            count_url: ["Acme employs 141 people."],
            range_url: ["Acme has 51-200 employees."],
        },
    )

    assert company.employee_count.value == 141
    items = [i for i in company.research_evidence if i["field"] == "employee_range"]
    assert any(
        item["claim"] == {"employee_range": 141}
        and research._normalize_grounding_url(item["source_url"])
        == research._normalize_grounding_url(count_url)
        for item in items
    )
    assert any(
        item["claim"] == {"employee_range": "51-200 employees"}
        and research._normalize_grounding_url(item["source_url"])
        == research._normalize_grounding_url(range_url)
        for item in items
    )


def test_count_outside_the_returned_range_is_kept_and_flagged():
    """A count that no returned band can contain is flagged for review, not
    dropped: it was already validated against its own source."""
    company = _company()
    count_url = "https://pitchbook.com/profiles/company/123"
    range_url = "https://exa.ai/websets/directory/acme"
    research._apply_firmographics(
        company,
        {
            "employee_range": "112-129 employees",
            "employee_range_source_url": range_url,
            "employee_range_excerpt": "Acme has 112-129 employees.",
            "employee_count": 141,
            "employee_count_source_url": count_url,
            "employee_count_excerpt": "Acme employs 141 people.",
        },
        "gemini_grounded_search",
        allowed_source_urls={count_url, range_url},
        searched_fields={"employee_range"},
        grounding_supports={
            count_url: ["Acme employs 141 people."],
            range_url: ["Acme has 112-129 employees."],
        },
    )

    assert company.employee_count.value == 141
    outcome = company.research_outcomes["employee_range"]
    assert outcome["status"] == "accepted_and_mapped"
    assert "outside the size range" in outcome["note"]
    claims = {
        json.dumps(item["claim"], sort_keys=True, default=str)
        for item in company.research_evidence if item["field"] == "employee_range"
    }
    assert json.dumps({"employee_range": 141}, sort_keys=True) in claims
    assert json.dumps({"employee_range": "112-129 employees"}, sort_keys=True) in claims


def test_bundled_leadership_claim_does_not_discard_the_whole_team():
    """Semrush's live response bundled three executives into one evidence claim
    whose excerpt named only the CEO, so every name was rejected."""
    company = _company()
    url = "https://acme.com/news/leadership"
    people = [
        {"name": "Bill Wagner", "title": "CEO", "excerpt": "Bill Wagner, CEO of Acme"},
        {"name": "Abby Miller", "title": "Chief Customer Officer",
         "excerpt": "Abby Miller as Chief Customer Officer"},
        {"name": "Tara Haas", "title": "Chief of Staff", "excerpt": "Tara Haas as Chief of Staff"},
    ]
    research._apply_firmographics(
        company,
        {
            "leadership": [dict(person, source_url=url) for person in people],
            "research_evidence": [{
                "field": "leadership", "criterion": "company_leadership",
                "claim": {"leadership": [
                    {"name": person["name"], "title": person["title"]} for person in people
                ]},
                "source_url": url, "excerpt": "Bill Wagner, CEO of Acme",
            }],
        },
        "gemini_grounded_search",
        allowed_source_urls={url},
        searched_fields={"leadership"},
        grounding_supports={url: [person["excerpt"] for person in people]},
    )

    assert company.research_outcomes["leadership"]["status"] == "accepted_and_mapped"
    items = [i for i in company.research_evidence if i["field"] == "leadership"]
    assert {i["claim"]["name"] for i in items} == {"Bill Wagner", "Abby Miller", "Tara Haas"}
    for item in items:
        assert set(item["claim"]) == {"name", "title"}


def test_cited_source_urls_collects_nested_citations():
    data = {
        "official_website_source_url": "https://acme.com",
        "leadership": [
            {"name": "A B", "source_url": "https://acme.com/team"},
        ],
        "research_evidence": [
            {"field": "industry", "source_url": "https://acme.com/about",
             "claim": {"industry": "SaaS"}},
            {"field": "funding", "source_url": None},
        ],
        "unrelated": "https://acme.com/ignored",
    }

    assert research._cited_source_urls(data) == {
        "https://acme.com", "https://acme.com/team", "https://acme.com/about",
    }


def test_cited_redirect_outside_the_chunk_list_is_resolved(monkeypatch):
    """PostHog's live response cited two chunks the chunk pass never resolved,
    so they were persisted as dead Google redirect URLs."""
    cited = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-extra"
    already = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token-known"
    seen: list[set[str]] = []

    def fake_resolve(urls, **_kwargs):
        seen.append(set(urls))
        return {url: "https://posthog.com/about" for url in urls}

    monkeypatch.setattr(research, "_resolve_grounding_urls", fake_resolve)
    data = {"research_evidence": [
        {"field": "industry", "source_url": cited},
        {"field": "headquarters", "source_url": already},
        {"field": "revenue", "source_url": "https://posthog.com/pricing"},
    ]}

    merged = research._resolve_cited_redirects(data, {already: "https://posthog.com/"})

    assert seen == [{cited}]
    assert merged[cited] == "https://posthog.com/about"
    assert merged[already] == "https://posthog.com/"
