from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from csg import contacts, db, discover, pipeline
from csg.models import Company, JobPosting, ProvenanceValue, ScoreResult
from csg.research_assessment import ResearchAssessmentInput, build_assessment, derive_readiness
from csg.research_assessment_generation import _assessment_payload, generate_company_assessment


class FakeConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def commit(self) -> None:
        pass

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> list[dict[str, Any]]:
        store = self.tables.setdefault(table, [])
        fields = on_conflict.split(",")
        for row in rows:
            key = tuple(row.get(field) for field in fields)
            found = next((index for index, old in enumerate(store)
                          if tuple(old.get(field) for field in fields) == key), None)
            if found is None:
                store.append(dict(row))
            else:
                store[found] = dict(row)
        return []

    def select(
        self, table: str, select: str = "*", limit: int | None = None,
        order: str | None = None, **filters: str,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.tables.get(table, [])]
        for field, value in filters.items():
            if value.startswith("eq."):
                rows = [row for row in rows if str(row.get(field)) == value[3:]]
            elif value.startswith("in.(") and value.endswith(")"):
                values = set(value[4:-1].split(","))
                rows = [row for row in rows if str(row.get(field)) in values]
        if order:
            field, _, direction = order.partition(".")
            rows.sort(key=lambda row: str(row.get(field) or ""), reverse=direction == "desc")
        if select != "*":
            fields = select.split(",")
            rows = [{field: row.get(field) for field in fields} for row in rows]
        return rows[:limit] if limit else rows

    def update(self, table: str, values: dict[str, Any], **filters: str) -> None:
        for row in self.tables.setdefault(table, []):
            if all(str(row.get(field)) == value[3:] for field, value in filters.items() if value.startswith("eq.")):
                row.update(values)


def _company_row(domain: str = "example.com", employees: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"domain": domain, "name": "Example Co", "status": "discovered"}
    if employees is not None:
        row["employee_count"] = {
            "value": employees, "source": "company profile", "url": "https://example.com/about",
            "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
        }
    return row


def _posting(
    external_id: str, location: str, *, url: str | None = None,
    source: str = "ats", description: str = "", run_id: str = "run-1",
) -> JobPosting:
    posting_url = url or f"https://example.com/jobs/{external_id}"
    same_company_host = "example.com" in posting_url
    return JobPosting(
        domain="example.com", source=source, external_id=external_id,
        title=f"Engineer {external_id}", location=location,
        url=posting_url,
        first_seen=datetime(2026, 11, 5, tzinfo=timezone.utc),
        description=description,
        raw={"_csg_validation": {
            "ownership": "verified" if same_company_host else "inferred",
            "current": "inferred" if source == "web_search" else "verified",
            "active_source": "live_ats_board" if source == "ats" else "live_careers_listing",
            "run_id": run_id,
            "retrieved_at": "2026-11-05T12:00:00Z",
            "reason": "test fixture source",
        }},
    )


def _seed_company(conn: FakeConn, employees: int | None = None) -> None:
    conn.upsert("companies", [_company_row(employees=employees)], on_conflict="domain")


def test_scored_pipeline_automatically_creates_new_company_assessment(monkeypatch):
    conn = FakeConn()
    company = Company(domain="example.com", name="Example Co")
    postings = [_posting("us", "Remote, United States")]
    saved_scores: list[ScoreResult] = []

    monkeypatch.setattr(pipeline.db, "get_company", lambda _conn, _domain: company)
    monkeypatch.setattr(pipeline.db, "upsert_company", lambda _conn, _company: None)
    monkeypatch.setattr(pipeline.db, "get_postings", lambda _conn, _domain: postings)
    monkeypatch.setattr(pipeline.db, "get_owned_postings", lambda _conn, _domain, run_id=None: postings)
    monkeypatch.setattr(pipeline.db, "upsert_postings", lambda _conn, rows: None)
    monkeypatch.setattr(pipeline.db, "save_score", lambda _conn, _run, row: saved_scores.append(row))
    monkeypatch.setattr(pipeline.ats, "detect", lambda *_args: _async_value(None))
    monkeypatch.setattr(pipeline, "discover_jobs", lambda *_args: _async_value(SimpleNamespace(
        ats_provider=None, ats_token=None, ats_detected_by=None, careers_url=None,
        method="careers_page", source="company careers page", hiring_signal="confirmed",
        notes=None, postings=postings,
    )))
    monkeypatch.setattr(pipeline, "evaluate", lambda *_args: ScoreResult(
        domain="example.com", score=80, tier="A",
    ))

    asyncio.run(pipeline._process_one(
        fetcher=None, conn=conn, cfg=SimpleNamespace(classify=lambda *_: []), company=company,
        stats=pipeline.Stats(), now=datetime(2026, 11, 5), run_id="run-1", force=False,
    ))

    assessment = db.latest_research_assessment(conn, "example.com")
    assert assessment is not None
    assert assessment.source_run_id == "run-1"
    assert assessment.hiring_model_assessments[0].model == "onshore"
    assert assessment.hiring_model_assessments[0].openness == "unknown"
    assert assessment.hiring_model_assessments[0].regional_hiring == "open"
    assert assessment.hiring_model_assessments[0].regional_hiring_evidence_status == "verified"
    assert saved_scores[0].tier == "A"


