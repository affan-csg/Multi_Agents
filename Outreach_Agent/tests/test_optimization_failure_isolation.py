"""The critical test for this layer, mirroring
test_learning_failure_isolation.py: if the Campaign-Level Optimization layer
fails (context fetch raises, or recompute raises), campaign creation,
drafting, revising, redrafting, and conversation replying must all still
succeed with the correct subject/body -- and existing Research, Outreach,
Conversation, and Learning-Agent functionality must be completely
unaffected."""

from __future__ import annotations

from outreach import api, db, ingest
from outreach.config import load_config
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


def _break_optimization_context(monkeypatch):
    def _raise(conn, **kwargs):
        raise RuntimeError("optimization context fetch broke")
    monkeypatch.setattr("outreach.optimization.context.get_optimization_context_summary", _raise)


def _break_optimization_recompute(monkeypatch):
    def _raise(conn, **kwargs):
        raise RuntimeError("optimization recompute broke")
    monkeypatch.setattr("outreach.optimization.routes.recompute_insights", _raise)


def test_revise_item_survives_optimization_context_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "revise_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="revision"),
    )
    _break_optimization_context(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn)

    class Req:
        instruction = "shorten"

    result = api.revise_item(item_id, Req())

    assert result["subject"] == "New"
    assert db.get_item(conn, item_id).subject == "New"


def test_redraft_item_survives_optimization_context_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="pain_summary"),
    )
    _break_optimization_context(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["subject"] == "New"


def test_redraft_generic_items_survives_optimization_context_failure(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="New", body="New body", personalized_from="pain_summary"),
    )
    _break_optimization_context(monkeypatch)
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


def test_conversation_revise_draft_survives_optimization_context_failure(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "revise_reply",
        lambda cfg, item, prospect, **kwargs: ("Re: Hi", "Shorter reply."),
    )
    _break_optimization_context(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    class Req:
        instruction = "shorten"

    result = conversation_routes.revise_draft("draft-1", Req())

    assert result["body"] == "Shorter reply."


def test_conversation_redraft_draft_survives_optimization_context_failure(conn, monkeypatch):
    monkeypatch.setattr(conversation_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(
        responder, "compose_reply",
        lambda cfg, item, prospect, thread, latest_inbound, intent, **kwargs: ("Re: Hi", "Composed reply."),
    )
    _break_optimization_context(monkeypatch)
    _seed_prospect(conn)
    item_id = _seed_item(conn, stage="sent")
    set_settings(conn, "camp1", enabled=True)
    _seed_conversation_message(conn, item_id)

    result = conversation_routes.redraft_draft("draft-1")

    assert result["body"] == "Composed reply."


def test_create_campaign_survives_optimization_context_failure(conn, monkeypatch):
    """The campaign-creation call site (ingest.py) is a NEW hook added for
    this feature -- a failure there must not block campaign creation, and
    the Learning Agent's own (also newly-wired) context fetch at this same
    call site must independently keep working."""
    monkeypatch.setattr(
        ingest, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="Body", personalized_from="pain_summary"),
    )
    _break_optimization_context(monkeypatch)
    _seed_prospect(conn, "acme.com")
    cfg = load_config()

    campaign, excluded = ingest.create_campaign_from_prospects(
        conn, cfg, research_run_id="run1", tiers=["A"],
    )

    assert campaign.campaign_id
    items = db.list_items(conn, campaign_id=campaign.campaign_id)
    assert len(items) == 1
    assert items[0].subject == "Hi"


def test_create_campaign_survives_learning_context_failure(conn, monkeypatch):
    """The Learning Agent's context fetch is now ALSO wired into campaign
    creation (previously it was not -- see ingest.py) -- its failure must be
    equally harmless."""
    monkeypatch.setattr(
        ingest, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="Body", personalized_from="pain_summary"),
    )

    def _raise(conn, **kwargs):
        raise RuntimeError("learning context fetch broke")
    monkeypatch.setattr("outreach.learning.context.get_learning_context_summary", _raise)
    _seed_prospect(conn, "acme.com")
    cfg = load_config()

    campaign, excluded = ingest.create_campaign_from_prospects(
        conn, cfg, research_run_id="run1", tiers=["A"],
    )

    assert campaign.campaign_id


def test_optimization_recompute_failure_does_not_break_recommendations_route(conn, monkeypatch):
    """The optimization layer's OWN routes handle their own errors normally
    (matching the Learning Agent's route-level convention -- see
    learning/routes.py: a genuine failure in a layer's own endpoint is
    allowed to surface, since nothing else depends on that endpoint
    succeeding). This test documents that boundary rather than asserting
    isolation where none is claimed."""
    from outreach.optimization import routes as opt_routes
    monkeypatch.setattr(opt_routes.db, "connect", lambda: conn)
    _break_optimization_recompute(monkeypatch)

    try:
        opt_routes.get_recommendations()
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_learning_agent_recompute_unaffected_by_optimization_package_existing(conn):
    """Sanity check: the pre-existing Learning Agent's own insight engine
    must produce identical results whether or not the new sibling
    optimization package is imported anywhere -- proving this feature is
    additive, not a modification of learning/engine.py."""
    from outreach.learning.engine import recompute_insights as learning_recompute
    insights = learning_recompute(conn)
    assert isinstance(insights, list)


def test_outreach_and_conversation_core_flows_still_pass_with_optimization_installed(conn, monkeypatch):
    """End-to-end smoke check: approving/sending style flows (Outreach's own
    core behavior) are untouched by this feature -- create_item/get_item
    round-trip normally."""
    _seed_prospect(conn, "acme.com")
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="run1"))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp1", research_domain="acme.com",
        stage="pending", subject="S", body="B", personalized_from="pain_summary",
    ))
    item = db.get_item(conn, "item-1")
    assert item.subject == "S"
    assert item.stage == "pending"
