"""Deterministic first-party profiler: extraction, acceptance and safe fetching.

Every test uses a fake fetcher or an httpx MockTransport, so none of them
touch the public web.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from csg import discover
from csg.discovery import site_profile
from csg.discovery import research
from csg.discovery.base import CompanyCandidate
from csg.models import Company

DESCRIPTION = (
    "Acme provides identity security and compliance automation for mid-market "
    "companies."
)

HOME_HTML = f"""<html><head>
<title>Acme Security</title>
<meta name="description" content="{DESCRIPTION}">
<link rel="canonical" href="https://acme.com/">
<script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Organization",
 "address": {{"@type": "PostalAddress", "addressLocality": "Austin",
             "addressRegion": "TX", "addressCountry": "US"}}}}
</script>
<script src="/wp-content/theme/app.js"></script>
</head><body>
<nav>
  <a href="/about">About</a>
  <a href="/team">Team</a>
  <a href="/contact">Contact</a>
  <a href="/careers">Careers</a>
</nav>
<h2>Our services</h2>
<ul><li>Identity security</li><li>Compliance automation</li>
<li>Cloud security reviews</li></ul>
</body></html>"""

TEAM_HTML = """<html><body>
<h1>Leadership</h1>
<div>Alex Kim</div><div>Chief Executive Officer</div>
<div>Sam Lee</div><div>Chief Technology Officer</div>
<p>Acme helps customers stay secure.</p>
</body></html>"""

CAREERS_HTML = """<html><body>
<h1>Careers</h1>
<a href="/careers/security-engineer/">Security Engineer</a>
</body></html>"""


class FakeFetcher:
    def __init__(self, pages: dict[str, site_profile.FetchedPage]):
        self.pages = pages
        self.requests: list[str] = []

    async def fetch(self, url: str, *, max_bytes: int = site_profile.MAX_BYTES,
                    domain: str = "") -> site_profile.FetchedPage:
        self.requests.append(url)
        page = self.pages.get(url)
        if page is None:
            return site_profile.FetchedPage(url=url, status=404)
        return page


def _page(url: str, body: str, status: int = 200) -> site_profile.FetchedPage:
    return site_profile.FetchedPage(url=url, status=status, body=body,
                                    content_type="text/html")


def _site_pages() -> dict[str, site_profile.FetchedPage]:
    return {
        "https://acme.com": _page("https://acme.com", HOME_HTML),
        "https://acme.com/about": _page(
            "https://acme.com/about", "<html><body><h1>About Acme</h1></body></html>"
        ),
        "https://acme.com/team": _page("https://acme.com/team", TEAM_HTML),
        "https://acme.com/contact": _page(
            "https://acme.com/contact", "<html><body><h1>Contact</h1></body></html>"
        ),
        "https://acme.com/careers": _page("https://acme.com/careers", CAREERS_HTML),
    }


def _profile(company: Company, pages: dict[str, site_profile.FetchedPage]):
    return asyncio.run(
        site_profile.profile_official_site(company, fetcher=FakeFetcher(pages))
    )


def test_facts_stated_by_official_pages_are_accepted_with_provenance(
    enable_official_site_profile,
):
    company = Company(domain="acme.com", name="Acme")
    profile = _profile(company, _site_pages())

    assert profile is not None and profile["status"] == "ok"
    assert company.description == DESCRIPTION
    assert company.website is not None and company.website.value == "https://acme.com/"
    assert company.headquarters is not None
    assert company.headquarters.value == {"city": "Austin", "state": "TX", "country": "US"}

    for field in ("official_website", "description", "headquarters", "leadership", "services"):
        outcome = company.research_outcomes[field]
        assert outcome["status"] == "accepted_and_mapped"
        assert outcome["verification"] == "official_site_fetch"
        assert profile["fields"][field] == "accepted"

    evidence = {item["field"]: item for item in company.research_evidence}
    assert evidence["description"]["source_url"] == "https://acme.com"
    assert DESCRIPTION in evidence["description"]["excerpt"]
    assert evidence["description"]["excerpt"].startswith("<meta")
    assert evidence["description"]["retrieved_at"] == profile["retrieved_at"]
    assert evidence["description"]["source_tier"] == 1
    assert evidence["leadership"]["claim"]["leadership"]["name"] == "Alex Kim"

    assert profile["careers_url"] == "https://acme.com/careers"
    assert profile["job_link_count"] == 1
    assert "WordPress" in profile["technologies"]
    informational = {item["field"] for item in company.research_evidence if item.get("informational")}
    assert {"careers_page", "job_links", "technology_stack"} <= informational


def test_model_accepted_fields_are_never_overwritten(enable_official_site_profile):
    company = Company(domain="acme.com", name="Acme", description="Existing verified text.")
    company.research_outcomes["description"] = {
        "status": "accepted_and_mapped", "criterion": "company_description",
    }
    profile = _profile(company, _site_pages())

    assert company.description == "Existing verified text."
    assert profile["fields"]["description"] == "already_verified"
    assert not any(
        item.get("field") == "description" for item in company.research_evidence
    )
    # Independent fields are still verified from the official site.
    assert company.research_outcomes["headquarters"]["status"] == "accepted_and_mapped"


def test_unreachable_site_marks_each_stage_and_changes_nothing(enable_official_site_profile):
    company = Company(domain="acme.com", name="Acme")
    fetcher = FakeFetcher({})

    profile = asyncio.run(site_profile.profile_official_site(company, fetcher=fetcher))

    assert profile["status"] == "unreachable"
    assert set(profile["fields"].values()) == {"site_unreachable"}
    assert company.research_evidence == []
    assert company.research_outcomes == {}
    # All three scheme/www candidates were attempted, robots first each time.
    assert fetcher.requests.count("https://acme.com/robots.txt") == 1


def test_robots_disallow_blocks_the_site(enable_official_site_profile):
    pages = _site_pages()
    for base in ("https://acme.com", "https://www.acme.com", "http://acme.com"):
        pages[f"{base}/robots.txt"] = site_profile.FetchedPage(
            url=f"{base}/robots.txt", status=200,
            body="User-agent: *\nDisallow: /\n", content_type="text/plain",
        )
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["status"] == "blocked_by_robots"
    assert profile["robots"] == "disallowed"
    assert company.research_evidence == []


def test_challenge_page_is_reported_as_challenged(enable_official_site_profile):
    pages = {"https://acme.com": _page(
        "https://acme.com", "<html><title>Just a moment...</title></html>", status=403,
    )}
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["status"] == "challenged"
    assert company.description is None


def test_a_claim_the_excerpt_does_not_state_is_not_a_fact():
    assert site_profile._fact(
        "description", "Acme is a bank.", "https://acme.com", DESCRIPTION,
    ) is None
    assert site_profile._fact(
        "description", "Acme is a bank",
        "https://acme.com", "Acme is not a bank and never was.",
    ) is None


def test_private_addresses_are_rejected_before_any_request(monkeypatch):
    monkeypatch.setattr(
        site_profile.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(site_profile.UnsafeURLError, match="non_public_address"):
        site_profile.validate_url("https://internal.example.com")

    monkeypatch.setattr(
        site_profile.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert site_profile.validate_url("https://acme.com/about") == "https://acme.com/about"

    for url in (
        "file:///etc/passwd",
        "http://acme.com:8080/",
        "https://user:pass@acme.com/",
        "http://127.0.0.1/",
        "http://localhost/",
    ):
        with pytest.raises(site_profile.UnsafeURLError):
            site_profile.validate_url(url)


def test_redirect_off_the_company_domain_is_rejected(monkeypatch):
    monkeypatch.setattr(
        site_profile.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    async def run():
        fetcher = site_profile.SafeSiteFetcher(transport=httpx.MockTransport(handler))
        try:
            return await fetcher.fetch("https://acme.com", domain="acme.com")
        finally:
            await fetcher.__aexit__(None, None, None)

    page = asyncio.run(run())

    assert page.error.startswith("host_outside_domain")


def test_response_size_and_content_type_limits(monkeypatch):
    monkeypatch.setattr(
        site_profile.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/large":
            return httpx.Response(200, content=b"x" * 5000,
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"\x89PNG",
                              headers={"content-type": "image/png"})

    async def run():
        fetcher = site_profile.SafeSiteFetcher(transport=httpx.MockTransport(handler))
        try:
            large = await fetcher.fetch("https://acme.com/large", max_bytes=1000)
            image = await fetcher.fetch("https://acme.com/logo.png")
            return large, image
        finally:
            await fetcher.__aexit__(None, None, None)

    large, image = asyncio.run(run())

    assert large.error == "too_large"
    assert image.error == "invalid_content_type"


def test_profiling_is_idempotent(enable_official_site_profile):
    company = Company(domain="acme.com", name="Acme")
    first = _profile(company, _site_pages())
    evidence_after_first = len(company.research_evidence)

    second = _profile(company, _site_pages())

    assert second["status"] == "ok"
    assert len(company.research_evidence) == evidence_after_first
    assert second["fields"]["description"] == "already_verified"
    # A fast second pass can land in the same clock tick; freshness is not part
    # of idempotency. What matters is that no duplicate evidence was added.
    assert second["retrieved_at"] >= first["retrieved_at"]


def test_robots_parser_allows_paths_not_disallowed():
    assert site_profile.parse_robots("User-agent: *\nDisallow: /private\n", "/about")
    assert not site_profile.parse_robots("User-agent: *\nDisallow: /\n", "/about")
    assert site_profile.parse_robots("User-agent: other\nDisallow: /\n", "/about")


def test_malformed_meta_description_is_rejected(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        '<html><head><meta name="description" content="Acme provides identity '
        'security and compliance automation for mid-market cos\ufffdp."></head></html>',
    )

    company = Company(domain="acme.com", name="Acme")
    profile = _profile(company, pages)

    assert profile["fields"]["description"] == "missing_on_site"
    assert company.description is None


def test_call_to_action_meta_description_is_rejected(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        '<html><head><meta name="description" content="Contact us today to learn '
        'how Acme can help your business grow."></head></html>',
    )
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["fields"]["description"] == "missing_on_site"
    assert company.description is None


def test_job_titles_are_not_services(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        f'<html><head><meta name="description" content="{DESCRIPTION}"></head>'
        "<body><h1>Acme</h1></body></html>",
    )
    pages["https://acme.com/careers"] = _page(
        "https://acme.com/careers",
        "<html><body><h1>Careers</h1>"
        "<h2>Solutions Engineer, APAC</h2>"
        "<ul><li>Singapore</li><li>Creative Producer</li></ul>"
        "</body></html>",
    )
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["fields"]["services"] == "missing_on_site"
    assert not any(item.get("field") == "services" for item in company.research_evidence)


def test_read_more_is_not_a_service(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        f'<html><head><meta name="description" content="{DESCRIPTION}"></head>'
        '<body><h2>Our services</h2><ul><li>Read more</li><li>Contact us</li>'
        '</ul></body></html>',
    )
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["fields"]["services"] == "missing_on_site"
    assert not any(item.get("field") == "services" for item in company.research_evidence)


def test_testimonial_and_former_leader_are_not_leadership(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com/about"] = _page(
        "https://acme.com/about",
        "<html><body><h1>About Acme</h1>"
        "<div>Jane Doe</div><div>CEO, Northwind Traders</div>"
        "<div>John Roe</div><div>Former Chief Technology Officer</div>"
        "</body></html>",
    )
    pages["https://acme.com/team"] = _page(
        "https://acme.com/team", "<html><body><h1>Team</h1></body></html>",
    )
    company = Company(domain="acme.com", name="Acme")

    profile = _profile(company, pages)

    assert profile["fields"]["leadership"] == "missing_on_site"
    assert not any(item.get("field") == "leadership" for item in company.research_evidence)


def test_schema_person_needs_organization_context(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com/team"] = _page(
        "https://acme.com/team",
        '<html><head><script type="application/ld+json">'
        '{"@type": "Organization", "employee": [{"@type": "Person", '
        '"name": "Alex Kim", "jobTitle": "Chief Executive Officer"}]}'
        '</script><script type="application/ld+json">'
        '{"@type": "Person", "name": "Guest Writer", '
        '"jobTitle": "Chief Executive Officer"}'
        '</script></head><body></body></html>',
    )
    company = Company(domain="acme.com", name="Acme")

    _profile(company, pages)

    claims = [
        item["claim"]["leadership"]["name"]
        for item in company.research_evidence
        if item.get("field") == "leadership"
    ]
    assert claims == ["Alex Kim"]


def test_short_structured_address_is_supported_without_text_length_heuristic(
    enable_official_site_profile,
):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        '<html><head><script type="application/ld+json">'
        '{"@type": "Organization", "address": {"@type": "PostalAddress", '
        '"addressLocality": "Austin", "addressRegion": "TX"}}'
        '</script></head><body></body></html>',
    )
    company = Company(domain="acme.com", name="Acme")

    _profile(company, pages)

    assert company.headquarters is not None
    assert company.headquarters.value == {"city": "Austin", "state": "TX"}


def test_unrelated_or_event_address_is_not_headquarters(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com"] = _page(
        "https://acme.com",
        '<html><head><script type="application/ld+json">'
        '{"@type": "Event", "location": {"addressLocality": "Austin", '
        '"addressRegion": "TX"}}'
        '</script></head><body></body></html>',
    )
    company = Company(domain="acme.com", name="Acme")

    _profile(company, pages)

    assert company.headquarters is None


def test_website_rejects_credentials_and_nonstandard_port(enable_official_site_profile):
    for canonical in (
        "https://user:secret@acme.com/",
        "https://acme.com:8443/",
    ):
        pages = _site_pages()
        pages["https://acme.com"] = _page(
            "https://acme.com",
            f'<html><head><link rel="canonical" href="{canonical}"></head></html>',
        )
        company = Company(domain="acme.com", name="Acme")

        _profile(company, pages)

        assert company.website is None


def test_robots_disallow_is_enforced_for_subpages(enable_official_site_profile):
    pages = _site_pages()
    pages["https://acme.com/robots.txt"] = site_profile.FetchedPage(
        url="https://acme.com/robots.txt", status=200,
        body="User-agent: *\nDisallow: /team\n", content_type="text/plain",
    )
    fetcher = FakeFetcher(pages)
    company = Company(domain="acme.com", name="Acme")

    profile = asyncio.run(site_profile.profile_official_site(company, fetcher=fetcher))

    assert "https://acme.com/team" not in fetcher.requests
    assert any(
        page["url"] == "https://acme.com/team" and page["status"] == "blocked_by_robots"
        for page in profile["pages"]
    )
    assert not any(item.get("field") == "leadership" for item in company.research_evidence)


def test_ipv6_and_mapped_ipv6_destinations_are_rejected():
    for url in (
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00::1]/",
    ):
        with pytest.raises(site_profile.UnsafeURLError):
            site_profile.validate_url(url)
    assert site_profile.validate_url("http://[::ffff:93.184.216.34]/") == (
        "http://[::ffff:93.184.216.34]/"
    )


def test_redirect_to_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        site_profile.socket, "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    async def run():
        fetcher = site_profile.SafeSiteFetcher(transport=httpx.MockTransport(handler))
        try:
            return await fetcher.fetch("https://acme.com", domain="acme.com")
        finally:
            await fetcher.__aexit__(None, None, None)

    page = asyncio.run(run())

    assert page.error.startswith("non_public_address")


def test_dns_rebinding_connects_to_the_validated_ip(monkeypatch):
    resolutions: list[str] = []
    seen: list[tuple[str, str, str | None]] = []

    def resolver(host, *args, **kwargs):
        resolutions.append(host)
        ip = "93.184.216.34" if len(resolutions) == 1 else "10.0.0.5"
        return [(2, 1, 6, "", (ip, 0))]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((
            request.url.host,
            request.headers.get("host", ""),
            request.extensions.get("sni_hostname"),
        ))
        return httpx.Response(200, content=b"<html>ok</html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(site_profile.socket, "getaddrinfo", resolver)

    async def run():
        fetcher = site_profile.SafeSiteFetcher(transport=httpx.MockTransport(handler))
        try:
            return await fetcher.fetch("https://acme.com", domain="acme.com")
        finally:
            await fetcher.__aexit__(None, None, None)

    page = asyncio.run(run())

    assert page.status == 200
    assert resolutions == ["acme.com"]
    assert seen == [("93.184.216.34", "acme.com", "acme.com")]


def test_informational_evidence_survives_a_gemini_retry(
    enable_official_site_profile,
):
    company = Company(domain="acme.com", name="Acme")
    _profile(company, _site_pages())
    informational = [
        item for item in company.research_evidence if item.get("informational")
    ]
    assert informational and all(item.get("evidence_key") for item in informational)
    keyless = {**informational[0]}
    keyless.pop("evidence_key")
    company.research_evidence.append(keyless)

    research._apply_firmographics(
        company, {}, source="gemini_grounded_search", allowed_source_urls=set(),
        grounding_available=False,
    )
    # The retry's official-site pass sees the stored profile as complete and
    # returns early; it must not be the only thing keeping informational data.
    company.research_outcomes["_site_profile"] = {"status": "ok"}
    asyncio.run(discover._profile_official_site(company, None, None, None))

    fields = [item.get("field") for item in company.research_evidence if item.get("informational")]
    assert {"careers_page", "job_links", "technology_stack"} <= set(fields)


def test_profiler_outcome_merge_keeps_metadata(enable_official_site_profile):
    company = Company(domain="acme.com", name="Acme")
    company.research_outcomes["description"] = {
        "status": "found_but_rejected",
        "criterion": "company_description",
        "reason": "old rejection",
        "evidence_keys": ["old-key"],
        "source_tier": 3,
        "discovery_hint": "hint",
    }

    _profile(company, _site_pages())

    outcome = company.research_outcomes["description"]
    assert outcome["status"] == "accepted_and_mapped"
    assert "old-key" in outcome["evidence_keys"]
    assert outcome["source_tier"] == 3
    assert outcome["discovery_hint"] == "hint"
    assert outcome["previous_rejection"] == "old rejection"


def test_enrich_all_preserves_operational_failure_after_profiler_success(
    enable_official_site_profile, monkeypatch,
):
    candidate = CompanyCandidate(
        domain="acme.com", source="test", discovered_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(discover.db, "get_company", lambda conn, domain: None)

    async def fake_enrich(company, run_id=None):
        return "request_error"

    async def fake_profile(company):
        company.description = DESCRIPTION
        company.research_outcomes["description"] = {
            "status": "accepted_and_mapped", "criterion": "company_description",
        }
        return {"status": "ok", "fields": {"description": "accepted"}}

    monkeypatch.setattr(discover, "enrich_company_firmographics", fake_enrich)
    monkeypatch.setattr(site_profile, "profile_official_site", fake_profile)
    saves: list[tuple[str, str, dict]] = []

    def save(key, status, payload):
        saves.append((key, status, payload))
        return True

    pairs, skipped, failures = asyncio.run(discover._enrich_all(
        None, [candidate], "GA", None, checkpoint_save=save,
    ))
    company = pairs[0][1]
    summary = company.research_outcomes["_summary"]

    assert skipped == 0
    assert failures == {"request_error": 1}
    assert company.description == DESCRIPTION
    assert summary["status"] == "failed"
    assert summary["failure_reason"] == "request_error"
    assert not research.stored_enrichment_is_terminal(summary)
    assert discover._needs_enrichment(Company(
        domain="acme.com", research_outcomes={"_summary": summary},
    ))
    assert any(key.startswith("site:") and status == "done" for key, status, _ in saves)
    assert any(key.startswith("enrich:") and status == "failed" for key, status, _ in saves)

    checkpoints = {key: (status, payload) for key, status, payload in saves}
    assert not discover._enrichment_checkpoint_is_reusable({
        "status": "failed", "payload": checkpoints["enrich:acme.com"][1],
    })

    calls = 0

    async def counting_enrich(company, run_id=None):
        nonlocal calls
        calls += 1
        return "timeout"

    def load(key):
        saved = checkpoints.get(key)
        return {"status": saved[0], "payload": saved[1]} if saved else None

    monkeypatch.setattr(discover, "enrich_company_firmographics", counting_enrich)
    retried, _, failures = asyncio.run(discover._enrich_all(
        None, [candidate], "GA", None, checkpoint_load=load,
        checkpoint_save=lambda *_args: True,
    ))

    assert calls == 1
    assert failures == {"timeout": 1}
    assert retried[0][1].research_outcomes["_summary"]["status"] == "failed"
