"""The follow-up sequence through the real engine: ledger, pre-send checks,
gating on sync health, the Conversation Agent toggle, and failure handling.

Email (SMTP), Calendly data, the LLM and the clock are all faked -- nothing
here can send a real email.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.channels import SendError
from outreach.config import load_config
from outreach.conversation import classifier, engine, responder
from outreach.conversation.inbox import InboundEmail
from outreach.conversation.settings import set_settings
from outreach.models import Campaign, ImportedProspect, OutreachItem

SENT_AT = datetime(2026, 3, 1, 9, 0, 0)


def at(days: float) -> datetime:
    return SENT_AT + timedelta(days=days)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def sent(monkeypatch):
    """Fake SMTP + fake LLM. Each composed follow-up differs, like a real one."""
    box: list[dict] = []

    def fake_send(*a, **kw):
        box.append(kw)
        return f"<fu{len(box)}@csg>"

    monkeypatch.setattr("outreach.conversation.responder.send_via_channel", fake_send)
    monkeypatch.setattr(
        responder, "compose_followup",
        lambda cfg_, item, prospect, *, followup_number, **kw: (
            "Re: Hiring support", f"Follow-up body number {followup_number}",
        ),
    )
    return box


def _seed(conn, *, stage="sent", followup_count=0):
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=SENT_AT))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme", contact_email="cto@acme.com", contact_name="Dana",
    ))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage=stage, to_email="cto@acme.com", subject="Hiring support", body="Pitch.",
        provider_message_id="<orig@csg>", followup_count=followup_count,
        sent_at=SENT_AT, created_at=SENT_AT, stage_updated_at=SENT_AT,
    ))


def _tick(conn, cfg, days, **kw):
    kw.setdefault("emails", [])
    return engine.tick(conn, cfg, "camp-1", now=at(days), **kw)


def _stage(conn):
    return db.get_item(conn, "item-1").stage


def _enable(conn, **kw):
    set_settings(conn, "camp-1", enabled=True, **kw)


# --- the happy path, day by day -------------------------------------------------

def test_full_sequence_waiting_7_14_then_terminal_at_21(conn, cfg, sent):
    _seed(conn)
    _enable(conn, auto_reply=True)

    _tick(conn, cfg, 1.9)
    assert _stage(conn) == "sent" and not sent

    r = _tick(conn, cfg, 2)
    assert r.moved_to_waiting == 1 and _stage(conn) == "waiting" and not sent

    _tick(conn, cfg, 6.9)
    assert not sent

    r = _tick(conn, cfg, 7)
    assert r.followups_sent == 1 and len(sent) == 1
    assert "number 1" in db.list_conversation_messages(conn, "item-1")[-1].body

    _tick(conn, cfg, 13.9)
    assert len(sent) == 1

    r = _tick(conn, cfg, 14)
    assert r.followups_sent == 1 and len(sent) == 2
    bodies = [m.body for m in db.list_conversation_messages(conn, "item-1")]
    assert bodies[0] != bodies[1]  # follow-up #2 differs from #1

    _tick(conn, cfg, 20.9)
    assert _stage(conn) == "waiting"

    r = _tick(conn, cfg, 21)
    assert r.moved_to_no_response == 1 and _stage(conn) == "not_interested"
    event = db.list_events(conn, "item-1")[-1]
    assert event.detail["reason"] == "no_response" and event.actor == "conversation_agent"

    # Terminal: nothing further is ever sent.
    _tick(conn, cfg, 40)
    _tick(conn, cfg, 90)
    assert len(sent) == 2


def test_followups_are_threaded_to_the_original_email(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    _tick(conn, cfg, 7)
    assert sent[0]["in_reply_to"] == "<orig@csg>"


# --- safety: never contact someone who replied / booked / bounced -------------

def _reply(body="Sounds good", days=3):
    return InboundEmail(
        message_id="<r1@acme.com>", in_reply_to="<orig@csg>", references=[],
        from_email="cto@acme.com", to_email="outreach@csg.com",
        subject="Re: Hiring support", body=body, received_at=at(days),
    )


def test_a_reply_in_the_same_cycle_prevents_the_send(conn, cfg, sent, monkeypatch):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setattr(classifier, "classify", lambda *a, **kw: "interested")
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: x", "Great."))

    _tick(conn, cfg, 8, emails=[_reply()])

    assert _stage(conn) == "replied"
    # Only the reply to the prospect itself goes out; no follow-up.
    assert [m["body"] for m in sent] == ["Great."]
    assert db.list_followup_attempts(conn, "item-1") == []


@pytest.mark.parametrize("intent", ["not_interested", "bounce"])
def test_opt_out_and_bounce_stop_the_sequence(conn, cfg, sent, monkeypatch, intent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setattr(classifier, "classify", lambda *a, **kw: intent)

    _tick(conn, cfg, 8, emails=[_reply("unsubscribe me" if intent == "not_interested" else "delivery failed")])
    _tick(conn, cfg, 15)
    _tick(conn, cfg, 25)

    assert sent == []


def test_a_calendly_booking_prevents_the_send(conn, cfg, sent):
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/E1", event_uuid="E1",
        invitee_email="cto@acme.com", status="active", item_id=None,
        created_at=SENT_AT, updated_at=SENT_AT,
    ))

    _tick(conn, cfg, 8)
    assert sent == []


def test_a_canceled_booking_still_blocks_chasing(conn, cfg, sent):
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/E1", event_uuid="E1",
        invitee_email="CTO@acme.com", status="canceled", item_id="item-1",
        created_at=SENT_AT, updated_at=SENT_AT,
    ))
    _tick(conn, cfg, 8)
    _tick(conn, cfg, 30)
    assert sent == []
    assert _stage(conn) == "waiting"  # and it is not terminally failed either


def test_a_booking_that_lands_during_composition_wins(conn, cfg, sent, monkeypatch):
    """The pre-send re-check reads Calendly again after the LLM call."""
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)

    def compose_then_book(cfg_, item, prospect, **kw):
        calendly_db.upsert_event(conn, CalendlyEvent(
            event_uri="https://api.calendly.com/scheduled_events/E9", event_uuid="E9",
            invitee_email="cto@acme.com", status="active", item_id="item-1",
            created_at=SENT_AT, updated_at=SENT_AT,
        ))
        return ("Re: x", "body")

    monkeypatch.setattr(responder, "compose_followup", compose_then_book)
    _tick(conn, cfg, 8)
    assert sent == []
    assert db.list_followup_attempts(conn, "item-1") == []


# --- the Conversation Agent toggle ---------------------------------------------

def test_agent_off_means_no_moves_and_no_sends(conn, cfg, sent):
    _seed(conn)
    set_settings(conn, "camp-1", enabled=False, auto_reply=True)
    for day in (3, 8, 15, 22, 40):
        r = _tick(conn, cfg, day)
        assert r.enabled is False
    assert sent == [] and _stage(conn) == "sent"
    assert db.list_followup_attempts(conn, "item-1") == []


def test_switching_off_then_on_resumes_from_recorded_state(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    _tick(conn, cfg, 8)
    assert len(sent) == 1

    set_settings(conn, "camp-1", enabled=False)
    _tick(conn, cfg, 15)
    assert len(sent) == 1

    set_settings(conn, "camp-1", enabled=True)
    _tick(conn, cfg, 16)
    assert len(sent) == 2  # resumed: #2 now, no replay of #1
    assert [a.followup_number for a in db.list_followup_attempts(conn, "item-1")] == [1, 2]


def test_agent_switched_off_mid_composition_sends_nothing(conn, cfg, sent, monkeypatch):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)

    def compose_then_disable(*a, **kw):
        set_settings(conn, "camp-1", enabled=False)
        return ("Re: x", "body")

    monkeypatch.setattr(responder, "compose_followup", compose_then_disable)
    _tick(conn, cfg, 8)
    assert sent == []
    # The claim was released, so it can be attempted when the agent is back on.
    assert db.list_followup_attempts(conn, "item-1") == []


# --- auto_reply OFF: draft once ------------------------------------------------

def test_auto_reply_off_writes_exactly_one_draft_per_number(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=False)
    for minutes in range(0, 600, 30):
        engine.tick(conn, cfg, "camp-1", emails=[], now=at(8) + timedelta(minutes=minutes))
    assert sent == []
    assert len(db.list_pending_drafts(conn, "item-1")) == 1
    assert [a.status for a in db.list_followup_attempts(conn, "item-1")] == ["drafted"]
    assert db.get_item(conn, "item-1").followup_count == 0


def test_no_terminal_move_when_followups_were_only_drafted(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=False)
    for day in (8, 15, 22, 30):
        _tick(conn, cfg, day)
    assert _stage(conn) == "waiting"


# --- idempotency ----------------------------------------------------------------------

def test_repeated_ticks_never_duplicate(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    for minutes in range(0, 300, 30):
        _tick(conn, cfg, 8 + minutes / 1440)
    assert len(sent) == 1


def test_a_preclaimed_attempt_is_never_resent(conn, cfg, sent):
    """Simulates another replica (or a crashed earlier run) owning the claim."""
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    assert db.claim_followup_attempt(conn, "item-1", 1) is not None

    _tick(conn, cfg, 8)
    _tick(conn, cfg, 9)
    assert sent == []  # "claimed" = unknown delivery state: needs a human, not a resend


def test_restart_does_not_reset_timers_or_resend(conn, cfg, sent):
    """Timers derive from the stored sent_at and the ledger lives in the DB;
    a fresh engine call on the same data (a 'restart') changes nothing."""
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    _tick(conn, cfg, 8)
    assert len(sent) == 1

    import importlib
    importlib.reload(engine)  # a new process's import state
    from outreach.conversation import engine as fresh_engine
    fresh_engine.tick(conn, cfg, "camp-1", emails=[], now=at(8.1))
    assert len(sent) == 1
    fresh_engine.tick(conn, cfg, "camp-1", emails=[], now=at(14))
    assert len(sent) == 2


def test_ledger_reconciles_a_crash_after_send(conn, cfg, sent):
    """Ledger says sent but the counter was never incremented: the next tick
    repairs the counter instead of sending #1 again."""
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    db.claim_followup_attempt(conn, "item-1", 1)
    db.update_followup_attempt(conn, "item-1", 1, status="sent")
    assert db.get_item(conn, "item-1").followup_count == 0

    _tick(conn, cfg, 8)
    assert db.get_item(conn, "item-1").followup_count == 1
    assert sent == []


