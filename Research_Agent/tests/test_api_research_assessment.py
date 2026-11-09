from fastapi.testclient import TestClient

from csg import api, db
from csg.models import Company
from csg.research_assessment import ResearchAssessmentInput, build_assessment


class FakeConn:
    def close(self):
        pass


def test_create_and_get_assessment_are_additive_api_routes(monkeypatch):
    conn = FakeConn()
    stored = []
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(db, "get_company", lambda _conn, domain: Company(domain=domain))
    monkeypatch.setattr(db, "next_research_assessment_version", lambda _conn, _domain: 1)
    monkeypatch.setattr(db, "save_research_assessment", lambda _conn, assessment: stored.append(assessment))
    monkeypatch.setattr(db, "latest_research_assessment", lambda _conn, _domain: stored[-1] if stored else None)

    with TestClient(api.app) as client:
        created = client.post("/companies/example.com/research-assessments", json={
            "hiring_model_assessments": [{
                    "model": "offshore", "regional_hiring": "open",
                    "regional_hiring_evidence_status": "inferred",
                    "regional_hiring_evidence_ids": ["india"],
                    "openness": "unknown", "evidence_status": "unavailable",
            }],
            "evidence": [{
                "evidence_id": "india", "criterion": "role_link", "claim": {"country": "India"},
                "evidence_status": "verified", "confidence": "high", "source_name": "Careers",
                "source_type": "official_job_board", "source_url": "https://example.com/jobs",
            }],
        })
        fetched = client.get("/companies/example.com/research-assessment")

    assert created.status_code == 200
    assert created.json()["assessment_version"] == 1
    assert created.json()["readiness_status"] == "unverified_pipeline"
    assert created.json()["hiring_model_assessments"][0]["openness"] == "unknown"
    assert created.json()["hiring_model_assessments"][0]["evidence_status"] == "unavailable"
    assert created.json()["hiring_model_assessments"][0]["regional_hiring"] == "open"
    assert created.json()["hiring_model_assessments"][0]["regional_hiring_evidence_status"] == "inferred"
    assert fetched.status_code == 200
    assert fetched.json()["assessment"]["assessment_id"] == created.json()["assessment_id"]


def test_assessment_api_rejects_legacy_ambiguous_hiring_openness(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(db, "get_company", lambda _conn, domain: Company(domain=domain))

    with TestClient(api.app) as client:
        response = client.post("/companies/example.com/research-assessments", json={
            "hiring_model_assessments": [{
                "model": "onshore", "openness": "open", "evidence_status": "verified",
                "evidence_ids": ["role"],
            }],
            "evidence": [{
                "evidence_id": "role", "criterion": "role_link", "claim": {"country": "US"},
                "evidence_status": "verified", "confidence": "high", "source_name": "Careers",
                "source_type": "official_job_board", "source_url": "https://example.com/jobs",
            }],
        })

    assert response.status_code == 422


def test_assessment_api_returns_404_for_unknown_company(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: FakeConn())
    monkeypatch.setattr(db, "get_company", lambda _conn, _domain: None)

    with TestClient(api.app) as client:
        response = client.get("/companies/missing.com/research-assessment")

    assert response.status_code == 404


def test_existing_company_detail_keeps_legacy_fields_and_adds_assessment(monkeypatch):
    assessment = build_assessment("example.com", ResearchAssessmentInput(), assessment_version=1)
    monkeypatch.setattr(db, "connect", lambda: FakeConn())
    monkeypatch.setattr(db, "get_company", lambda _conn, domain: Company(domain=domain, name="Example"))
    monkeypatch.setattr(db, "get_scores", lambda _conn, _run_id: [])
    monkeypatch.setattr(db, "get_postings", lambda _conn, _domain: [])
    monkeypatch.setattr(db, "get_qualification", lambda _conn, _domain, _run_id: None)
    monkeypatch.setattr(db, "get_qualification_attempt", lambda _conn, _domain, _run_id: None)
    monkeypatch.setattr(db, "get_contacts", lambda _conn, _domain: [])
    monkeypatch.setattr(db, "latest_research_assessment", lambda _conn, _domain: assessment)

    result = api.company_detail("run-1", "example.com")

    assert result["company"]["name"] == "Example"
    assert result["postings"] == []
    assert result["contacts"] == []
    assert result["research_assessment"]["assessment_id"] == assessment.assessment_id
    assert result["outreach_ready"] is False


def test_company_list_adds_assessment_without_dropping_legacy_rows(monkeypatch):
    assessment = build_assessment("example.com", ResearchAssessmentInput(), assessment_version=1)
    rows = [
        {"domain": "example.com", "run_id": "run-1", "tier": "A"},
        {"domain": "legacy.com", "run_id": "run-1", "tier": "B"},
    ]
    monkeypatch.setattr(db, "connect", lambda: FakeConn())
    monkeypatch.setattr(db, "get_scores", lambda _conn, _run_id: rows)
    monkeypatch.setattr(db, "get_run_matches", lambda _conn, _run_id: [])
    monkeypatch.setattr(
        db, "latest_research_assessment_summaries",
        lambda _conn, domains: {"example.com": {
            "assessment_id": assessment.assessment_id,
            "domain": assessment.domain,
            "assessment_version": assessment.assessment_version,
            "schema_version": assessment.schema_version,
            "source_run_id": assessment.source_run_id,
            "readiness_status": assessment.readiness_status,
            "assessed_at": assessment.assessed_at.isoformat(),
            "category_assessments": [],
            "hiring_model_assessments": [],
        }},
    )

    with TestClient(api.app) as client:
        response = client.get("/runs/run-1/companies")

    assert response.status_code == 200
    result = response.json()
    assert [row["domain"] for row in result] == ["example.com", "legacy.com"]
    assert result[0]["research_assessment"]["assessment_id"] == assessment.assessment_id
    assert result[1]["research_assessment"] is None