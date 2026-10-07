"""Follow-up timing and caps.

The failure this file guards against is the expensive one: nudging someone
who already answered, or nudging them more times than configured. Both are
the kind of bug a user only discovers from an angry reply, so the rules are
tested exhaustively rather than sampled.

`followup.py` is pure, so most of this needs no database at all -- just an
item, settings, and a clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.config import load_config
from outreach.conversation import classifier, engine, followup, responder
from outreach.conversation.settings import set_settings
from outreach.models import Campaign, ConversationSettings, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


def _item(**kw) -> OutreachItem:
    defaults = dict(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage="sent", to_email="cto@acme.com",
        sent_at=NOW - timedelta(days=5), created_at=NOW - timedelta(days=5),
    )
    defaults.update(kw)
    return OutreachItem(**defaults)


def _settings(**kw) -> ConversationSettings:
    defaults = dict(campaign_id="camp-1", enabled=True, followup_after_days=3, max_followups=2)
    defaults.update(kw)
    return ConversationSettings(**defaults)


# ---------------------------------------------------------------------------
# sent -> waiting
# ---------------------------------------------------------------------------

def test_moves_to_waiting_after_the_threshold():
    assert followup.should_move_to_waiting(_item(sent_at=NOW - timedelta(days=4)), _settings(), now=NOW) is True


def test_does_not_move_to_waiting_before_the_threshold():
    assert followup.should_move_to_waiting(_item(sent_at=NOW - timedelta(days=1)), _settings(), now=NOW) is False


def test_waiting_threshold_is_exact_elapsed_time_not_a_calendar_day():
    """should_move_to_waiting compares exact elapsed time since sent_at (see
    followup.days_since): with a 1-day setting, precisely 24 hours."""
    settings = _settings(followup_after_days=1)

    just_under_24h = _item(sent_at=NOW - timedelta(hours=23, minutes=59))
    exactly_24h = _item(sent_at=NOW - timedelta(hours=24))
    just_over_24h = _item(sent_at=NOW - timedelta(hours=24, minutes=1))

    assert followup.should_move_to_waiting(just_under_24h, settings, now=NOW) is False
    assert followup.should_move_to_waiting(exactly_24h, settings, now=NOW) is True
    assert followup.should_move_to_waiting(just_over_24h, settings, now=NOW) is True


def test_new_campaign_settings_default_to_the_day_2_7_14_21_schedule(conn):
    """What a campaign that has never been configured actually gets --
    get_settings' constructed default, not just the model's field default."""
    from outreach.conversation.settings import get_settings

    settings = get_settings(conn, "never-configured-campaign")
    assert settings.followup_after_days == 2
    assert settings.followup_days == [7, 14]
    assert settings.no_response_after_days == 21
    # Never sends on its own until someone turns it on.
    assert settings.auto_reply is False and settings.enabled is False


def test_never_moves_to_waiting_once_they_replied():
    """The whole point of Waiting is "no reply yet" -- a card with an inbound
    message on record must never be filed there."""
    item = _item(sent_at=NOW - timedelta(days=30), last_inbound_at=NOW - timedelta(days=1))
    assert followup.should_move_to_waiting(item, _settings(), now=NOW) is False


def test_only_sent_items_move_to_waiting():
    for stage in ("replied", "booked", "waiting", "pending", "not_interested"):
        assert followup.should_move_to_waiting(_item(stage=stage), _settings(), now=NOW) is False


# ---------------------------------------------------------------------------
# which follow-up is due (pure; full day-by-day coverage is in
# test_followup_schedule.py)
# ---------------------------------------------------------------------------

def _action(item, settings=None, **ctx):
    return followup.next_action(
        item, settings or _settings(), followup.FollowupContext(**ctx), now=NOW,
    )


def test_followup_due_for_a_silent_waiting_item():
    item = _item(stage="waiting", sent_at=NOW - timedelta(days=8), followup_count=0)
    action = _action(item)
    assert action.kind == "send_followup" and action.number == 1


def test_never_follows_up_someone_who_replied():
    """The most damaging possible follow-up bug."""
    item = _item(stage="waiting", sent_at=NOW - timedelta(days=30),
                 last_inbound_at=NOW - timedelta(days=1), followup_count=0)
    assert _action(item).kind == "none"


def test_respects_the_max_followups_cap():
    item = _item(stage="waiting", sent_at=NOW - timedelta(days=15), followup_count=2)
    action = _action(item, _settings(max_followups=2, no_response_after_days=60), followups_sent=2,
                     last_followup_sent_at=NOW - timedelta(days=4))
    assert action.kind != "send_followup"


def test_max_followups_zero_disables_nudges_entirely():
    item = _item(stage="waiting", sent_at=NOW - timedelta(days=30), followup_count=0)
    assert _action(item, _settings(max_followups=0)).kind == "none"


def test_only_waiting_items_get_a_followup_not_failed_ones():
    item = _item(stage="not_interested", sent_at=NOW - timedelta(days=30), followup_count=0)
    assert _action(item).kind == "none"


def test_consecutive_followups_are_spaced_out():
    item = _item(stage="waiting", sent_at=NOW - timedelta(days=30), followup_count=1)

    assert _action(item, followups_sent=1, last_followup_sent_at=NOW - timedelta(hours=6)).kind == "none"
    long_ago = _action(item, followups_sent=1, last_followup_sent_at=NOW - timedelta(days=7))
    assert long_ago.kind == "send_followup" and long_ago.number == 2


def test_no_followup_for_items_not_in_waiting():
    """A nudge is never the first thing that happens to a card -- sent items
    are moved to Waiting first."""
    for stage in ("replied", "booked"):
        assert _action(_item(stage=stage, sent_at=NOW - timedelta(days=30))).kind == "none"
    sent = _action(_item(stage="sent", sent_at=NOW - timedelta(days=30)))
    assert sent.kind == "move_waiting"


# ---------------------------------------------------------------------------
# end to end, through the engine
# ---------------------------------------------------------------------------

def _seed(conn, *, stage="sent", sent_days_ago=5, followup_count=0):
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme",
        contact_email="cto@acme.com", contact_name="Dana",
    ))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage=stage, to_email="cto@acme.com",
        subject="Hiring support", body="Original pitch.",
        provider_message_id="<orig@csg>", followup_count=followup_count,
        sent_at=NOW - timedelta(days=sent_days_ago),
        created_at=NOW - timedelta(days=sent_days_ago),
        stage_updated_at=NOW - timedelta(days=sent_days_ago),
    ))


def test_engine_moves_silent_item_to_waiting(conn, cfg, monkeypatch):
    _seed(conn, sent_days_ago=5)
    set_settings(conn, "camp-1", enabled=True, followup_after_days=3, max_followups=0)

    result = engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)

    assert result.moved_to_waiting == 1
    assert db.get_item(conn, "item-1").stage == "waiting"


def _capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or f"<sent{len(sent)}@csg>",
    )
    return sent


def test_engine_drafts_followup_once_without_sending_when_auto_reply_off(conn, cfg, monkeypatch):
    """Auto Reply OFF (the default): one draft per number, never a send."""
    _seed(conn, stage="waiting", sent_days_ago=8)
    set_settings(conn, "camp-1", enabled=True, auto_reply=False)
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: Hiring support", "Quick thought."))
    sent = _capture_sends(monkeypatch)

    first = engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)
    second = engine.tick(conn, cfg, "camp-1", emails=[], now=NOW + timedelta(minutes=30))

    assert sent == []
    assert first.followups_drafted == 1 and second.followups_drafted == 0
    assert len(db.list_pending_drafts(conn, "item-1")) == 1
    assert db.get_item(conn, "item-1").followup_count == 0


def test_engine_sends_followup_and_counts_it(conn, cfg, monkeypatch):
    _seed(conn, stage="waiting", sent_days_ago=8)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: Hiring support", "Quick thought."))
    sent = _capture_sends(monkeypatch)

    result = engine.tick(conn, cfg, "camp-1", emails=[], now=NOW)

    assert len(sent) == 1
    assert result.followups_sent == 1
    assert db.get_item(conn, "item-1").followup_count == 1
    # A follow-up answers nothing yet -- it threads against the ORIGINAL
    # outreach send instead, so it still lands in the same conversation.
    assert sent[0]["in_reply_to"] == "<orig@csg>"


def test_repeated_ticks_do_not_exceed_the_cap(conn, cfg, monkeypatch):
    """Ticking again and again still respects the cap -- the counter and the
    ledger are persisted, not per-tick."""
    _seed(conn, stage="waiting", sent_days_ago=60)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: Hiring support", "Quick thought."))
    sent = _capture_sends(monkeypatch)

    for day in range(0, 100, 10):
        engine.tick(conn, cfg, "camp-1", emails=[], now=NOW + timedelta(days=day))

    assert len(sent) == 2
    assert db.get_item(conn, "item-1").followup_count == 2


def test_reply_stops_further_followups(conn, cfg, monkeypatch):
    """Once they answer, the nudges stop -- even with allowance remaining."""
    _seed(conn, stage="waiting", sent_days_ago=30)
    set_settings(conn, "camp-1", enabled=True, auto_reply=True)
    monkeypatch.setattr(classifier, "classify", lambda cfg_, e, **kw: "interested")
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: x", "Sure."))
    monkeypatch.setattr(responder, "compose_followup", lambda *a, **kw: ("Re: x", "Bump."))
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel", lambda *a, **kw: "<sent@csg>",
    )

    from outreach.conversation.inbox import InboundEmail
    engine.tick(conn, cfg, "camp-1", emails=[InboundEmail(
        message_id="<r1@acme.com>", in_reply_to="<orig@csg>", references=[],
        from_email="cto@acme.com", to_email="outreach@csg.com",
        subject="Re: Hiring support", body="Interested.", received_at=NOW,
    )], now=NOW)

    assert db.get_item(conn, "item-1").stage == "replied"

    before = db.get_item(conn, "item-1").followup_count
    for day in range(10, 60, 10):
        engine.tick(conn, cfg, "camp-1", emails=[], now=NOW + timedelta(days=day))

    assert db.get_item(conn, "item-1").followup_count == before
