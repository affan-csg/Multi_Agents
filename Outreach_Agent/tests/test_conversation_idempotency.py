"""Duplicate handling, locking, and coordination with the Outreach Agent.

The agent is designed to be run as often as you like -- the manual "Check
replies" button invites exactly that. These tests hold it to that promise:
re-reading the same mailbox must have no cumulative effect on anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db, pipeline
from outreach.config import load_config
from outreach.conversation import classifier, engine, responder
from outreach.conversation.inbox import InboundEmail
from outreach.conversation.settings import set_settings
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


def _seed(conn, *, stage="sent", item_id="item-1", domain="acme.com", sent_days_ago=0):
    if not db.get_campaign(conn, "camp-1"):
        db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, name=domain.split(".")[0].title(),
        contact_email=f"cto@{domain}", contact_name="Dana",
    ))
    db.create_item(conn, OutreachItem(
        item_id=item_id, campaign_id="camp-1", research_domain=domain,
        stage=stage, to_email=f"cto@{domain}",
        subject="Hiring support", body="Original pitch.",
        provider_message_id=f"<orig-{item_id}@csg>",
        sent_at=NOW - timedelta(days=sent_days_ago),
        created_at=NOW - timedelta(days=sent_days_ago),
        stage_updated_at=NOW - timedelta(days=sent_days_ago),
    ))


def _reply(message_id="<reply-1@acme.com>", domain="acme.com", item_id="item-1"):
    return InboundEmail(
        message_id=message_id, in_reply_to=f"<orig-{item_id}@csg>", references=[],
        from_email=f"cto@{domain}", to_email="outreach@csg.com",
        subject="Re: Hiring support", body="Tell me more.", received_at=NOW,
    )


def _fixed(monkeypatch, intent="interested"):
    monkeypatch.setattr(classifier, "classify", lambda cfg, email_in, **kw: intent)
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: Hiring support", "Sure."))
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: Hiring support", "Bump."))


# ---------------------------------------------------------------------------
# duplicate replies
# ---------------------------------------------------------------------------

def test_same_reply_processed_once_across_ticks(conn, cfg, monkeypatch):
    """The same email on five consecutive ticks: one message, one
    classification, one stage move."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    first = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)
    assert first.new_inbound == 1 and first.advanced == 1

    for _ in range(4):
        again = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)
        assert again.new_inbound == 0
        assert again.duplicates == 1
        assert again.advanced == 0

    inbound = [m for m in db.list_conversation_messages(conn, "item-1") if m.direction == "inbound"]
    assert len(inbound) == 1

    moves = [e for e in db.list_events(conn, "item-1") if e.event_type == "stage_change"]
    assert len(moves) == 1


def test_duplicate_reply_never_sends_twice(conn, cfg, monkeypatch):
    """The consequence that actually matters: re-reading a reply must not
    email the prospect again."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    _fixed(monkeypatch)

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    for _ in range(3):
        engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert len(sent) == 1


def test_same_email_in_one_batch_processed_once(conn, cfg, monkeypatch):
    """Deduplication is per message, not per tick -- a batch containing the
    same email twice still yields one."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply(), _reply()], now=NOW)

    assert result.new_inbound == 1
    assert result.duplicates == 1


