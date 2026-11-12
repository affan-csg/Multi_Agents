"""Every Company/JobPosting field must survive a write->read cycle.

Regression cover for the worst bug in this codebase: upsert_company wrote
neither revenue, funding, description nor region, and _row_to_company read
none of them back -- so every enrichment result (a paid LLM call) was
silently discarded before scoring ever saw it. Same for delivery_fit and
practice_area on postings, which made the CSG Delivery Fit dimension score
zero for every company, always.

These use a fake in-memory PostgREST rather than a live Supabase, so they
run offline and still exercise the real db.py code paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from csg import db
from csg.models import Company, JobPosting


class FakeConn:
    """Minimal stand-in for SupabaseConn: dict tables, PostgREST-ish filters."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def _key(self, table: str, row: dict[str, Any]) -> tuple:
        keys = {
            "companies": ("domain",),
            "job_postings": ("domain", "ats_provider", "external_id"),
        }.get(table, ("domain",))
        return tuple(row.get(k) for k in keys)

    @staticmethod
    def _validate_required_evidence(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            for field in ("classification_evidence", "research_evidence", "research_outcomes"):
                assert row.get(field) is not None, f"companies.{field} is NOT NULL"

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str = "") -> None:
        if table == "companies":
            self._validate_required_evidence(rows)
        store = self.tables.setdefault(table, [])
        for row in rows:
            k = self._key(table, row)
            for i, existing in enumerate(store):
                if self._key(table, existing) == k:
                    store[i] = dict(row)
                    break
            else:
                store.append(dict(row))

    def insert_if_absent(self, table: str, rows: list[dict[str, Any]], on_conflict: str = "") -> list[dict[str, Any]]:
        if table == "companies":
            self._validate_required_evidence(rows)
        store = self.tables.setdefault(table, [])
        inserted = []
        for row in rows:
            if not any(self._key(table, existing) == self._key(table, row) for existing in store):
                store.append(dict(row))
                inserted.append(row)
        return inserted

    def select(self, table: str, select: str = "*", limit: int | None = None,
               order: str | None = None, **filters: str) -> list[dict[str, Any]]:
        rows = self.tables.get(table, [])
        for field, expr in filters.items():
            if expr.startswith("eq."):
                want = expr[3:]
                rows = [r for r in rows if str(r.get(field)) == want]
            elif expr.startswith("in."):
                wanted = set(expr[4:-1].split(","))
                rows = [r for r in rows if str(r.get(field)) in wanted]
        return rows[:limit] if limit else list(rows)

    def update(self, table: str, values: dict[str, Any], **filters: str) -> None:
        for row in self.select(table, **filters):
            row.update(values)

    def commit(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def conn():
    return FakeConn()


def test_company_firmographics_survive_the_round_trip(conn):
    """The bug: a paid enrichment call was discarded on write."""
    db.upsert_company(conn, Company(
        domain="acme.com", name="Acme", region="GA",
        description="AI infrastructure company.",
        employee_count={"value": 400, "source": "gemini", "method": "estimated",
                        "confidence": "medium"},
        revenue={"value": "$25M", "source": "gemini", "method": "estimated",
                 "confidence": "medium"},
        funding={"value": "$40M Series B", "source": "crunchbase",
                 "method": "reported", "confidence": "high"},
    ))
    got = db.get_company(conn, "acme.com")

    assert got is not None
    assert got.region == "GA"
    assert got.description == "AI infrastructure company."
    assert got.employees == 400
    assert got.revenue.value == "$25M"
    assert got.funding.value == "$40M Series B"
    assert got.funding.method == "reported"


def test_every_company_field_is_persisted(conn):
    """Guards the whole class of bug, not just today's four fields."""
    db.upsert_company(conn, Company(
        domain="acme.com", name="Acme", region="GA", industry="Software",
        website={"value": "https://acme.com", "source": "search", "url": "https://acme.com/about",
                  "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high"},
        headquarters={"value": {"city": "Atlanta", "state": "GA", "country": "United States"},
                      "source": "search", "url": "https://acme.com/about",
                      "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high"},
        company_type={"value": "smb", "source": "search", "url": "https://acme.com/about",
                      "fetched_at": "2026-11-05T12:00:00Z", "method": "reported", "confidence": "high"},
        hq_state="GA", description="d", employee_count=400, revenue="$25M",
        funding="$40M", ats_provider="greenhouse", ats_token="acme",
        ats_detected_by="careers_page", careers_url="https://acme.com/careers",
        status="scored", job_discovery_method="ats", job_source="greenhouse",
        hiring_signal="confirmed", job_discovery_notes="n/a",
    ))
    stored = conn.tables["companies"][0]
    for field in Company.model_fields:
        assert field in stored, f"{field} is never written to the companies table"

    got = db.get_company(conn, "acme.com")
    for field in Company.model_fields:
        if field == "dq_reason":  # legitimately None for a company that passed
            continue
        assert getattr(got, field) is not None, f"{field} does not survive read-back"


def test_provenance_is_stored_as_json_not_python_objects(conn):
    """JSONB needs primitives -- a pydantic object would fail to serialize."""
    db.upsert_company(conn, Company(domain="acme.com", employee_count=400))
    stored = conn.tables["companies"][0]["employee_count"]
    assert isinstance(stored, dict)
    assert stored["value"] == 400


def test_coalesce_preserves_prior_research(conn):
    """A later run that learns nothing new must not erase what we know."""
    db.upsert_company(conn, Company(
        domain="acme.com", name="Acme", revenue="$25M", employee_count=400))
    # Second pass: ATS detection only, no firmographics.
    db.upsert_company(conn, Company(
        domain="acme.com", ats_provider="lever", ats_token="acme", status="ats_found"))

    got = db.get_company(conn, "acme.com")
    assert got.revenue.value == "$25M", "prior research was wiped"
    assert got.employees == 400
    assert got.ats_provider == "lever"


def test_empty_reassessment_preserves_evidence_and_outcomes(conn):
    classification = [{
        "category": "system_integrator", "supports": True, "fact": "services",
        "why": "documented integration services", "source_name": "Services",
        "source_url": "https://acme.com/services", "excerpt": "We implement software.",
        "confidence": "high", "evidence_type": "direct",
    }]
    evidence = [{
        "evidence_key": "stable-key", "field": "services", "criterion": "company_services",
        "claim": {"services": "implementation"}, "source_url": "https://acme.com/services",
        "source_name": "Services", "excerpt": "We implement software.",
        "evidence_type": "direct", "confidence": "high",
    }]
    outcomes = {"services": {"status": "accepted_and_mapped", "criterion": "company_services"}}
    db.upsert_company(conn, Company(
        domain="acme.com",
        website={"value": "https://acme.com", "source": "operator_approved",
                 "method": "reported", "confidence": "high"},
        classification_evidence=classification,
        research_evidence=evidence, research_outcomes=outcomes,
    ))
    db.upsert_company(conn, Company(domain="acme.com", ats_provider="lever"))

    refreshed = db.get_company(conn, "acme.com")
    assert refreshed is not None
    assert refreshed.website is not None
    assert refreshed.website.value == "https://acme.com"
    assert refreshed.website.source == "operator_approved"
    assert refreshed.classification_evidence == classification
    assert refreshed.research_evidence == evidence
    assert refreshed.research_outcomes == outcomes


def test_company_claim_and_upsert_use_non_null_evidence_defaults(conn):
    company = Company(domain="acme.com")
    assert db.claim_company(conn, company) is True
    claimed = conn.tables["companies"][0]
    assert claimed["classification_evidence"] == []
    assert claimed["research_evidence"] == []
    assert claimed["research_outcomes"] == {}

    nullable_company = Company.model_construct(
        domain="nullable.example", classification_evidence=None,
        research_evidence=None, research_outcomes=None,
    )
    db.upsert_company(conn, nullable_company)
    updated = next(row for row in conn.tables["companies"] if row["domain"] == "nullable.example")
    assert updated["classification_evidence"] == []
    assert updated["research_evidence"] == []
    assert updated["research_outcomes"] == {}


def test_posting_classification_survives_the_round_trip(conn):
    """The bug: pipeline set these, saved, re-read -- and got None back, so the
    CSG Delivery Fit dimension scored zero for every company."""
    db.upsert_company(conn, Company(domain="acme.com"))
    db.upsert_postings(conn, [JobPosting(
        domain="acme.com", ats_provider="greenhouse", external_id="1",
        title="AI Engineer", location="Atlanta, GA", description="",
        practice_area="ai_ml", delivery_fit="LATAM")])

    got = db.get_postings(conn, "acme.com")
    assert len(got) == 1
    assert got[0].practice_area == "ai_ml"
    assert got[0].delivery_fit == "LATAM"


def test_posting_validation_survives_round_trip_and_scopes_current_run(conn):
    db.upsert_company(conn, Company(domain="acme.com"))
    db.upsert_postings(conn, [
        JobPosting(
            domain="acme.com", ats_provider="lever", external_id="verified",
            title="Engineer", url="https://jobs.lever.co/acme/verified",
            raw={"_csg_validation": {"ownership": "verified", "current": "verified",
                                      "active_source": "live_ats_board", "run_id": "run-2"}},
        ),
        JobPosting(
            domain="acme.com", ats_provider="lever", external_id="stale",
            title="Old Engineer", url="https://jobs.lever.co/acme/stale",
            raw={"_csg_validation": {"ownership": "verified", "current": "verified",
                                      "active_source": "live_ats_board", "run_id": "run-1"}},
        ),
        JobPosting(
            domain="acme.com", ats_provider="lever", external_id="mismatch",
            title="Other Employer", url="https://jobs.lever.co/other/mismatch",
        ),
    ])

    assert {row.external_id for row in db.get_validated_postings(conn, "acme.com", "run-2")} == {"verified"}
    assert {row.external_id for row in db.get_validated_postings(conn, "acme.com")} == {"verified", "stale"}


# --- structural audit ----------------------------------------------------
# The bugs above all shared one shape: a field was added to a model (and to
# schema.sql) but one leg of the read/write/export path was never updated, so
# data vanished silently. These tests inspect the code itself, so adding a
# field to any model fails the suite until every leg is wired up.

def _source(fn):
    import inspect
    return inspect.getsource(fn)


def test_every_company_field_is_in_the_write_and_read_path():
    import re
    from csg.models import Company

    written = set(db._COALESCE_FIELDS) | {
        "domain", "last_checked", "status", "dq_reason", "first_seen"}
    read_src = _source(db._row_to_company)

    missing_write = [f for f in Company.model_fields if f not in written]
    missing_read = [f for f in Company.model_fields
                    if not re.search(rf"\b{f}=", read_src)]
    assert not missing_write, f"never persisted: {missing_write}"
    assert not missing_read, f"never read back: {missing_read}"


def test_every_posting_field_is_in_the_write_and_read_path():
    import re
    from csg.models import JobPosting

    write_src, read_src = _source(db.upsert_postings), _source(db._row_to_posting)
    for field in JobPosting.model_fields:
        column = "raw_json" if field == "raw" else field
        assert f'"{column}"' in write_src, f"{field} is never persisted"
        if field != "raw":
            assert re.search(rf"\b{field}=", read_src), f"{field} is never read back"


def test_every_prospect_field_is_exported():
    """Prospect is the Outreach Agent's contract -- a field it declares but
    export never fills is a silently empty promise.

    outreach_ready/has_linkedin_contact are set via
    **Prospect.compute_outreach_flags(...) (kwargs-unpacked), not a literal
    `field=` assignment, so they're checked separately against the actual
    computed values below rather than by source regex.
    """
    import re
    from csg import export
    from csg.models import Prospect

    computed_via_helper = {"outreach_ready", "has_linkedin_contact"}
    src = _source(export.to_jsonl)
    assert "compute_outreach_flags" in src, \
        "outreach readiness flags are no longer wired into the export"

    missing = [f for f in Prospect.model_fields
               if f != "schema_version" and f not in computed_via_helper
               and not re.search(rf"\b{f}=", src)]
    assert not missing, f"declared on Prospect but never exported: {missing}"


def test_outreach_readiness_rule_is_email_required_linkedin_bonus():
    """The actual business rule (see models.Prospect.compute_outreach_flags):
    a company is outreach-ready as soon as it has one contact with an email.
    LinkedIn is recorded but never gates readiness -- Hunter.io's own parser
    (contacts._parse_emails) already discards any entry without an email, so
    a non-empty contacts list is proof enough."""
    from csg.models import Prospect

    email_only = Prospect.compute_outreach_flags(
        [{"email": "a@acme.com", "linkedin_url": None}])
    assert email_only == {"outreach_ready": True, "has_linkedin_contact": False}

    email_and_linkedin = Prospect.compute_outreach_flags(
        [{"email": "a@acme.com", "linkedin_url": "https://linkedin.com/in/a"}])
    assert email_and_linkedin == {"outreach_ready": True, "has_linkedin_contact": True}

    no_contacts = Prospect.compute_outreach_flags([])
    assert no_contacts == {"outreach_ready": False, "has_linkedin_contact": False}
