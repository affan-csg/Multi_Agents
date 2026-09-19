"""Tests for the multi-level job discovery fallback (jobs_discovery.py).

Covers the chain ATS -> careers_page -> web_search -> UNKNOWN and the
specific failure-mode requirement that started this work: an unsupported or
undetectable ATS must never be scored as "zero hiring need" -- it must
either recover real job data from a fallback method, or land on
hiring_signal="unknown" rather than a confirmed zero.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from csg import jobs_discovery
from csg.jobs_discovery import discover_jobs
from csg.models import Company, JobPosting


class FakeFetcher:
    """Same minimal stand-in used across test_ats_detect.py / test_ats_rippling.py."""

    def __init__(self, gets: dict[str, tuple[int, str]] | None = None,
                 jsons: dict[str, tuple[int, object]] | None = None,
                 raise_on: dict[str, Exception] | None = None):
        self.gets = gets or {}
        self.jsons = jsons or {}
        self.raise_on = raise_on or {}
        self.get_calls: list[str] = []

    async def get(self, url: str) -> tuple[int, str]:
        self.get_calls.append(url)
        if url in self.raise_on:
            raise self.raise_on[url]
        return self.gets.get(url, (404, ""))

    async def get_json(self, url: str) -> tuple[int, object]:
        if url in self.raise_on:
            raise self.raise_on[url]
        return self.jsons.get(url, (404, {}))

    async def post_json(self, url: str, json_body: dict) -> tuple[int, object]:
        return (404, {})


def _company(domain: str = "acme.com", name: str = "Acme") -> Company:
    return Company(domain=domain, name=name)


def _no_tavily(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


# --------------------------------------------------------------------------
# 1. ATS detected -> jobs found
# --------------------------------------------------------------------------

def test_ats_detected_and_jobs_found():
    gh_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    jobs_payload = {"jobs": [
        {"id": 1, "title": "Backend Engineer", "location": {"name": "Remote"},
         "departments": [{"name": "Engineering"}], "absolute_url": "https://x/1"},
    ]}
    fetcher = FakeFetcher(jsons={gh_url: (200, jobs_payload)})
    ats_result = ("greenhouse", "acme", "careers_page", "https://acme.com/careers")

    result = asyncio.run(discover_jobs(fetcher, _company(), ats_result))

    assert result.method == "ats"
    assert result.hiring_signal == "confirmed"
    assert result.source == "greenhouse"
    assert len(result.postings) == 1
    assert result.postings[0].source == "ats"
    assert result.postings[0].ats_provider == "greenhouse"


# --------------------------------------------------------------------------
# 2. No ATS -> careers page -> jobs found
# --------------------------------------------------------------------------

def test_no_ats_careers_page_scrape_finds_jobs():
    careers_url = "https://acme.com/careers"
    body = (
        '<html><body>'
        '<a href="/careers/senior-backend-engineer">Senior Backend Engineer</a>'
        '<a href="/careers/product-manager">Product Manager</a>'
        '<a href="/about">About</a>'
        '</body></html>'
    )
    fetcher = FakeFetcher(gets={careers_url: (200, body)})

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "careers_page"
    assert result.hiring_signal == "confirmed"
    assert len(result.postings) == 2
    titles = {p.title for p in result.postings}
    assert "Senior Backend Engineer" in titles
    assert all(p.source == "careers_page" for p in result.postings)
    assert all(p.ats_provider is None for p in result.postings)


def test_careers_page_jsonld_jobposting_parsed():
    careers_url = "https://acme.com/careers"
    jsonld = {
        "@type": "JobPosting",
        "title": "Staff Data Engineer",
        "identifier": {"value": "REQ-42"},
        "jobLocation": {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
        "employmentType": "FULL_TIME",
        "url": "https://acme.com/careers/staff-data-engineer",
    }
    body = f'<html><script type="application/ld+json">{json.dumps(jsonld)}</script></html>'
    fetcher = FakeFetcher(gets={careers_url: (200, body)})

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "careers_page"
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.title == "Staff Data Engineer"
    assert p.external_id == "REQ-42"
    assert p.location == "Austin, TX"
    assert p.employment_type == "FULL_TIME"


# --------------------------------------------------------------------------
# 3. No ATS -> careers page fails/empty -> external search -> jobs found
# --------------------------------------------------------------------------

def test_no_ats_careers_empty_falls_through_to_external_search(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    fetcher = FakeFetcher()  # every careers path 404s

    async def fake_search(domain, name):
        return [JobPosting(domain=domain, ats_provider=None, source="web_search",
                            external_id="search:1", title="Acme is hiring a Platform Engineer",
                            url="https://example.com/article")], None

    monkeypatch.setattr("csg.jobs_discovery._search_jobs_external", fake_search)

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "web_search"
    assert result.hiring_signal == "confirmed"
    assert result.source == "tavily search"
    assert len(result.postings) == 1
    assert result.postings[0].source == "web_search"


# --------------------------------------------------------------------------
# 4. Unsupported ATS (detect() returns None) -> fallback continues
# --------------------------------------------------------------------------

def test_unsupported_ats_continues_to_fallback():
    # ats_result is None (as ats.detect() returns for an unsupported/undetectable
    # platform, e.g. Phenom/iCIMS) -- must not short-circuit to zero.
    careers_url = "https://acme.com/careers"
    body = '<a href="/jobs/site-reliability-engineer">SRE</a>'
    fetcher = FakeFetcher(gets={careers_url: (200, body)})

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "careers_page"
    assert len(result.postings) == 1


# --------------------------------------------------------------------------
# 5. ATS fetch timeout/error -> fallback continues
# --------------------------------------------------------------------------

def test_ats_fetch_error_continues_to_careers_page():
    gh_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    careers_url = "https://acme.com/careers"
    body = '<a href="/careers/backend-engineer">Backend Engineer</a>'
    fetcher = FakeFetcher(
        raise_on={gh_url: httpx.ConnectTimeout("timed out")},
        gets={careers_url: (200, body)},
    )
    ats_result = ("greenhouse", "acme", "careers_page", careers_url)

    result = asyncio.run(discover_jobs(fetcher, _company(), ats_result))

    assert result.method == "careers_page"
    assert len(result.postings) == 1
    assert "ATS fetch failed" in (result.notes or "")


# --------------------------------------------------------------------------
# 6. Search/API 429 -> retry/fallback continues (no unhandled exception)
# --------------------------------------------------------------------------

def test_external_search_429_does_not_crash_and_yields_unknown(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    fetcher = FakeFetcher()  # careers pages all 404

    def fake_tavily_sync(query, api_key):
        raise httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "https://api.tavily.com/search"),
            response=httpx.Response(429, request=httpx.Request("POST", "https://x")))

    monkeypatch.setattr("csg.jobs_discovery._tavily_search_sync", fake_tavily_sync)

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    # The 429 surfaces as a classified failure note, not an unhandled crash,
    # and the chain still reaches its terminal UNKNOWN state.
    assert result.method == "unknown"
    assert result.hiring_signal == "unknown"
    assert result.postings == []


# --------------------------------------------------------------------------
# 7. All methods fail -> UNKNOWN, not zero hiring need
# --------------------------------------------------------------------------

def test_all_methods_exhausted_yields_unknown_not_zero(monkeypatch):
    _no_tavily(monkeypatch)
    fetcher = FakeFetcher()  # no ATS, careers pages all 404, no Tavily key

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "unknown"
    assert result.hiring_signal == "unknown"
    assert result.postings == []
    assert result.notes  # a failure reason is always recorded for UNKNOWN
    assert "ATS not detected" in result.notes


def test_careers_page_reachable_but_empty_is_still_unknown_after_search_fails(monkeypatch):
    _no_tavily(monkeypatch)
    careers_url = "https://acme.com/careers"
    fetcher = FakeFetcher(gets={careers_url: (200, "<html><body>Nothing here.</body></html>")})

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert result.method == "unknown"
    assert result.hiring_signal == "unknown"
    assert "no jobs found" in result.notes or "TAVILY_API_KEY" in result.notes


# --------------------------------------------------------------------------
# 8. Tavily DNS/connection-error resilience -- the fix for the D-tier bug
# where a transient `[Errno 11002] getaddrinfo failed` (or any other
# transport-layer blip) burned the only fallback method left and dropped a
# perfectly hireable company to hiring_signal=unknown. See
# _tavily_post_with_connection_retry in jobs_discovery.py.
# --------------------------------------------------------------------------

def _patch_tavily_client(monkeypatch, handler):
    """_tavily_post_with_connection_retry builds its own httpx.Client() with
    no injection point -- same constraint as groq_tavily's own client, same
    fix: patch httpx.Client in the module under test to route through a
    MockTransport instead of a real socket, so a "DNS failure" can be
    simulated deterministically instead of by disconnecting the network.

    usage_tracker's own fire-and-forget usage-recording POST goes through
    this same patched httpx.Client (the patch is module-wide, not scoped to
    just the Tavily call) -- routed to a plain 200 here so it neither hits
    the network nor is counted as a Tavily call by `handler`.
    """
    real_client = httpx.Client

    def routed_handler(request: httpx.Request) -> httpx.Response:
        if "api.tavily.com" not in str(request.url):
            return httpx.Response(200, json={})
        return handler(request)

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(routed_handler))

    monkeypatch.setattr(jobs_discovery.httpx, "Client", fake_client)


def test_tavily_dns_failure_then_success_recovers_within_retry_budget(monkeypatch):
    """DNS failure -> retry -> success: the exact scenario from the bug
    report. The first call raises the transport error Windows reports for a
    DNS resolution failure; the retry must succeed and return real results,
    not fall through to UNKNOWN."""
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    monkeypatch.setattr(jobs_discovery.time, "sleep", lambda s: None)  # don't actually wait in tests

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno 11002] getaddrinfo failed")
        return httpx.Response(200, json={"results": [
            {"title": "Acme is hiring a Backend Engineer", "url": "https://acme.com/jobs/1", "content": "..."},
        ]})

    _patch_tavily_client(monkeypatch, handler)
    fetcher = FakeFetcher()  # no ATS, careers pages all 404 -> forces the web_search stage

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert calls["n"] == 2  # exactly one retry, not a second full pass through the chain
    assert result.method == "web_search"
    assert result.hiring_signal == "confirmed"
    assert len(result.postings) == 1


def test_tavily_dns_failure_exhausts_retries_then_unknown_not_zero(monkeypatch):
    """DNS failure -> retries exhausted: every attempt fails the same way
    the bug report's blip did. The company must land on hiring_signal
    ="unknown" (never scored as zero hiring need), and the failure must be
    visible in job_discovery_notes rather than silently swallowed."""
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    monkeypatch.setattr(jobs_discovery.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("[Errno 11002] getaddrinfo failed")

    _patch_tavily_client(monkeypatch, handler)
    fetcher = FakeFetcher()  # no ATS, careers pages all 404

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    # bounded: initial attempt + _TAVILY_CONNECTION_ERROR_RETRIES retries, no more
    assert calls["n"] == jobs_discovery._TAVILY_CONNECTION_ERROR_RETRIES + 1
    assert result.method == "unknown"
    assert result.hiring_signal == "unknown"
    assert result.postings == []
    assert "connection_error" in result.notes


def test_tavily_connection_error_retry_does_not_consume_rate_limit_budget(monkeypatch):
    """A connection error and a 429 are different failure modes with
    different retry budgets (see _tavily_post_with_connection_retry's
    docstring) -- a DNS blip on the first attempt must not eat into the
    separate 429 retry allowance if Tavily *also* rate-limits afterwards."""
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    monkeypatch.setattr(jobs_discovery.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno 11002] getaddrinfo failed")
        if calls["n"] == 2:
            return httpx.Response(429, json={"error": {"message": "try again in 0.01s"}})
        return httpx.Response(200, json={"results": [
            {"title": "Acme hiring now", "url": "https://acme.com/jobs/2", "content": "..."},
        ]})

    _patch_tavily_client(monkeypatch, handler)
    fetcher = FakeFetcher()

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert calls["n"] == 3
    assert result.method == "web_search"
    assert result.hiring_signal == "confirmed"


def test_tavily_timeout_is_also_retried(monkeypatch):
    """classify_error's other transport-layer bucket -- a plain connect/read
    timeout -- gets the same retry treatment as a DNS failure, not just
    httpx.ConnectError specifically."""
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    monkeypatch.setattr(jobs_discovery.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"results": [
            {"title": "Acme career opening", "url": "https://acme.com/jobs/3", "content": "..."},
        ]})

    _patch_tavily_client(monkeypatch, handler)
    fetcher = FakeFetcher()

    result = asyncio.run(discover_jobs(fetcher, _company(), None))

    assert calls["n"] == 2
    assert result.method == "web_search"
    assert result.hiring_signal == "confirmed"