# --- send failures -----------------------------------------------------------------------

def test_failed_send_retries_up_to_three_times_then_stops(conn, cfg, monkeypatch):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: x", "body"))
    attempts = []

    def failing_send(*a, **kw):
        attempts.append(1)
        raise SendError("smtp down")

    monkeypatch.setattr("outreach.conversation.responder.send_via_channel", failing_send)

    for i in range(6):
        r = _tick(conn, cfg, 8 + i * 0.1)
    assert len(attempts) == 3
    ledger = db.list_followup_attempts(conn, "item-1")[0]
    assert ledger.status == "failed" and ledger.attempts == 3 and "smtp down" in ledger.error
    assert db.get_item(conn, "item-1").followup_count == 0
    # Failed attempts leave no stack of duplicate drafts behind.
    assert db.list_pending_drafts(conn, "item-1") == []


def test_a_retry_that_succeeds_counts_once(conn, cfg, monkeypatch):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: x", "body"))
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise SendError("timeout")
        return "<ok@csg>"

    monkeypatch.setattr("outreach.conversation.responder.send_via_channel", flaky)
    _tick(conn, cfg, 8)
    _tick(conn, cfg, 8.1)
    _tick(conn, cfg, 8.2)
    assert len(calls) == 2
    assert db.get_item(conn, "item-1").followup_count == 1
    assert db.list_followup_attempts(conn, "item-1")[0].status == "sent"


