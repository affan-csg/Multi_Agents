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
