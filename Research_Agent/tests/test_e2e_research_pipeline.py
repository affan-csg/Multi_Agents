"""Deterministic end-to-end Research campaign through every real stage.

Only external boundaries are faked: discovery results, Gemini HTTP responses,
ATS/careers HTTP, Hunter, and the Outreach HTTP client. Discovery dedup,
Gemini grounding validation, the official-site profiler orchestration,
ATS -> careers -> unknown job discovery, scoring, qualification persistence,
contact persistence, database writes, and the Dashboard company-detail
contract are the production implementations.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from csg import api, db, discover, export, jobs_discovery, pipeline, qualify
from csg.config import load_config
from csg.discovery import research, site_profile
from csg.discovery.base import CompanyCandidate
from csg.models import Company, ProvenanceValue


DOMAINS = [
    "complete.example",
    "hiring.example",
    "conflict.example",
    "quiet.example",
    "flaky.example",
    "siteonly.example",
    "retry.example",
]


class FakeConn:
    """Small PostgREST-like store sufficient for every db function in the run."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    @staticmethod
    def _key(table, row):
        if table in {"scores", "qualifications", "qualification_attempts"}:
            return (row.get("domain"), row.get("run_id"))
        if table == "job_postings":
            return (row.get("domain"), row.get("external_id"))
        return (row.get("domain"),)

    def upsert(self, table, rows, on_conflict=""):
        store = self.tables.setdefault(table, [])
        for row in rows:
            for index, existing in enumerate(store):
                if self._key(table, existing) == self._key(table, row):
                    store[index] = dict(row)
                    break
            else:
                store.append(dict(row))

    def insert_if_absent(self, table, rows, on_conflict=""):
        store = self.tables.setdefault(table, [])
        inserted = []
        for row in rows:
            if not any(self._key(table, existing) == self._key(table, row) for existing in store):
                store.append(dict(row))
                inserted.append(row)
        return inserted

    def select(self, table, select="*", limit=None, order=None, **filters):
        rows = list(self.tables.get(table, []))
        for field, expr in filters.items():
            if expr.startswith("eq."):
                want = expr[3:]
                rows = [r for r in rows if str(r.get(field)) == want]
            elif expr.startswith("in."):
                wanted = set(expr[4:-1].split(","))
                rows = [r for r in rows if str(r.get(field)) in wanted]
        return rows[:limit] if limit else rows

    def update(self, table, values, **filters):
        for row in self.select(table, **filters):
            row.update(values)

    def commit(self):
        pass

    def close(self):
        pass


class FakeFetcher:
    def __init__(self, **kwargs):
        self.gets = kwargs.get("gets") or {}
        self.jsons = kwargs.get("jsons") or {}
        self.stats = {"hits": 0, "misses": 0}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, url):
        return self.gets.get(url, (404, ""))

    async def get_json(self, url):
        return self.jsons.get(url, (404, {}))

    async def post_json(self, url, json_body):
        return (404, {})


def _blank_payload(domain: str) -> dict:
    return {
        "official_website": None, "official_website_source_url": None,
        "official_website_excerpt": None, "headquarters": None,
        "headquarters_source_url": None, "headquarters_excerpt": None,
        "company_type": None, "company_type_source_url": None,
        "company_type_excerpt": None, "business_category": None,
        "business_category_source_url": None, "business_category_excerpt": None,
        "employee_count": None, "employee_count_source_url": None,
        "employee_count_excerpt": None, "revenue": None,
        "revenue_source_url": None, "revenue_excerpt": None,
        "funding": None, "funding_source_url": None, "funding_excerpt": None,
        "industry": None, "industry_source_url": None, "industry_excerpt": None,
        "description": None, "description_source_url": None,
        "description_excerpt": None, "employee_range": None,
        "employee_range_source_url": None, "employee_range_excerpt": None,
        "services": None, "services_source_url": None, "services_excerpt": None,
        "leadership": [], "hiring_service_fit": None,
        "hiring_service_fit_source_url": None, "hiring_service_fit_excerpt": None,
        "classification_evidence": [], "research_evidence": [],
    }


def _grounded_response(domain: str, values: dict, source_text: str, *, metadata: bool = True):
    data = _blank_payload(domain)
    data.update(values)
    url = f"https://{domain}/about"
    candidate = SimpleNamespace()
    if metadata:
        candidate.grounding_metadata = SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri=url))],
            grounding_supports=[SimpleNamespace(
                segment=SimpleNamespace(text=source_text),
                grounding_chunk_indices=[0],
            )],
        )
    return SimpleNamespace(
        text=json.dumps(data), candidates=[candidate],
        usage_metadata=SimpleNamespace(total_token_count=321),
    )