def test_real_email_channel_errors_are_send_errors():
    """Regression: EmailSendError used to be a bare RuntimeError, so SMTP
    failures escaped every `except SendError` handler."""
    from outreach.channels.email import EmailSendError

    assert issubclass(EmailSendError, SendError)


# --- sync health gates the follow-up phase ---------------------------------------------------

def test_inbox_failure_skips_followups_entirely(conn, cfg, sent):
    _seed(conn)
    _enable(conn, auto_reply=True)
    r = _tick(conn, cfg, 8, emails=[], inbox_ok=False)
    assert sent == [] and _stage(conn) == "sent"
    assert "follow-ups skipped" in (r.message or "")


def test_imap_fetch_error_does_not_assume_no_reply(conn, cfg, sent, monkeypatch):
    from outreach.conversation import inbox

    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setattr(inbox, "fetch_replies", lambda **kw: (_ for _ in ()).throw(inbox.InboxError("down")))

    r = engine.tick(conn, cfg, "camp-1", now=at(8))
    assert sent == []
    assert "down" in r.message and "skipped" in r.message


def test_stale_calendly_data_skips_followups(conn, cfg, sent, monkeypatch):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")

    _tick(conn, cfg, 8)  # never synced
    assert sent == []

    db.update_automation_state(conn, "sync", last_calendly_success_at=at(8) - timedelta(hours=5))
    _tick(conn, cfg, 8)  # synced, but longer than 2x the interval ago
    assert sent == []

    db.update_automation_state(conn, "sync", last_calendly_success_at=at(8) - timedelta(minutes=10))
    _tick(conn, cfg, 8)
    assert len(sent) == 1