def test_company_research_evidence_maps_without_bypassing_assessment_requirements():
    conn = FakeConn()
    source_url = "https://example.com/about"
    conn.upsert("companies", [{
        **_company_row(),
        "research_evidence": [
            {"evidence_key": "size-key", "field": "employee_range",
             "criterion": "automatic_company_size", "claim": {"employee_range": "51-200"},
             "source_url": source_url, "source_name": "Company profile", "excerpt": "51-200 employees",
             "evidence_type": "direct", "confidence": "high", "retrieved_at": "2026-11-12T10:00:00Z"},
            {"evidence_key": "leadership-key", "field": "leadership",
             "criterion": "company_leadership", "claim": {"name": "A. Example", "title": "CEO"},
             "source_url": source_url, "source_name": "About", "excerpt": "A. Example, CEO",
             "evidence_type": "direct", "confidence": "high", "retrieved_at": "2026-11-12T10:00:00Z"},
            {"evidence_key": "services-key", "field": "services",
             "criterion": "company_services", "claim": {"services": "software implementation"},
             "source_url": source_url, "source_name": "Services", "excerpt": "We implement software.",
             "evidence_type": "direct", "confidence": "high", "retrieved_at": "2026-11-12T10:00:00Z"},
            {"evidence_key": "fit-key", "field": "hiring_service_fit",
             "criterion": "company_hiring_fit", "claim": {"hiring_signal": "roles listed"},
             "source_url": source_url, "source_name": "Careers", "excerpt": "Open engineering positions",
             "evidence_type": "direct", "confidence": "high", "retrieved_at": "2026-11-12T10:00:00Z"},
        ],
    }], on_conflict="domain")

    company = db.get_company(conn, "example.com")
    assert company is not None
    assessment = _assessment_payload(conn, company)
    mapped = {row.criterion for row in assessment.evidence}
    criteria = {row.criterion: row.result for row in assessment.criteria_assessments}

    assert {"automatic_company_size", "company_leadership", "company_services", "company_hiring_fit"} <= mapped
    assert criteria["company_size"] == "met"
    assert criteria["decision_maker"] == "unknown"
    assert criteria["service_fit"] == "unknown"


