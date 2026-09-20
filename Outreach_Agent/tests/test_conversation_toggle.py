"""The ON/OFF toggle, and what must and must not happen on either side of it.

The requirement this file exists to prove: OFF means the agent stops
completely, manual control still works, nothing is lost, and turning it back
ON resumes without duplicating anything.

"OFF does nothing" is asserted negatively and specifically -- no stage write,
no stored message, no send -- rather than by checking a summary counter,
because a counter can read zero while a side effect still happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.config import load_config
from outreach.conversation import classifier, engine, responder
from outreach.conversation.inbox import InboundEmail
from outreach.conversation.settings import get_settings, is_enabled, set_settings
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


def _seed(conn, *, stage="sent", sent_days_ago=0):
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme",
        contact_email="cto@acme.com", contact_name="Dana",
    ))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage=stage, to_email="cto@acme.com",
        subject="Hiring support", body="Original pitch.",
        provider_message_id="<orig@csg>",
        sent_at=NOW - timedelta(days=sent_days_ago),
        created_at=NOW - timedelta(days=sent_days_ago),
        stage_updated_at=NOW - timedelta(days=sent_days_ago),
    ))


def _reply(message_id="<reply-1@acme.com>"):
    return InboundEmail(
        message_id=message_id, in_reply_to="<orig@csg>", references=[],
        from_email="cto@acme.com", to_email="outreach@csg.com",
        subject="Re: Hiring support", body="Tell me more.", received_at=NOW,
    )


def _fixed(monkeypatch, intent="interested"):
    monkeypatch.setattr(classifier, "classify", lambda cfg, email_in, **kw: intent)
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: Hiring support", "Sure."))
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: Hiring support", "Bump."))


# ---------------------------------------------------------------------------
# default state
# ---------------------------------------------------------------------------

def test_disabled_by_default(conn):
    """An unconfigured campaign reads as off, without a settings row having
    to exist -- deploying the agent is a no-op for every existing campaign."""
    assert is_enabled(conn, "never-configured") is False
    assert get_settings(conn, "never-configured").enabled is False
    assert conn.tables.get("conversation_settings", []) == []


def test_auto_reply_off_by_default_even_when_enabled(conn):
    """Switching the agent ON does not, by itself, authorize sending."""
    set_settings(conn, "camp-1", enabled=True)
    assert get_settings(conn, "camp-1").auto_reply is False


# ---------------------------------------------------------------------------
# OFF: complete no-op
# ---------------------------------------------------------------------------

def test_off_does_absolutely_nothing(conn, cfg, monkeypatch):
    """With the agent off: no stage change, no stored message, no send --
    even with a reply sitting right there waiting to be processed."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False)
    _fixed(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert result.enabled is False
    assert db.get_item(conn, "item-1").stage == "sent"
    assert db.list_conversation_messages(conn, "item-1") == []
    assert db.list_events(conn, "item-1") == []
    assert result.fetched == 0


def test_off_never_sends_even_with_auto_reply_on(conn, cfg, monkeypatch):
    """auto_reply is subordinate to `enabled`: the master switch wins."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False, auto_reply=True)
    _fixed(monkeypatch)

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert sent == []


def test_off_skips_followups_entirely(conn, cfg, monkeypatch):
    """Long silence plus the agent off means nothing happens, not even the
    bookkeeping move to waiting."""
    _seed(conn, sent_days_ago=30)
    set_settings(conn, "camp-1", enabled=False, followup_after_days=3)
    _fixed(monkeypatch)

    engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)

    assert db.get_item(conn, "item-1").stage == "sent"


def test_tick_all_ignores_disabled_campaigns(conn, cfg, monkeypatch):
    """Disabled campaigns are never listed, let alone processed."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False)
    _fixed(monkeypatch)

    assert engine.tick_all(conn, cfg, emails=[_reply()], now=NOW) == []


# ---------------------------------------------------------------------------
# OFF: manual control still works
# ---------------------------------------------------------------------------

def test_manual_stage_moves_work_while_off(conn, cfg):
    """Dragging a card by hand is unaffected by the agent being off -- this
    is exactly how you drive the pipeline yourself."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False)

    db.set_stage(conn, "item-1", "waiting", actor="user")
    db.set_stage(conn, "item-1", "replied", actor="user")
    db.set_stage(conn, "item-1", "booked", actor="user")

    assert db.get_item(conn, "item-1").stage == "booked"
    actors = [e.actor for e in db.list_events(conn, "item-1")]
    assert actors == ["user", "user", "user"]


def test_turning_off_preserves_all_data(conn, cfg, monkeypatch):
    """Switching off is a single boolean write: history, stage and counters
    all survive untouched."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)
    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    messages_before = db.list_conversation_messages(conn, "item-1")
    events_before = db.list_events(conn, "item-1")
    stage_before = db.get_item(conn, "item-1").stage
    assert messages_before and events_before

    set_settings(conn, "camp-1", enabled=False)

    assert db.list_conversation_messages(conn, "item-1") == messages_before
    assert db.list_events(conn, "item-1") == events_before
    assert db.get_item(conn, "item-1").stage == stage_before


def test_toggling_off_preserves_followup_configuration(conn):
    """Flipping `enabled` alone leaves the other knobs exactly as set --
    the UI's toggle does not have to resend them."""
    set_settings(conn, "camp-1", enabled=True, auto_reply=True,
                 followup_after_days=7, max_followups=5)
    set_settings(conn, "camp-1", enabled=False)

    s = get_settings(conn, "camp-1")
    assert (s.enabled, s.auto_reply, s.followup_after_days, s.max_followups) == (False, True, 7, 5)


# ---------------------------------------------------------------------------
# re-enabling
# ---------------------------------------------------------------------------

def test_re_enabling_does_not_replay_history(conn, cfg, monkeypatch):
    """OFF then ON resumes from recorded state: the reply already processed
    is not re-processed, and the card does not move a second time."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)
    assert db.get_item(conn, "item-1").stage == "replied"

    set_settings(conn, "camp-1", enabled=False)
    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    set_settings(conn, "camp-1", enabled=True)
    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert result.duplicates == 1
    assert result.new_inbound == 0
    assert result.advanced == 0
    assert len(db.list_conversation_messages(conn, "item-1")) == 2  # 1 inbound + 1 draft


def test_manual_move_while_off_is_respected_after_re_enabling(conn, cfg, monkeypatch):
    """A card a person moved to booked while the agent was off is not pulled
    back when the agent resumes."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False)
    db.set_stage(conn, "item-1", "booked", actor="user")

    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch, intent="interested")
    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert db.get_item(conn, "item-1").stage == "booked"


