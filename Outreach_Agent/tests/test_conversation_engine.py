"""The Conversation Agent's core flow: sent -> waiting -> replied -> booked.

Every test here runs fully offline. Two seams make that possible without an
IMAP server or a live LLM:

  * `engine.tick(..., emails=[...])` takes a batch of InboundEmail directly,
    bypassing inbox.fetch_replies.
  * `engine.tick(..., now=...)` takes the clock, so follow-up timing that
    would take days of real silence is exercised in milliseconds.

The classifier and the LLM drafting calls are monkeypatched per test, so what
is under test is the engine's decision-making and its writes -- not a
model's judgment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.config import load_config
from outreach.conversation import classifier, engine, responder
from outreach.conversation.inbox import InboundEmail
from outreach.conversation.settings import set_settings
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


def _seed(conn, *, stage="sent", sent_days_ago=0, provider_message_id="<orig@csg>"):
    """One campaign with one item in a conversation stage, plus its prospect."""
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme", industry="SaaS",
        contact_email="cto@acme.com", contact_name="Dana",
    ))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage=stage, to_email="cto@acme.com",
        subject="Hiring support for Acme", body="Original pitch.",
        provider_message_id=provider_message_id,
        sent_at=NOW - timedelta(days=sent_days_ago),
        created_at=NOW - timedelta(days=sent_days_ago),
        stage_updated_at=NOW - timedelta(days=sent_days_ago),
    ))
    return db.get_item(conn, "item-1")


def _reply(body="Sounds interesting, tell me more.", message_id="<reply-1@acme.com>",
           in_reply_to="<orig@csg>", subject="Re: Hiring support for Acme"):
    return InboundEmail(
        message_id=message_id, in_reply_to=in_reply_to, references=[],
        from_email="cto@acme.com", to_email="outreach@csg.com",
        subject=subject, body=body, received_at=NOW,
    )


def _fixed_intent(monkeypatch, intent):
    monkeypatch.setattr(classifier, "classify", lambda cfg, email_in, **kw: intent)


def _fixed_draft(monkeypatch, subject="Re: Hiring support for Acme", body="Happy to walk you through it."):
    monkeypatch.setattr(
        responder, "compose_reply",
        lambda *a, **kw: (subject, body),
    )
    monkeypatch.setattr(
        responder, "compose_followup",
        lambda *a, **kw: (subject, body),
    )


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_reply_moves_sent_to_replied(conn, cfg, monkeypatch):
    """An interested reply on a `sent` item advances it to `replied`."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert result.new_inbound == 1
    assert result.advanced == 1
    assert db.get_item(conn, "item-1").stage == "replied"


def test_booked_reply_moves_straight_to_booked(conn, cfg, monkeypatch):
    """A reply agreeing to a meeting jumps the card to `booked` -- a legal
    transition directly from `sent` per CONVERSATION_TRANSITIONS."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "booked")
    _fixed_draft(monkeypatch)

    engine.tick(conn, cfg, "camp-1", emails=[_reply("Thursday 2pm works.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "booked"


def test_calendly_booking_notification_moves_card_to_booked(conn, cfg, monkeypatch):
    """End-to-end regression for a real live booking: a Calendly "New Event"
    notification is FROM notifications@calendly.com and TO the account
    owner -- neither header names the prospect -- with the invitee's
    address only in Reply-To. Runs the REAL classifier and matcher (not
    monkeypatched), since the whole point of this test is that matching
    strategy 4 and the deterministic booking-confirmation check actually
    connect to each other correctly, not just individually."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_draft(monkeypatch)

    notification = InboundEmail(
        message_id="<notif1@calendly.com>", in_reply_to=None, references=[],
        from_email="notifications@calendly.com", to_email="affan@careersourcegroup.com",
        subject="New Event: testing - 06:00pm Fri, 25 Sep 2026 - 30 Minute Meeting",
        body="", received_at=NOW, reply_to="cto@acme.com",
    )

    result = engine.tick(conn, cfg, "camp-1", emails=[notification], now=NOW)

    assert result.matched == 1
    assert result.classified.get("booked") == 1
    assert db.get_item(conn, "item-1").stage == "booked"