def test_pipeline_revalidates_cached_ats_and_excludes_unvalidated_posts_from_score(monkeypatch):
    conn = FakeConn()
    company = Company(domain="example.com", name="Example Co", ats_provider="lever", ats_token="wrong-board")
    stale_posting = _posting("wrong", "Remote, United States")
    stale_posting.raw = {}
    score_inputs: list[list[JobPosting]] = []
    detect_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(pipeline.db, "upsert_company", lambda *_args: None)
    monkeypatch.setattr(pipeline.db, "upsert_postings", lambda *_args: None)
    monkeypatch.setattr(pipeline.db, "save_score", lambda *_args: None)
    monkeypatch.setattr(pipeline.db, "get_postings", lambda *_args: [stale_posting])
    monkeypatch.setattr(pipeline, "refresh_company_assessment_safely", lambda *_args: True)

    async def no_board(*args):
        detect_calls.append((args[1], args[2]))
        return None

    async def result_with_stale_post(*_args):
        return SimpleNamespace(
            ats_provider=None, ats_token=None, ats_detected_by=None, careers_url=None,
            method="web_search", source="tavily search", hiring_signal="unknown",
            notes="no validated active roles", postings=[stale_posting],
        )

    def capture_score(_cfg, _company, postings, _now):
        score_inputs.append(postings)
        return ScoreResult(domain="example.com", score=0, tier="D")

    monkeypatch.setattr(pipeline.ats, "detect", no_board)
    monkeypatch.setattr(pipeline, "discover_jobs", result_with_stale_post)
    monkeypatch.setattr(pipeline, "evaluate", capture_score)

    asyncio.run(pipeline._process_one(
        fetcher=None, conn=conn, cfg=SimpleNamespace(classify=lambda *_: []), company=company,
        stats=pipeline.Stats(), now=datetime(2026, 11, 5), run_id="run-2", force=False,
    ))

    assert detect_calls == [("example.com", "Example Co")]
    assert score_inputs == [[]]
    assert company.ats_provider is None
    assert company.hiring_signal == "unknown"


async def _async_value(value):
    return value


def test_new_research_evidence_updates_existing_assessment_without_duplicate_versions():
    conn = FakeConn()
    _seed_company(conn)
    first = generate_company_assessment(conn, "example.com", "run-1")
    assert first.assessment_version == 1
    assert first.hiring_model_assessments[0].openness == "unknown"

    conn.upsert("job_postings", [
        _posting("us", "Remote, United States", run_id="run-2").model_dump(mode="json"),
    ], on_conflict="domain,ats_provider,external_id")
    updated = generate_company_assessment(conn, "example.com", "run-2")
    unchanged = generate_company_assessment(conn, "example.com", "run-2")

    assert updated.assessment_version == 2
    assert updated.hiring_model_assessments[0].regional_hiring == "open"
    assert updated.hiring_model_assessments[0].regional_hiring_evidence_ids
    assert unchanged.assessment_id == updated.assessment_id
    assert db.next_research_assessment_version(conn, "example.com") == 3


def test_startup_and_smb_categories_overlap_with_provenanced_company_facts():
    conn = FakeConn()
    row = _company_row(employees=100)
    row["funding"] = {
        "value": "Series B", "source": "Company profile", "url": "https://example.com/funding",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
    }
    conn.upsert("companies", [row], on_conflict="domain")

    assessment = generate_company_assessment(conn, "example.com")
    memberships = {item.category: item.membership for item in assessment.category_assessments}

    assert memberships["startup"] == "unknown"
    assert memberships["smb"] == "match"
    assert "system_integrator" in memberships


def test_funding_without_explicit_startup_context_does_not_match_startup():
    conn = FakeConn()
    row = _company_row(employees=100)
    row["funding"] = {
        "value": "Series B", "source": "Company profile", "url": "https://example.com/funding",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
    }
    conn.upsert("companies", [row], on_conflict="domain")
    assessment = generate_company_assessment(conn, "example.com")
    assert next(item for item in assessment.category_assessments if item.category == "startup").membership == "unknown"


def test_researched_identity_facts_update_basic_criteria_and_category_without_job_location():
    conn = FakeConn()
    row = _company_row()
    row["website"] = {
        "value": "https://example.com", "source": "search", "url": "https://example.com/about",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
    }
    row["headquarters"] = {
        "value": {"city": "Austin", "state": "TX", "country": "United States"},
        "source": "search", "url": "https://example.com/about",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
    }
    row["company_type"] = {
        "value": "system_integrator", "source": "search", "url": "https://example.com/about",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high",
    }
    conn.upsert("companies", [row], on_conflict="domain")

    assessment = generate_company_assessment(conn, "example.com")
    criteria = {item.criterion: item for item in assessment.criteria_assessments}
    categories = {item.category: item for item in assessment.category_assessments}

    assert criteria["website"].result == "met"
    assert criteria["us_hq"].result == "met"
    assert categories["system_integrator"].membership == "match"
    assert all(item.source_url == "https://example.com/about" for item in assessment.evidence)