def _full_values(domain: str, name: str) -> tuple[dict, str]:
    url = f"https://{domain}/about"
    source = (
        f"{name} official website is https://{domain}. {name} provides workflow software. "
        f"{name} is headquartered in Austin, TX, "
        f"United States. {name} is a small business and a SaaS company. {name} has "
        f"500 employees. {name} annual revenue is $50M. {name} is in the Software "
        f"industry. {name} provides workflow software. {name} provides implementation services."
    )
    values = {
        "official_website": f"https://{domain}",
        "official_website_source_url": url,
        "official_website_excerpt": f"{name} official website is https://{domain}",
        "headquarters": {"city": "Austin", "state": "TX", "country": "United States"},
        "headquarters_source_url": url,
        "headquarters_excerpt": f"{name} is headquartered in Austin, TX, United States",
        "company_type": "small business", "company_type_source_url": url,
        "company_type_excerpt": f"{name} is a small business",
        "business_category": "SaaS", "business_category_source_url": url,
        "business_category_excerpt": f"{name} is a SaaS company",
        "employee_count": 500, "employee_count_source_url": url,
        "employee_count_excerpt": f"{name} has 500 employees",
        "revenue": "$50M", "revenue_source_url": url,
        "revenue_excerpt": f"{name} annual revenue is $50M",
        "industry": "Software", "industry_source_url": url,
        "industry_excerpt": f"{name} is in the Software industry",
        "description": f"{name} provides workflow software",
        "description_source_url": url,
        "description_excerpt": f"{name} provides workflow software",
        "employee_range": "500 employees", "employee_range_source_url": url,
        "employee_range_excerpt": f"{name} has 500 employees",
        "services": f"{name} provides implementation services",
        "services_source_url": url,
        "services_excerpt": f"{name} provides implementation services",
    }
    return values, source


def _partial_values(domain: str, name: str) -> tuple[dict, str]:
    values, source = _full_values(domain, name)
    for key in ("revenue", "revenue_source_url", "revenue_excerpt", "services",
                "services_source_url", "services_excerpt"):
        values[key] = None
    return values, source


class FakeGemini:
    def __init__(self):
        self.calls: list[str] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, contents, **_kwargs):
        domain = next((d for d in DOMAINS if d in contents), "unknown.example")
        self.calls.append(domain)
        if domain == "flaky.example":
            raise ConnectionError("temporary provider reset")
        if domain == "siteonly.example":
            return _grounded_response(domain, {}, "", metadata=False)
        if domain == "conflict.example":
            return _grounded_response(domain, {
                "description": "A conflicting description with no source support",
                "description_source_url": f"https://{domain}/about",
                "description_excerpt": "A conflicting description with no source support",
            }, "This page describes a different company.")
        if domain == "hiring.example":
            values, source = _partial_values(domain, "Hiring Co")
            return _grounded_response(domain, values, source)
        if domain == "retry.example":
            values, source = _full_values(domain, "Retry Co")
            return _grounded_response(domain, values, source)
        if domain == "quiet.example":
            values, source = _full_values(domain, "Quiet Co")
            return _grounded_response(domain, values, source)
        values, source = _full_values(domain, "Complete Co")
        return _grounded_response(domain, values, source)

    def close(self):
        pass


async def _fake_profile(company: Company):
    if company.domain not in {"siteonly.example", "conflict.example", "flaky.example"}:
        return {"status": "ok", "extractor": "e2e-site-v1", "fields": {}}
    if not company.website:
        company.website = ProvenanceValue(
            value=f"https://{company.domain}", source="official_site",
            url=f"https://{company.domain}/about", method="reported", confidence="high",
        )
        company.research_outcomes["official_website"] = {
            "status": "accepted_and_mapped", "criterion": "automatic_website",
            "reason": "official site fetched",
        }
    if not company.headquarters:
        company.headquarters = ProvenanceValue(
            value={"city": "Austin", "state": "TX", "country": "United States"},
            source="official_site", url=f"https://{company.domain}/about",
            method="reported", confidence="high",
        )
        company.research_outcomes["headquarters"] = {
            "status": "accepted_and_mapped", "criterion": "automatic_headquarters",
            "reason": "official site fetched",
        }
    if company.domain == "siteonly.example" and not company.description:
        company.description = "Official-site description for a company Gemini could not verify."
    company.research_evidence.append({
        "evidence_key": f"site-{company.domain}", "field": "official_website",
        "claim": {"official_website": f"https://{company.domain}"},
        "source_url": f"https://{company.domain}/about", "source_name": "official website",
        "excerpt": f"{company.domain} official website", "evidence_type": "direct",
        "confidence": "high", "source_tier": 1, "criterion": "automatic_website",
        "verification_method": "official_site_fetch", "informational": False,
    })
    company.careers_url = f"https://{company.domain}/careers"
    return {"status": "ok", "extractor": "e2e-site-v1",
            "fields": {"official_website": "accepted", "headquarters": "accepted"}}


