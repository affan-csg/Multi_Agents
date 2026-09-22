"""Tests for the draft_versions snapshot hooks added to api.py's
revise_item/redraft_item/redraft_generic_items and conversation/routes.py's
revise_draft/redraft_draft. Confirms each writes a correctly-shaped
draft_versions row, and that repeated calls are NOT deduplicated (each edit
is a legitimate distinct event)."""

from __future__ import annotations

from outreach import api, db
from outreach.conversation import responder, routes as conversation_routes
from outreach.conversation.settings import set_settings
from outreach.learning import db as learning_db
from outreach.models import Campaign, ConversationMessage, ImportedProspect, OutreachItem
from outreach.personalize import DraftedMessage


def _seed_prospect(conn, domain="acme.com"):
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, research_run_id="run1", tier="A", score=80,
        contact_email=f"contact@{domain}", contact_name="Jordan", name="Acme Corp",
        pain_summary="hiring urgently",
    ))


def _seed_item(conn, domain="acme.com", *, stage="pending", subject="Old subject", body="Old body"):
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="run1"))
    item = OutreachItem(
        item_id=f"item-{domain}", campaign_id="camp1", research_domain=domain,
        stage=stage, subject=subject, body=body, personalized_from="pain_summary",
    )
    db.create_item(conn, item)
    return item.item_id


def test_revise_item_writes_draft_version(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "revise_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New subject", body="New body", personalized_from="revision"),
    )
    _seed_prospect(conn)
    item_id = _seed_item(conn)

    class Req:
        instruction = "make it shorter"

    api.revise_item(item_id, Req())

    versions = learning_db.list_draft_versions(conn, item_id=item_id)
    assert len(versions) == 1
    v = versions[0]
    assert v.source == "revise"
    assert v.before_subject == "Old subject"
    assert v.after_subject == "New subject"
    assert v.instruction == "make it shorter"
    assert v.campaign_id == "camp1"


def test_revise_item_twice_creates_two_distinct_versions(conn, monkeypatch):
    """Idempotency check: two genuine edits must produce two rows, not be
    deduplicated -- each /revise call is a legitimate distinct edit event."""
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "revise_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New subject", body="New body", personalized_from="revision"),
    )
    _seed_prospect(conn)
    item_id = _seed_item(conn)

    class Req:
        instruction = "make it shorter"

    api.revise_item(item_id, Req())
    api.revise_item(item_id, Req())

    versions = learning_db.list_draft_versions(conn, item_id=item_id)
    assert len(versions) == 2
    assert versions[0].version_id != versions[1].version_id


def test_redraft_item_writes_draft_version(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New subject", body="New body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="needs_review")

    api.redraft_item(item_id)

    versions = learning_db.list_draft_versions(conn, item_id=item_id, source="redraft")
    assert len(versions) == 1
    assert versions[0].instruction is None


def test_redraft_generic_items_writes_one_version_per_item(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New subject", body="New body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "one.com")
    _seed_prospect(conn, "two.com")
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="run1"))
    for domain in ("one.com", "two.com"):
        db.create_item(conn, OutreachItem(
            item_id=f"item-{domain}", campaign_id="camp1", research_domain=domain,
            stage="pending", personalized_from="template_only",
        ))

    api.redraft_generic_items("camp1")

    versions = learning_db.list_draft_versions(conn, source="redraft_generic")
    assert len(versions) == 2


def _seed_conversation_message(conn, item_id, campaign_id="camp1"):
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "inbound-1", "item_id": item_id, "direction": "inbound",
        "provider_message_id": "msg-1", "subject": "Re: Hi", "body": "Sounds interesting, tell me more",
        "intent": "interested", "created_at": "2026-01-02T00:00:00", "draft_pending": False,
    })
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "draft-1", "item_id": item_id, "direction": "outbound",
        "in_reply_to": "msg-1", "subject": "Re: Hi", "body": "Here is some info about CSG.",
        "draft_pending": True, "created_at": "2026-01-02T01:00:00",
    })


def test_conversation_revise_draft_writes_draft_version(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "revise_reply",
        lambda cfg, item, prospect, **kwargs: ("Re: Hi", "Shorter info about CSG."),
    )
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    class Req:
        instruction = "make it shorter"

    conversation_routes.revise_draft("draft-1", Req())

    versions = learning_db.list_draft_versions(conn, source="conversation_revise")
    assert len(versions) == 1
    assert versions[0].message_id == "draft-1"
    assert versions[0].before_body == "Here is some info about CSG."
    assert versions[0].after_body == "Shorter info about CSG."


def test_conversation_redraft_draft_writes_draft_version(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "compose_reply",
        lambda cfg, item, prospect, thread, latest, intent, **kwargs: ("Re: Hi", "Fresh reply about CSG."),
    )
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    conversation_routes.redraft_draft("draft-1")

    versions = learning_db.list_draft_versions(conn, source="conversation_redraft")
    assert len(versions) == 1
    assert versions[0].message_id == "draft-1"
    assert versions[0].after_body == "Fresh reply about CSG."