def test_distinct_replies_are_both_processed(conn, cfg, monkeypatch):
    """Dedupe must not over-reach: two genuinely different replies from the
    same person are two messages."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    engine.tick(conn, cfg, "camp-1", emails=[_reply(message_id="<a@acme.com>")], now=NOW)
    engine.tick(conn, cfg, "camp-1", emails=[_reply(message_id="<b@acme.com>")], now=NOW)

    inbound = [m for m in db.list_conversation_messages(conn, "item-1") if m.direction == "inbound"]
    assert len(inbound) == 2


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------

def test_lock_is_exclusive(conn):
    """Two concurrent claims on one item: exactly one wins."""
    _seed(conn)
    stale_before = NOW - timedelta(minutes=10)

    assert db.claim_conversation_lock(conn, "item-1", stale_before=stale_before) is True
    assert db.claim_conversation_lock(conn, "item-1", stale_before=stale_before) is False


def test_lock_released_after_use(conn):
    _seed(conn)
    stale_before = NOW - timedelta(minutes=10)

    db.claim_conversation_lock(conn, "item-1", stale_before=stale_before)
    db.release_conversation_lock(conn, "item-1")

    assert db.claim_conversation_lock(conn, "item-1", stale_before=stale_before) is True


def test_stale_lock_is_reclaimable(conn):
    """A tick that crashed holding a lock must not strand the item forever."""
    _seed(conn)
    db.update_item_fields(
        conn, "item-1",
        conversation_locked_at=(NOW - timedelta(hours=2)).isoformat(),
    )

    assert db.claim_conversation_lock(conn, "item-1", stale_before=NOW - timedelta(minutes=10)) is True


def test_lock_is_released_even_when_processing_raises(conn, cfg, monkeypatch):
    """An exception mid-item must not leave the lock held -- otherwise one
    bad reply would freeze that card permanently."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)

    def boom(*a, **kw):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(classifier, "classify", boom)

    result = engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    assert "acme.com" in result.errors
    assert db.get_item(conn, "item-1").conversation_locked_at is None


def test_one_item_failing_does_not_stop_the_others(conn, cfg, monkeypatch):
    """A batch is processed per item; one failure is recorded and the rest
    continue."""
    _seed(conn, item_id="item-1", domain="acme.com")
    _seed(conn, item_id="item-2", domain="beta.com")
    set_settings(conn, "camp-1", enabled=True)
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: x", "Sure."))
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: x", "Bump."))

    def selective(cfg_, email_in, **kw):
        if "acme" in email_in.from_email:
            raise RuntimeError("bad reply")
        return "interested"

    monkeypatch.setattr(classifier, "classify", selective)

    engine.tick(conn, cfg, "camp-1", emails=[
        _reply(message_id="<a@acme.com>", domain="acme.com", item_id="item-1"),
        _reply(message_id="<b@beta.com>", domain="beta.com", item_id="item-2"),
    ], now=NOW)

    assert db.get_item(conn, "item-1").stage == "sent"      # failed, untouched
    assert db.get_item(conn, "item-2").stage == "replied"   # succeeded


# ---------------------------------------------------------------------------
# coordination with the Outreach Agent
# ---------------------------------------------------------------------------

def test_outreach_send_and_conversation_tick_do_not_interfere(conn, cfg, monkeypatch):
    """The two agents on one campaign, operating on disjoint stage sets: the
    Outreach Agent sends the pending item, the Conversation Agent advances
    the already-sent one, and neither disturbs the other's card."""
    _seed(conn, item_id="item-sent", domain="acme.com", stage="sent")
    _seed(conn, item_id="item-pending", domain="beta.com", stage="pending")
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    monkeypatch.setattr(pipeline, "send_via_channel", lambda *a, **kw: "<sent@csg>")

    engine.tick(conn, cfg, "camp-1", emails=[
        _reply(message_id="<a@acme.com>", domain="acme.com", item_id="item-sent"),
    ], now=NOW)
    pipeline.send_campaign(conn, cfg, "camp-1")

    assert db.get_item(conn, "item-sent").stage == "replied"   # conversation agent's
    assert db.get_item(conn, "item-pending").stage == "sent"   # outreach agent's

    # Each card was moved by exactly one actor.
    assert [e.actor for e in db.list_events(conn, "item-sent")] == ["conversation_agent"]
    assert [e.actor for e in db.list_events(conn, "item-pending")] == ["system"]


def test_conversation_agent_credits_itself_in_the_audit_trail(conn, cfg, monkeypatch):
    """Every move is attributable: 'conversation_agent' is distinct from
    'system' (Outreach) and 'user' (a person)."""
    _seed(conn)
    set_settings(conn, "camp-1", enabled=True)
    _fixed(monkeypatch)

    engine.tick(conn, cfg, "camp-1", emails=[_reply()], now=NOW)

    event = db.list_events(conn, "item-1")[0]
    assert event.actor == "conversation_agent"
    assert event.from_stage == "sent"
    assert event.to_stage == "replied"
    assert event.detail["intent"] == "interested"