def test_full_flow_sent_waiting_replied_booked(conn, cfg, monkeypatch):
    """The whole funnel across four ticks, one stage at a time."""
    _seed(conn, sent_days_ago=5)
    set_settings(conn, "camp-1", enabled=True, followup_after_days=3, max_followups=0)
    _fixed_draft(monkeypatch)

    # 1. Silence past the threshold -> waiting.
    engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)
    assert db.get_item(conn, "item-1").stage == "waiting"

    # 2. They reply with interest -> replied.
    _fixed_intent(monkeypatch, "interested")
    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)
    assert db.get_item(conn, "item-1").stage == "replied"

    # 3. They confirm a time -> booked.
    _fixed_intent(monkeypatch, "booked")
    engine.tick(
        conn, cfg, "camp-1",
        emails=[_reply("Tuesday at 10 works.", message_id="<reply-2@acme.com>")],
        now=NOW,
    )
    assert db.get_item(conn, "item-1").stage == "booked"

    stages = [e.to_stage for e in db.list_events(conn, "item-1") if e.event_type == "stage_change"]
    assert stages == ["waiting", "replied", "booked"]


# ---------------------------------------------------------------------------
# intents that must NOT move a card
# ---------------------------------------------------------------------------

def test_out_of_office_does_not_move_the_card(conn, cfg, monkeypatch):
    """An autoresponder is not a reply: the card stays put and the prospect
    is not marked as having engaged, so follow-ups still apply."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "out_of_office")

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("I am on leave until the 5th.")], now=NOW)

    item = db.get_item(conn, "item-1")
    assert item.stage == "sent"
    assert item.last_inbound_at is None
    assert result.advanced == 0
    # Still recorded as history, just not acted on.
    assert len(db.list_conversation_messages(conn, "item-1")) == 1


def test_bounce_does_not_move_the_card(conn, cfg, monkeypatch):
    """A delivery failure is not a reply either."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "bounce")

    engine.tick(conn, cfg, "camp-1", emails=[_reply("Undeliverable")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "sent"


def test_not_interested_moves_to_replied_but_sends_nothing(conn, cfg, monkeypatch):
    """A rejection is a real reply, so the card moves -- but the agent never
    answers it. The respectful response to "no" is silence."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    _fixed_intent(monkeypatch, "not_interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("Not interested, remove me.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "replied"
    assert result.replies_sent == 0
    assert result.replies_drafted == 0


# ---------------------------------------------------------------------------
# forward-only transitions
# ---------------------------------------------------------------------------

def test_never_moves_a_card_backwards(conn, cfg, monkeypatch):
    """A booked card stays booked even if a later reply classifies as merely
    interested -- CONVERSATION_TRANSITIONS gives `booked` no destinations."""
    _seed(conn, stage="booked")
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(
        conn, cfg, "camp-1",
        emails=[_reply("One more question.", message_id="<reply-9@acme.com>")],
        now=NOW,
    )

    assert db.get_item(conn, "item-1").stage == "booked"
    assert result.advanced == 0


def test_replied_cannot_go_back_to_waiting(conn, cfg, monkeypatch):
    """Silence after a reply must not demote the card -- `should_move_to_waiting`
    only ever considers `sent` items."""
    _seed(conn, stage="replied", sent_days_ago=30)
    set_settings(conn, "camp-1", enabled=True, followup_after_days=3)

    engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)

    assert db.get_item(conn, "item-1").stage == "replied"


# ---------------------------------------------------------------------------
# stage ownership -- the Outreach Agent's items are untouchable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["pending", "needs_review", "failed"])
def test_never_touches_outreach_owned_items(conn, cfg, monkeypatch, stage):
    """Items the Outreach Agent still owns are not merely skipped -- they are
    never loaded, because list_conversation_items filters on stage in the
    query. This is the structural guarantee against the two agents racing."""
    _seed(conn, stage=stage)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert db.get_item(conn, "item-1").stage == stage
    assert result.matched == 0
    assert result.advanced == 0
