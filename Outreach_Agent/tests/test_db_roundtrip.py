"""Every model field must survive a write -> read cycle through db.py,
against the fake in-memory PostgREST in conftest.py."""

from __future__ import annotations

from outreach import db
from outreach.models import Campaign, ImportedProspect, OutreachItem


def test_prospect_survives_round_trip(conn):
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", research_run_id="run1", name="Acme",
        tier="A", score=82.5, industry="Software", region="GA",
        pain_summary="hiring urgently", recommended_angle="AI/ML angle",
        contact_email="jane@acme.com", contact_name="Jane Doe",
        contact_position="VP Eng", contact_linkedin_url="https://linkedin.com/in/jane",
        open_reqs=[{"title": "ML Engineer", "location": "Atlanta"}],
    ))
    got = db.get_prospect(conn, "acme.com")
    assert got is not None
    assert got.tier == "A"
    assert got.score == 82.5
    assert got.contact_email == "jane@acme.com"
    assert got.pain_summary == "hiring urgently"
    assert got.open_reqs == [{"title": "ML Engineer", "location": "Atlanta"}]
    assert got.has_contact is True


def test_prospect_without_contact_has_contact_false(conn):
    db.upsert_prospect(conn, ImportedProspect(research_domain="nocontact.com"))
    got = db.get_prospect(conn, "nocontact.com")
    assert got.has_contact is False


def test_prospect_assessment_snapshot_survives_round_trip(conn):
    assessment = {
        "assessment_id": "assessment-9",
        "assessment_version": 9,
        "assessed_at": "2026-11-05T12:00:00Z",
        "readiness_status": "contact_enrichment_pending",
        "category_assessments": [{
            "category": "smb", "membership": "conflicted", "evidence_status": "verified",
            "evidence_ids": ["employee-source-a", "employee-source-b"],
        }],
        "hiring_model_assessments": [{
            "model": "offshore", "openness": "open", "evidence_status": "inferred",
            "confidence": "medium", "evidence_ids": ["india-role"],
        }],
        "evidence": [{
            "evidence_id": "india-role", "criterion": "role_link", "claim": {"country": "India"},
            "evidence_status": "verified", "confidence": "high",
            "source_url": "https://example.com/careers/india",
        }],
    }
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="assessed.com", research_run_id="run1",
        raw_prospect={"research_assessment": assessment},
    ))

    got = db.get_prospect(conn, "assessed.com")

    assert got.raw_prospect["research_assessment"] == assessment
    assert got.raw_prospect["research_assessment"]["category_assessments"][0]["membership"] == "conflicted"
    assert got.raw_prospect["research_assessment"]["hiring_model_assessments"][0]["evidence_status"] == "inferred"
    assert got.raw_prospect["research_assessment"]["evidence"][0]["evidence_id"] == "india-role"


def test_legacy_reimport_preserves_assessment_but_refreshes_other_snapshot_data(conn):
    assessment = {"assessment_id": "assessment-1", "assessment_version": 1,
                  "readiness_status": "unverified_pipeline"}
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="refresh.com", research_run_id="run1", name="Old Name",
        raw_prospect={"company": {"domain": "refresh.com", "name": "Old Name"},
                      "research_assessment": assessment},
    ))

    db.upsert_prospect(conn, ImportedProspect(
        research_domain="refresh.com", research_run_id="run2", name="Updated Name",
        raw_prospect={"company": {"domain": "refresh.com", "name": "Updated Name"}},
    ))

    got = db.get_prospect(conn, "refresh.com")
    assert got.name == "Updated Name"
    assert got.raw_prospect["company"]["name"] == "Updated Name"
    assert got.raw_prospect["research_assessment"] == assessment
    assert got.research_run_ids == ["run1", "run2"]


def test_reimport_with_updated_assessment_replaces_previous_assessment(conn):
    previous = {"assessment_version": 1, "readiness_status": "unverified_pipeline"}
    updated = {"assessment_version": 2, "readiness_status": "fully_researched"}
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="updated-assessment.com", raw_prospect={"research_assessment": previous},
    ))

    db.upsert_prospect(conn, ImportedProspect(
        research_domain="updated-assessment.com", raw_prospect={"research_assessment": updated},
    ))

    got = db.get_prospect(conn, "updated-assessment.com")
    assert got.raw_prospect["research_assessment"] == updated


def test_explicit_null_assessment_remains_authoritative(conn):
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="null-assessment.com", raw_prospect={"research_assessment": {"assessment_id": "old"}},
    ))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="null-assessment.com", raw_prospect={"research_assessment": None},
    ))

    got = db.get_prospect(conn, "null-assessment.com")
    assert got.raw_prospect["research_assessment"] is None


def test_campaign_and_item_round_trip(conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", name="Q4 push", channel="email"))
    campaign = db.get_campaign(conn, "c1")
    assert campaign is not None
    assert campaign.name == "Q4 push"

    db.create_item(conn, OutreachItem(
        item_id="i1", campaign_id="c1", research_domain="acme.com",
        to_email="jane@acme.com",
    ))
    item = db.get_item(conn, "i1")
    assert item is not None
    assert item.stage == "pending"
    assert item.to_email == "jane@acme.com"


def test_set_stage_records_event(conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", channel="email"))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))

    db.set_stage(conn, "i1", "waiting", actor="user")

    item = db.get_item(conn, "i1")
    assert item.stage == "waiting"

    events = db.list_events(conn, "i1")
    assert len(events) == 1
    assert events[0].from_stage == "pending"
    assert events[0].to_stage == "waiting"
    assert events[0].actor == "user"


def test_mark_sent_updates_item_and_records_event(conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", channel="email"))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))

    db.mark_sent(conn, "i1", provider_message_id="msg-123")

    item = db.get_item(conn, "i1")
    assert item.stage == "sent"
    assert item.provider_message_id == "msg-123"
    assert item.sent_at is not None


def test_mark_failed_updates_item_and_records_error(conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", channel="email"))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))

    db.mark_failed(conn, "i1", "SMTP timeout")

    item = db.get_item(conn, "i1")
    assert item.stage == "failed"
    assert item.error == "SMTP timeout"