def test_job_location_does_not_establish_us_headquarters():
    conn = FakeConn()
    _seed_company(conn)
    conn.upsert("job_postings", [_posting("us", "Austin, TX").model_dump(mode="json")],
                on_conflict="domain,ats_provider,external_id")

    assessment = generate_company_assessment(conn, "example.com")
    assert next(item for item in assessment.criteria_assessments if item.criterion == "us_hq").result == "unknown"


def test_system_integrator_needs_explicit_company_identity_evidence():
    conn = FakeConn()
    _seed_company(conn, employees=300)
    conn.upsert("job_postings", [
        _posting("generic", "Remote, United States", description="Experience with systems integration.")
        .model_dump(mode="json"),
    ], on_conflict="domain,ats_provider,external_id")
    first = generate_company_assessment(conn, "example.com")
    assert next(row for row in first.category_assessments
                if row.category == "system_integrator").membership == "unknown"

    conn.upsert("job_postings", [
        _posting("identity", "Remote, United States",
                 description="We are a systems integrator serving public agencies.")
        .model_dump(mode="json"),
    ], on_conflict="domain,ats_provider,external_id")
    second = generate_company_assessment(conn, "example.com")
    match = next(row for row in second.category_assessments
                 if row.category == "system_integrator")

    assert match.membership == "match"
    assert match.evidence_ids


def test_explicit_us_latam_and_pakistan_postings_support_their_own_models():
    conn = FakeConn()
    _seed_company(conn)
    postings = [
        _posting("us", "Austin, TX"),
        _posting("latam", "Remote, Mexico"),
        _posting("pk", "Remote, Pakistan"),
    ]
    conn.upsert("job_postings", [row.model_dump(mode="json") for row in postings],
                on_conflict="domain,ats_provider,external_id")

    assessment = generate_company_assessment(conn, "example.com")
    models = {row.model: row for row in assessment.hiring_model_assessments}

    assert all(row.openness == "unknown" for row in models.values())
    assert all(row.regional_hiring == "open" for row in models.values())
    assert all(row.regional_hiring_evidence_status == "verified" for row in models.values())
    assert all(row.regional_hiring_evidence_ids for row in models.values())
    assert all(row.evidence_status == "unavailable" and not row.evidence_ids for row in models.values())


def test_estimated_firmographics_and_aggregator_roles_remain_inferred():
    conn = FakeConn()
    row = _company_row(employees=100)
    row["employee_count"] = {
        "value": 100, "source": "business directory", "url": "https://directory.example/profile",
        "fetched_at": "2026-11-05T12:00:00Z", "method": "estimated", "confidence": "low",
    }
    conn.upsert("companies", [row], on_conflict="domain")
    conn.upsert("job_postings", [
        _posting("latam-search", "Remote, Mexico", source="web_search",
                 url="https://jobs.example/role").model_dump(mode="json"),
    ], on_conflict="domain,ats_provider,external_id")

    assessment = generate_company_assessment(conn, "example.com")
    categories = {row.category: row for row in assessment.category_assessments}
    nearshore = next(row for row in assessment.hiring_model_assessments if row.model == "nearshore")

    assert categories["smb"].membership == "unknown"
    assert categories["smb"].evidence_status == "unavailable"
    assert nearshore.openness == "unknown"
    assert nearshore.regional_hiring == "unknown"
    assert nearshore.regional_hiring_evidence_status == "unavailable"
    assert nearshore.confidence is None


def test_india_only_posting_never_claims_verified_pakistan_openness():
    conn = FakeConn()
    _seed_company(conn)
    conn.upsert("job_postings", [
        _posting("india", "Remote, India").model_dump(mode="json"),
    ], on_conflict="domain,ats_provider,external_id")

    assessment = generate_company_assessment(conn, "example.com")
    offshore = next(row for row in assessment.hiring_model_assessments if row.model == "offshore")

    assert offshore.openness == "unknown"
    assert offshore.evidence_status == "unavailable"
    assert offshore.regional_hiring == "unknown"