def test_inbox_reply_is_processed_even_when_calendly_is_stale(conn, cfg, sent, monkeypatch):
    """Replies must keep landing on the board when only the follow-up phase
    is gated."""
    _seed(conn, stage="waiting")
    _enable(conn)
    monkeypatch.setattr(classifier, "classify", lambda *a, **kw: "interested")
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: x", "Hi"))
    _tick(conn, cfg, 8, emails=[_reply()], calendly_ok=False)
    assert _stage(conn) == "replied"


# --- inbound id stability ----------------------------------------------------------------------

def test_inbound_message_ids_are_stable_across_processes(conn):
    """Regression: ids used the per-process hash(), so a restart produced a
    different id for the same email."""
    import hashlib

    _seed(conn, stage="waiting")
    item = db.get_item(conn, "item-1")
    msg = engine._store_inbound(conn, item, _reply(), at(3))
    digest = hashlib.sha1(b"<r1@acme.com>").hexdigest()[:16]
    assert msg.message_id == f"in-item-1-{digest}"


# --- drafts sent by hand keep the sequence going --------------------------------

def test_manually_sent_drafts_continue_the_sequence_to_the_terminal_move(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=False)

    sent_day = [8]

    def send_by_hand():
        for m in db.list_pending_drafts(conn, "item-1"):
            responder.send_pending_draft(conn, cfg, db.get_item(conn, "item-1"), m)
            db.update_conversation_message(conn, m.message_id, sent_at=at(sent_day[0]).isoformat())

    _tick(conn, cfg, 8)
    assert len(db.list_pending_drafts(conn, "item-1")) == 1
    send_by_hand()
    sent_day[0] = 15
    _tick(conn, cfg, 15)                       # reconciles #1, drafts #2
    assert db.get_item(conn, "item-1").followup_count == 1
    assert len(db.list_pending_drafts(conn, "item-1")) == 1
    send_by_hand()
    _tick(conn, cfg, 22)                       # reconciles #2, then day 21 applies
    assert _stage(conn) == "not_interested"
    assert [a.status for a in db.list_followup_attempts(conn, "item-1")] == ["sent", "sent"]


def test_agent_on_and_auto_reply_on_sends_followups_without_a_person(conn, cfg, sent):
    _seed(conn, stage="waiting")
    _enable(conn, auto_reply=True)
    _tick(conn, cfg, 8)
    assert len(sent) == 1 and db.list_pending_drafts(conn, "item-1") == []
