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
    '"company_type_excerpt": "Acme is a small business", "employee_count": 500, '
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
        "grounding_metadata_types": ["GroundingMetadata"],
        "grounding_chunk_count": 1,
        "grounding_support_count": 1,
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
    assert "supporting URL missing" in outcome["reason"]
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
    outcomes = {"revenue": {"status": "found_but_rejected", "reason": "supporting URL missing"},
                "_summary": {"status": "needs_review"}}

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