def test_unknown_evidence_stays_unknown_and_existing_conflicts_are_preserved():
    conn = FakeConn()
    _seed_company(conn)
    first = generate_company_assessment(conn, "example.com")
    assert all(row.membership == "unknown" for row in first.category_assessments)
    assert all(row.openness == "unknown" for row in first.hiring_model_assessments)
    assert not first.evidence

    conflicting = build_assessment(
        "example.com",
        ResearchAssessmentInput(
            category_assessments=[{
                "category": "smb", "membership": "conflicted", "evidence_status": "inferred",
            }],
            hiring_model_assessments=[{
                "model": "nearshore", "openness": "mixed", "evidence_status": "inferred",
            }],
        ),
        assessment_version=2,
        assessment_id="manual-conflict",
    )
    db.save_research_assessment(conn, conflicting)
    refreshed = generate_company_assessment(conn, "example.com")

    assert next(row for row in refreshed.category_assessments if row.category == "smb").membership == "conflicted"
    nearshore = next(row for row in refreshed.hiring_model_assessments if row.model == "nearshore")
    assert nearshore.openness == "unknown"
    assert nearshore.regional_hiring == "mixed"


def test_legacy_job_evidence_is_not_carried_forward_without_current_validation():
    conn = FakeConn()
    _seed_company(conn)
    legacy_evidence = {
        "evidence_id": "legacy-sophos-role",
        "criterion": "automatic_role_location",
        "claim": {"title": "Engineer", "location": "United States", "regions": ["onshore"], "current": True},
        "evidence_status": "verified", "confidence": "high",
        "source_name": "lever", "source_type": "official_job_board",
        "source_url": "https://jobs.lever.co/sophos/role-1",
        "source_published_at": None, "retrieved_at": "2026-09-20T12:00:00Z",
        "excerpt": "United States", "conflict_group": None, "unavailable_reason": None,
    }
    legacy = build_assessment(
        "example.com",
        ResearchAssessmentInput(
            evidence=[legacy_evidence],
            hiring_model_assessments=[{
                "model": "onshore", "openness": "open", "evidence_status": "verified",
                "evidence_ids": ["legacy-sophos-role"],
            }],
        ),
        assessment_version=1,
        assessment_id="legacy-assessment",
    )
    db.save_research_assessment(conn, legacy)

    refreshed = generate_company_assessment(conn, "example.com", "new-run")
    onshore = next(row for row in refreshed.hiring_model_assessments if row.model == "onshore")

    assert onshore.openness == "unknown"
    assert onshore.regional_hiring == "unknown"
    assert all(row.evidence_id != "legacy-sophos-role" for row in refreshed.evidence)
    assert db.get_research_assessment(conn, "legacy-assessment") is not None


def test_readiness_uses_existing_backend_derivation_and_missing_sources_stay_empty():
    conn = FakeConn()
    _seed_company(conn)
    assessment = generate_company_assessment(conn, "example.com")

    assert assessment.readiness_status == derive_readiness(assessment)
    assert assessment.readiness_status == "unverified_pipeline"
    assert assessment.evidence == []
    assert all(row.membership == "unknown" for row in assessment.category_assessments)
    assert all(row.openness == "unknown" and not row.evidence_ids
               for row in assessment.hiring_model_assessments)


def test_hunter_results_refresh_assessment_without_validating_identity(monkeypatch):
    conn = FakeConn()
    _seed_company(conn)
    first = generate_company_assessment(conn, "example.com")

    async def fake_find_contacts(_domain, limit):
        assert limit == contacts.DEFAULT_LIMIT
        return [{
            "email": "jordan@example.com", "first_name": "Jordan", "last_name": "Example",
            "position": "Chief Technology Officer", "verification": "valid",
        }]

    monkeypatch.setattr(contacts, "find_contacts", fake_find_contacts)
    stats = asyncio.run(contacts.discover_run(conn, ["example.com"]))
    updated = db.latest_research_assessment(conn, "example.com")

    assert stats["contacts_saved"] == 1
    assert updated.assessment_version == first.assessment_version + 1
    assert updated.decision_makers[0].hunter_outcome == "valid"
    assert updated.decision_makers[0].linkedin_validation == "not_validated"
    assert updated.readiness_status == "unverified_pipeline"