class FakeQualificationBackend:
    model = "gemini-3.8-flash"
    max_concurrency = 1

    async def complete(self, _prompt, *, run_id=None, domain=None):
        return qualify._Qualification(
            pain_summary=f"Validated evidence indicates a staffing need for {domain}.",
            recommended_angle="Lead with the verified hiring evidence, not assumptions.",
        )

    async def close(self):
        pass

    def is_retryable(self, exc):
        return False


class FakeOutreachClient:
    def __init__(self, _base):
        pass

    def import_prospects(self, run_id, **kwargs):
        return {"imported": 2, "excluded_no_contact": []}

    def create_campaign(self, **kwargs):
        return {"campaign": {"campaign_id": "campaign-e2e"}, "item_count": 2,
                "excluded_no_contact": []}

    def close(self):
        pass


def test_full_research_campaign_persists_and_serves_complete_company_records(monkeypatch, tmp_path):
    conn = FakeConn()
    gemini = FakeGemini()
    calls: list[str] = []
    checkpoints: dict[str, dict] = {}

    candidates = [
        CompanyCandidate(domain=domain, name=domain.split(".")[0].title(), source="e2e",
                         source_url=f"https://{domain}/about", discovered_at=datetime.now(timezone.utc))
        for domain in DOMAINS
    ] + [
        CompanyCandidate(domain="complete.example", name="Complete Co", source="duplicate",
                         discovered_at=datetime.now(timezone.utc)),
    ]

    async def discover_candidates(_self, region, limit=100):
        return candidates

    async def detect(_fetcher, domain, *_args, **_kwargs):
        if domain == "hiring.example":
            return ("greenhouse", "hiringco", "careers_page", "https://hiring.example/careers")
        if domain == "quiet.example":
            return ("bamboohr", "quietco", "careers_page", "https://quiet.example/careers")
        return None

    now = datetime.now(timezone.utc).isoformat()
    jobs = {
        "https://boards-api.greenhouse.io/v1/boards/hiringco/jobs?content=true": (200, {
            "jobs": [{"id": i, "title": title, "location": {"name": "Remote, US"},
                       "departments": [{"name": "Engineering"}], "absolute_url": f"https://hiring.example/jobs/{i}",
                       "updated_at": now}
                      for i, title in enumerate([
                          "Senior Data Engineer", "Staff Platform Engineer", "Analytics Engineer",
                          "Machine Learning Engineer", "Backend Engineer",
                      ], 1)]
        }),
        "https://quietco.bamboohr.com/careers/list": (200, {"meta": {"totalCount": 0}, "result": []}),
    }
    pages = {
        "https://complete.example/careers": (200, ''.join(
            f'<a href="/careers/{slug}">{slug.replace("-", " ").title()}</a>'
            for slug in ("senior-data-engineer", "platform-engineer", "product-manager")
        )),
        "https://retry.example/careers": (200, '<a href="/careers/data-engineer">Data Engineer</a>'),
        "https://conflict.example/careers": (200, "<p>No roles are listed here.</p>"),
        "https://siteonly.example/careers": (200, "<p>Careers page without structured roles.</p>"),
        "https://flaky.example/careers": (500, "temporary server error"),
    }
    fetcher = FakeFetcher(gets=pages, jsons=jobs)

    retry_company = Company(domain="retry.example", name="Retry Co")
    retry_company.research_outcomes = {
        **{field: {"status": "found_but_rejected", "reason": "provider grounding was empty"}
           for field in research._FIELD_CRITERIA},
        "_summary": {"status": "needs_review", "review_fields": {"grounding": "empty"}},
    }
    checkpoints["enrich:retry.example"] = {
        "status": "done",
        "payload": {"company": retry_company.model_dump(mode="json"),
                    "completeness": retry_company.research_outcomes["_summary"]},
    }

    monkeypatch.setattr("csg.discover.WebSearchProvider.discover", discover_candidates)
    monkeypatch.setattr(discover, "Fetcher", lambda **_kwargs: fetcher)
    monkeypatch.setattr(pipeline, "Fetcher", lambda **_kwargs: fetcher)
    monkeypatch.setattr(pipeline.ats, "detect", detect)
    monkeypatch.setattr(pipeline, "refresh_company_assessment_safely", lambda *_a, **_k: True)
    monkeypatch.setattr("csg.discovery.gemini_keys.make_gemini_client", lambda _key: gemini)
    monkeypatch.setattr(site_profile, "profile_official_site", _fake_profile)
    monkeypatch.setattr(qualify, "_select_backend", lambda: FakeQualificationBackend())
    monkeypatch.setattr("csg.outreach_client.OutreachClient", FakeOutreachClient)
    monkeypatch.setattr(export, "to_csv", lambda *_a, **_k: tmp_path / "companies.csv")
    monkeypatch.setattr(export, "to_jsonl", lambda *_a, **_k: tmp_path / "companies.jsonl")
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setenv("OUTREACH_API_BASE", "https://outreach.example")

    async def fake_contacts(_conn, domains, **_kwargs):
        for domain in domains:
            db.save_contacts(conn, domain, [{
                "email": f"talent@{domain}", "first_name": "Tess", "last_name": "Talent",
                "position": "Head of Talent", "seniority": "executive",
                "department": "People", "confidence": 95, "verification": "valid",
            }], source="hunter")
        return {"domains": len(domains), "contacts": len(domains)}

    import csg.contacts as contacts_mod
    monkeypatch.setattr(contacts_mod, "discover_run", fake_contacts)

    def checkpoint_load(key):
        return checkpoints.get(key)

    def checkpoint_save(key, status, payload):
        checkpoints[key] = {"status": status, "payload": payload}
        return True

    cfg = load_config()
    stats = asyncio.run(discover.run_discovery(
        cfg, region="GA", limit=20, conn=conn, run_id="e2e-run",
        auto_pipeline=False, checkpoint_load=checkpoint_load,
        checkpoint_save=checkpoint_save,
    ))

    assert stats.discovered == 7
    assert stats.duplicates_existing == 0
    assert len(conn.select("companies")) == 7
    assert gemini.calls.count("retry.example") == 1
    assert checkpoints["enrich:flaky.example"]["status"] == "failed"
    assert checkpoints["enrich:siteonly.example"]["status"] == "done"
    assert checkpoints["enrich:hiring.example"]["payload"]["reason"] is None

    # Qualification/contact stages use the same conn and real persistence
    # helpers; only their external LLM/Hunter calls are faked.
    q = asyncio.run(qualify.qualify_run(
        conn, "e2e-run", tiers=["A", "B", "C", "D"],
        domains=DOMAINS,
    ))
    assert q["qualified"] == 7
    c = asyncio.run(fake_contacts(conn, DOMAINS))
    assert c["contacts"] == 7
    asyncio.run(discover._handoff_to_outreach(
        cfg, "e2e-run", stats, None,
        checkpoint_load=checkpoint_load, checkpoint_save=checkpoint_save,
    ))
    assert stats.outreach["campaign_id"] == "campaign-e2e"

    complete = api.company_detail("e2e-run", "complete.example")
    assert complete["company"]["employee_count"]["value"] == 500
    assert complete["company"]["research_outcomes"]["_summary"]["counts"]["verified"] >= 8
    assert "official_website" not in complete["company"]["research_outcomes"]["_summary"]["review_fields"]
    assert complete["postings"] and all(p["url"] for p in complete["postings"])
    assert complete["qualification"]["pain_summary"].startswith("Validated evidence")
    assert complete["contacts"][0]["email"] == "talent@complete.example"
    assert complete["company"]["research_evidence"]
    assert any(e["field"] == "employee_range" for e in complete["company"]["research_evidence"])

    hiring = api.company_detail("e2e-run", "hiring.example")
    assert hiring["company"]["ats_provider"] == "greenhouse"
    assert hiring["company"]["hiring_signal"] == "confirmed"
    assert len(hiring["postings"]) == 5
    assert all(p["practice_area"] for p in hiring["postings"])

    quiet = api.company_detail("e2e-run", "quiet.example")
    assert quiet["company"]["hiring_signal"] == "none"
    assert quiet["postings"] == []
    assert "no open postings" in quiet["company"]["job_discovery_notes"]

    conflict = api.company_detail("e2e-run", "conflict.example")
    assert conflict["company"]["research_outcomes"]["description"]["status"] == "found_but_rejected"
    assert conflict["company"]["research_outcomes"]["_summary"]["status"] in {
        "needs_review", "partial", "complete_with_gaps"
    }
    assert conflict["company"]["research_outcomes"]["_site_profile"]["status"] == "ok"

    flaky = api.company_detail("e2e-run", "flaky.example")
    assert flaky["company"]["research_outcomes"]["_summary"]["status"] == "failed"
    assert flaky["company"]["research_outcomes"]["_summary"]["failure_reason"] == "connection_error"
    assert flaky["company"]["research_outcomes"]["_site_profile"]["status"] == "ok"

    siteonly = api.company_detail("e2e-run", "siteonly.example")
    assert siteonly["company"]["website"]
    assert siteonly["company"]["headquarters"]
    assert siteonly["company"]["research_outcomes"]["_site_profile"]["status"] == "ok"

    retried = api.company_detail("e2e-run", "retry.example")
    assert retried["company"]["employee_count"]["value"] == 500
    assert retried["company"]["research_outcomes"]["employee_range"]["status"] == "accepted_and_mapped"