# ---------------------------------------------------------------------------
# auto_reply: the second switch
# ---------------------------------------------------------------------------

def test_enabled_without_auto_reply_drafts_but_never_sends(conn, cfg, monkeypatch):
    """The default ON state: everything happens except the send."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=False)
    _fixed(monkeypatch)

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert sent == []
    assert result.replies_drafted == 1
    assert result.replies_sent == 0
    assert db.get_item(conn, "item-1").stage == "replied"

    drafts = db.list_pending_drafts(conn, "item-1")
    assert len(drafts) == 1
    assert drafts[0].draft_pending is True
    assert drafts[0].sent_at is None


def test_auto_reply_on_actually_sends(conn, cfg, monkeypatch):
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    _fixed(monkeypatch)

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert len(sent) == 1
    assert sent[0]["to_email"] == "cto@acme.com"
    assert result.replies_sent == 1
    assert db.list_pending_drafts(conn, "item-1") == []


def test_switching_off_mid_tick_stops_the_send(conn, cfg, monkeypatch):
    """`enabled` is re-read immediately before every send, so a tick that
    started before the switch was flipped does not go on to email people."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    monkeypatch.setattr(classifier, "classify", lambda cfg, email_in, **kw: "interested")

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    # Someone flips the switch off while the reply is being composed.
    def compose_then_disable(*a, **kw):
        set_settings(conn, "camp-1", enabled=False)
        return ("Re: Hiring support", "Sure.")

    monkeypatch.setattr(responder, "compose_reply", compose_then_disable)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert sent == []
    assert result.replies_sent == 0
    assert "switched off mid-tick" in (result.message or "")