def test_cached_contacts_allow_assessment_retry_after_transient_failure(monkeypatch):
    from csg import research_assessment_generation

    conn = FakeConn()
    _seed_company(conn)
    db.save_contacts(conn, "example.com", [{
        "email": "jordan@example.com", "first_name": "Jordan", "last_name": "Example",
        "position": "Chief Technology Officer", "verification": "valid",
    }], source="hunter")
    original = research_assessment_generation.generate_company_assessment
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary assessment persistence failure")
        return original(*args, **kwargs)

    async def should_not_search(*_args, **_kwargs):
        raise AssertionError("cached Hunter contacts should not be searched again")

    monkeypatch.setattr(research_assessment_generation, "generate_company_assessment", fail_once)
    monkeypatch.setattr(contacts, "find_contacts", should_not_search)
    first = asyncio.run(contacts.discover_run(conn, ["example.com"]))
    assert first["skipped"] == 1
    assert db.latest_research_assessment(conn, "example.com") is None

    second = asyncio.run(contacts.discover_run(conn, ["example.com"]))
    retried = db.latest_research_assessment(conn, "example.com")
    assert second["skipped"] == 1
    assert attempts == 2
    assert retried is not None
    assert retried.decision_makers[0].hunter_outcome == "valid"


def test_assessment_generation_failure_does_not_interrupt_pipeline(monkeypatch, capsys):
    conn = FakeConn()
    company = Company(domain="example.com", name="Example Co")
    saved_scores: list[ScoreResult] = []
    monkeypatch.setattr(pipeline.db, "get_company", lambda _conn, _domain: company)
    monkeypatch.setattr(pipeline.db, "upsert_company", lambda _conn, _company: None)
    monkeypatch.setattr(pipeline.db, "get_postings", lambda _conn, _domain: [])
    monkeypatch.setattr(pipeline.db, "upsert_postings", lambda _conn, rows: None)
    monkeypatch.setattr(pipeline.db, "save_score", lambda _conn, _run, row: saved_scores.append(row))
    monkeypatch.setattr(pipeline.ats, "detect", lambda *_args: _async_value(None))
    monkeypatch.setattr(pipeline, "discover_jobs", lambda *_args: _async_value(SimpleNamespace(
        ats_provider=None, ats_token=None, ats_detected_by=None, careers_url=None,
        method="unknown", source=None, hiring_signal="unknown", notes="not found", postings=[],
    )))
    monkeypatch.setattr(pipeline, "evaluate", lambda *_args: ScoreResult(
        domain="example.com", score=80, tier="A",
    ))
    monkeypatch.setattr(
        "csg.research_assessment_generation.generate_company_assessment",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("assessment store unavailable")),
    )

    asyncio.run(pipeline._process_one(
        fetcher=None, conn=conn, cfg=SimpleNamespace(classify=lambda *_: []), company=company,
        stats=pipeline.Stats(), now=datetime(2026, 11, 5), run_id="run-1", force=False,
    ))

    assert saved_scores and company.status == "scored"
    assert "assessment store unavailable" in capsys.readouterr().out

    monkeypatch.setattr(discover.db, "get_scores", lambda _conn, _run: [{
        "domain": "example.com", "tier": "A", "score": 80, "disqualified": False,
    }])
    monkeypatch.setattr(discover.export, "to_csv", lambda *_args, **_kwargs: "x.csv")
    monkeypatch.setattr(discover.export, "to_jsonl", lambda *_args, **_kwargs: "x.jsonl")
    stages: list[str] = []

    async def qualify(_conn, _run_id, *, domains, on_progress=None):
        stages.append("qualify")
        return {"qualified": len(domains)}

    async def find_contacts(_conn, domains, *, on_progress=None):
        stages.append("contacts")
        return {"found": len(domains)}

    async def handoff(*_args, **_kwargs):
        stages.append("handoff")

    monkeypatch.setattr(discover.qualify_mod, "qualify_run", qualify)
    monkeypatch.setattr(discover.contacts_mod, "discover_run", find_contacts)
    monkeypatch.setattr(discover, "_handoff_to_outreach", handoff)
    stats = discover.DiscoveryStats(region="GA", run_id="run-1")
    asyncio.run(discover._auto_qualify_and_contact(conn, "run-1", 1, stats, None))
    asyncio.run(discover._handoff_to_outreach(SimpleNamespace(), "run-1", stats, None))

    assert stages == ["qualify", "contacts", "handoff"]