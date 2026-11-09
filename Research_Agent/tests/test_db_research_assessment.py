from typing import Any

from csg import db
from csg.research_assessment import CompanyEvidence, ResearchAssessmentInput, build_assessment


class FakeConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        store = self.tables.setdefault(table, [])
        key_fields = on_conflict.split(",")
        for row in rows:
            key = tuple(row.get(field) for field in key_fields)
            for index, existing in enumerate(store):
                if tuple(existing.get(field) for field in key_fields) == key:
                    store[index] = dict(row)
                    break
            else:
                store.append(dict(row))

    def select(self, table: str, select: str = "*", limit: int | None = None,
               order: str | None = None, **filters: str) -> list[dict[str, Any]]:
        rows = list(self.tables.get(table, []))
        for field, value in filters.items():
            if value.startswith("eq."):
                rows = [row for row in rows if str(row.get(field)) == value[3:]]
            elif value.startswith("in.(") and value.endswith(")"):
                values = set(value[4:-1].split(","))
                rows = [row for row in rows if str(row.get(field)) in values]
        if order:
            field, _, direction = order.partition(".")
            rows.sort(key=lambda row: row.get(field) or "", reverse=direction == "desc")
        if select != "*":
            fields = select.split(",")
            rows = [{field: row.get(field) for field in fields} for row in rows]
        return rows[:limit] if limit else rows


def test_research_assessment_and_evidence_round_trip():
    conn = FakeConn()
    evidence = CompanyEvidence(
        evidence_id="source-1", criterion="company_size", claim={"employees": 100},
        evidence_status="inferred", confidence="medium", source_name="Company profile",
        source_type="third_party", source_url="https://example.com/profile",
    )
    payload = ResearchAssessmentInput(
        category_assessments=[{
            "category": "startup", "membership": "match", "evidence_status": "inferred",
            "evidence_ids": ["source-1"],
        }],
        hiring_model_assessments=[{
            "model": "offshore", "openness": "open", "evidence_status": "inferred",
            "confidence": "medium", "evidence_ids": ["source-1"],
        }],
        decision_makers=[{
            "name": "Jordan Example", "title": "CTO", "persona": "technical_executive",
            "hunter_outcome": "accept_all", "hunter_email": "jordan@example.com",
            "hunter_checked_at": "2026-11-05T12:00:00Z",
        }],
        evidence=[evidence],
    )
    assessment = build_assessment("example.com", payload, assessment_version=1,
                                  assessment_id="assessment-1")

    db.save_research_assessment(conn, assessment)
    restored = db.get_research_assessment(conn, "assessment-1")

    assert restored is not None
    assert restored.domain == "example.com"
    assert restored.assessment_version == 1
    assert restored.readiness_status == "unverified_pipeline"
    assert restored.evidence[0].source_url == "https://example.com/profile"
    assert restored.hiring_model_assessments[0].openness == "unknown"
    assert restored.hiring_model_assessments[0].regional_hiring == "open"
    assert restored.hiring_model_assessments[0].regional_hiring_evidence_status == "inferred"
    assert restored.decision_makers[0].hunter_outcome == "accept_all"
    assert restored.decision_makers[0].linkedin_validation == "not_validated"


def test_latest_assessment_and_next_version_are_domain_scoped():
    conn = FakeConn()
    first = build_assessment("example.com", ResearchAssessmentInput(), assessment_version=1,
                             assessment_id="assessment-1")
    second = build_assessment("example.com", ResearchAssessmentInput(), assessment_version=2,
                              assessment_id="assessment-2")
    db.save_research_assessment(conn, first)
    db.save_research_assessment(conn, second)

    assert db.next_research_assessment_version(conn, "example.com") == 3
    assert db.latest_research_assessment(conn, "example.com").assessment_id == "assessment-2"
    assert db.latest_research_assessment(conn, "missing.com") is None


def test_batch_latest_assessment_summaries_include_only_latest_version():
    conn = FakeConn()
    first = build_assessment(
        "example.com", ResearchAssessmentInput(), assessment_version=1,
        assessment_id="assessment-1",
    )
    evidence = CompanyEvidence(
        evidence_id="source-2", criterion="company_size", claim={"employees": 100},
        evidence_status="verified", confidence="high", source_name="Company site",
        source_type="official_company", source_url="https://example.com/about",
    )
    second = build_assessment(
        "example.com", ResearchAssessmentInput(evidence=[evidence]), assessment_version=2,
        assessment_id="assessment-2",
    )
    other = build_assessment(
        "other.com", ResearchAssessmentInput(), assessment_version=1,
        assessment_id="assessment-3",
    )
    db.save_research_assessment(conn, first)
    db.save_research_assessment(conn, second)
    db.save_research_assessment(conn, other)

    assessments = db.latest_research_assessment_summaries(conn, ["example.com", "missing.com"])

    assert set(assessments) == {"example.com"}
    assert assessments["example.com"]["assessment_id"] == "assessment-2"
    assert assessments["example.com"]["assessment_version"] == 2
    assert assessments["example.com"]["category_assessments"] == []
    assert db.latest_research_assessment_summaries(conn, []) == {}