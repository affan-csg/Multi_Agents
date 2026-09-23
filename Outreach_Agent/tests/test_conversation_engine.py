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


def test_not_interested_moves_to_failed_stage_but_sends_nothing(conn, cfg, monkeypatch):
    """A rejection is a real reply, so the card moves -- to the terminal
    not_interested stage ("Failed" in the UI), not to replied -- but the
    agent never answers it. The respectful response to "no" is silence."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    _fixed_intent(monkeypatch, "not_interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("Not interested, remove me.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "not_interested"
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


# ---------------------------------------------------------------------------
# not_interested ("Failed") -- terminal stage, different rejection wordings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "Not interested, thanks.",
    "We're not looking for this right now.",
    "No thanks, please don't contact us again.",
    "We don't need your services.",
    "Please remove me from this list.",
    "Not a fit for us, sorry.",
    "Unsubscribe me please.",
    "We already have a vendor for this.",
])
def test_various_rejection_wordings_reach_failed_stage(conn, cfg, monkeypatch, body):
    """A range of real-world rejection phrasings, all classified by the
    (monkeypatched, here) classifier as not_interested, all land on the same
    terminal stage. The classifier's own wording judgment is exercised
    separately in test_conversation_classifier.py -- this test is about the
    engine's routing once that judgment is made."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "not_interested")

    engine.tick(conn, cfg, "camp-1", emails=[_reply(body)], now=NOW)

    assert db.get_item(conn, "item-1").stage == "not_interested"


@pytest.mark.parametrize("from_stage", ["sent", "waiting", "replied"])
def test_not_interested_reachable_from_every_open_stage(conn, cfg, monkeypatch, from_stage):
    """A rejection can arrive at any point in the open conversation -- right
    after the first send, after a follow-up nudge, or even after a prior
    positive-sounding reply cools off -- and always reaches the same terminal
    stage, per CONVERSATION_TRANSITIONS."""
    _seed(conn, stage=from_stage)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "not_interested")

    engine.tick(conn, cfg, "camp-1", emails=[_reply("Not interested.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "not_interested"


def test_not_interested_is_terminal_for_the_agent(conn, cfg, monkeypatch):
    """Once a card is Failed, the Conversation Agent's own automated moves
    can never take it anywhere else -- CONVERSATION_TRANSITIONS gives
    not_interested no destinations, same as booked."""
    _seed(conn, stage="not_interested")
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(
        conn, cfg, "camp-1",
        emails=[_reply("Actually, let's talk after all.", message_id="<reply-9@acme.com>")],
        now=NOW,
    )

    assert db.get_item(conn, "item-1").stage == "not_interested"
    assert result.advanced == 0


def test_not_interested_item_receives_no_automated_followup(conn, cfg, monkeypatch):
    """A Failed prospect must never get a follow-up nudge -- should_send_followup
    and should_move_to_waiting both gate on the item's exact stage, and
    not_interested is neither 'sent' nor 'waiting', so a Failed item is
    structurally excluded from the follow-up pass regardless of how much
    silence has passed."""
    _seed(conn, stage="not_interested", sent_days_ago=30)
    set_settings(conn, "camp-1", enabled=True, followup_after_days=1, max_followups=2)
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)

    assert db.get_item(conn, "item-1").stage == "not_interested"
    assert result.followups_sent == 0
    assert result.followups_drafted == 0
    assert result.moved_to_waiting == 0


def test_end_to_end_rejection_flow_sent_waiting_failed(conn, cfg, monkeypatch):
    """The rejection funnel across two ticks: silence moves the card to
    Waiting exactly like the happy path, then a rejection reply moves it to
    the terminal Failed stage instead of Replied/Booked."""
    _seed(conn, sent_days_ago=5)
    set_settings(conn, "camp-1", enabled=True, followup_after_days=3, max_followups=0)
    _fixed_draft(monkeypatch)

    # 1. Silence past the threshold -> waiting, same as the happy path.
    engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)
    assert db.get_item(conn, "item-1").stage == "waiting"

    # 2. They reply with a clear rejection -> Failed, not Replied.
    _fixed_intent(monkeypatch, "not_interested")
    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("Not interested, remove me.")], now=NOW)
    assert db.get_item(conn, "item-1").stage == "not_interested"
    assert result.replies_sent == 0
    assert result.replies_drafted == 0

    stages = [e.to_stage for e in db.list_events(conn, "item-1") if e.event_type == "stage_change"]
    assert stages == ["waiting", "not_interested"]


def test_not_interested_preserves_message_and_audit_trail(conn, cfg, monkeypatch):
    """The rejection email itself, its classification, and the stage-change
    event (with reason/intent/provider_message_id) are all preserved -- a
    Failed card is not a dead end for the record, only for further outreach."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed_intent(monkeypatch, "not_interested")

    engine.tick(
        conn, cfg, "camp-1",
        emails=[_reply("Not a fit for us, please remove me.", message_id="<reply-42@acme.com>")],
        now=NOW,
    )

    messages = db.list_conversation_messages(conn, "item-1")
    inbound = [m for m in messages if m.direction == "inbound"]
    assert len(inbound) == 1
    assert inbound[0].body == "Not a fit for us, please remove me."
    assert inbound[0].intent == "not_interested"
    assert inbound[0].provider_message_id == "<reply-42@acme.com>"
    assert inbound[0].received_at == NOW

    events = [e for e in db.list_events(conn, "item-1") if e.event_type == "stage_change"]
    failed_event = next(e for e in events if e.to_stage == "not_interested")
    assert failed_event.from_stage == "sent"
    assert failed_event.actor == "conversation_agent"
    assert failed_event.detail.get("intent") == "not_interested"
    assert failed_event.detail.get("provider_message_id") == "<reply-42@acme.com>"


def test_not_interested_respects_auto_reply_off(conn, cfg, monkeypatch):
    """Even with auto_reply on, a rejection never gets an automated reply --
    responder._REPLYABLE excludes not_interested outright, so this holds
    regardless of the auto_reply switch. Verified here with auto_reply off,
    the more common default configuration."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=False)
    _fixed_intent(monkeypatch, "not_interested")
    _fixed_draft(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("No thanks.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "not_interested"
    assert result.replies_sent == 0
    assert result.replies_drafted == 0
    # No draft was even composed for this intent -- should_reply("not_interested") is False.
    outbound = [m for m in db.list_conversation_messages(conn, "item-1") if m.direction == "outbound"]
    assert outbound == []


def test_disabled_agent_never_sets_not_interested(conn, cfg, monkeypatch):
    """The master switch is a complete no-op, including for rejections --
    same guarantee test_conversation_toggle.py exercises for every other
    stage move."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False)
    _fixed_intent(monkeypatch, "not_interested")

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply("Not interested.")], now=NOW)

    assert db.get_item(conn, "item-1").stage == "sent"
    assert result.enabled is False
