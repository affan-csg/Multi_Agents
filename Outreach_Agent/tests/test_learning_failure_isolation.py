"""The critical rl_prompt.md section 12 test: if the Feedback Learning
Agent fails (context fetch raises, or the draft_versions write raises),
drafting/revising/redrafting/conversation replying must still succeed with
the correct subject/body, and outreach_items must still update correctly --
no draft_versions row is expected to exist afterward (the write failed
silently, as designed)."""

from __future__ import annotations

from outreach import api, db
from outreach.conversation import responder, routes as conversation_routes
from outreach.conversation.settings import set_settings
from outreach.learning import db as learning_db
from outreach.models import Campaign, ImportedProspect, OutreachItem
from outreach.personalize import DraftedMessage


def _seed_prospect(conn, domain="acme.com"):
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, research_run_id="run1", tier="A", score=80,
        contact_email=f"contact@{domain}", pain_summary="hiring urgently",
    ))


def _seed_item(conn, domain="acme.com", *, stage="pending", subject="Old subject", body="Old body"):
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="run1"))
    item = OutreachItem(
        item_id=f"item-{domain}", campaign_id="camp1", research_domain=domain,
        stage=stage, subject=subject, body=body, personalized_from="pain_summary",
    )
    db.create_item(conn, item)
    return item.item_id


def _seed_conversation_message(conn, item_id):
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "inbound-1", "item_id": item_id, "direction": "inbound",
        "provider_message_id": "msg-1", "subject": "Re: Hi", "body": "Sounds interesting",
        "intent": "interested", "created_at": "2026-01-02T00:00:00", "draft_pending": False,
    })
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "draft-1", "item_id": item_id, "direction": "outbound",
        "in_reply_to": "msg-1", "subject": "Re: Hi", "body": "Draft reply.",
        "draft_pending": True, "created_at": "2026-01-02T01:00:00",
    })


def _break_context_fetch(monkeypatch):
    def _raise(conn, **kwargs):
        raise RuntimeError("learning context fetch broke")
    monkeypatch.setattr("outreach.learning.context.get_learning_context_summary", _raise)


def _break_draft_version_write(monkeypatch):
    def _raise(conn, **kwargs):
        raise RuntimeError("learning write broke")
    monkeypatch.setattr("outreach.learning.db.record_draft_edit", _raise)


def test_revise_item_survives_context_fetch_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "revise_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="revision"),
    )
    _break_context_fetch(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn)

    class Req:
        instruction = "shorten"

    result = api.revise_item(item_id, Req())

    assert result["subject"] == "New"
    assert db.get_item(conn, item_id).subject == "New"


def test_revise_item_survives_draft_version_write_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "revise_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="revision"),
    )
    _break_draft_version_write(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn)

    class Req:
        instruction = "shorten"

    result = api.revise_item(item_id, Req())

    assert result["subject"] == "New"
    assert db.get_item(conn, item_id).subject == "New"
    assert learning_db.list_draft_versions(conn, item_id=item_id) == []


def test_redraft_item_survives_context_fetch_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="pain_summary"),
    )
    _break_context_fetch(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["subject"] == "New"


def test_redraft_item_survives_draft_version_write_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="pain_summary"),
    )
    _break_draft_version_write(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["subject"] == "New"
    assert db.get_item(conn, item_id).subject == "New"
    assert learning_db.list_draft_versions(conn, item_id=item_id) == []


def test_redraft_generic_items_survives_per_item_write_failure(conn, monkeypatch):
    """One item's learning-write failure must not stop the rest of the
    batch from being redrafted."""
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="pain_summary"),
    )
    _break_draft_version_write(monkeypatch)
    _seed_prospect(conn, "one.com")
    _seed_prospect(conn, "two.com")
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="run1"))
    for domain in ("one.com", "two.com"):
        db.create_item(conn, OutreachItem(
            item_id=f"item-{domain}", campaign_id="camp1", research_domain=domain,
            stage="pending", personalized_from="template_only",
        ))

    result = api.redraft_generic_items("camp1")

    assert result["upgraded"] == 2


def test_conversation_revise_draft_survives_context_fetch_failure(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "revise_reply",
        lambda cfg, item, prospect, **kwargs: ("Re: Hi", "Shorter reply."),
    )
    _break_context_fetch(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    class Req:
        instruction = "shorten"

    result = conversation_routes.revise_draft("draft-1", Req())

    assert result["body"] == "Shorter reply."


def test_conversation_revise_draft_survives_draft_version_write_failure(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "revise_reply",
        lambda cfg, item, prospect, **kwargs: ("Re: Hi", "Shorter reply."),
    )
    _break_draft_version_write(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    class Req:
        instruction = "shorten"

    result = conversation_routes.revise_draft("draft-1", Req())

    assert result["body"] == "Shorter reply."
    assert learning_db.list_draft_versions(conn, source="conversation_revise") == []


def test_draft_message_directly_survives_learning_context_none():
    """personalize.draft_message called directly with learning_context=None
    (the default, and what api.py falls back to on a context-fetch failure)
    must still produce a normal draft."""
    from outreach.personalize import draft_message
    from outreach.config import load_config

    prospect = ImportedProspect(
        research_domain="acme.com", pain_summary="hiring urgently", contact_name="Jordan",
    )
    import outreach.personalize as personalize_module
    result_holder = {}

    def fake_call_llm(prompt):
        result_holder["prompt"] = prompt
        return "SUBJECT: Hi\n\nBODY: Some body text"

    orig = personalize_module._call_llm
    personalize_module._call_llm = fake_call_llm
    try:
        cfg = load_config()
        draft = draft_message(cfg, prospect, learning_context=None)
        assert draft.subject == "Hi"
        assert "learning_context" not in result_holder.get("prompt", "")
    finally:
        personalize_module._call_llm = orig
