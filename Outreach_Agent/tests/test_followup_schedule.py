"""The day 0 / 2 / 7 / 14 / 21 schedule, as pure policy.

followup.next_action reads state and returns what is due; it sends nothing.
Every boundary is exercised with an injected clock -- no mailbox, no database.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach.conversation import followup
from outreach.models import ConversationSettings, OutreachItem

SENT_AT = datetime(2026, 3, 1, 9, 0, 0)


def at(days: float) -> datetime:
    return SENT_AT + timedelta(days=days)


def _settings(**kw) -> ConversationSettings:
    return ConversationSettings(campaign_id="c", enabled=True, **kw)


def _item(stage="sent", **kw) -> OutreachItem:
    base = dict(
        item_id="i1", campaign_id="c", research_domain="acme.com", stage=stage,
        to_email="a@acme.com", sent_at=SENT_AT, created_at=SENT_AT,
    )
    base.update(kw)
    return OutreachItem(**base)


def _act(item, days, settings=None, **ctx):
    return followup.next_action(
        item, settings or _settings(), followup.FollowupContext(**ctx), now=at(days),
    )


# --- defaults ---------------------------------------------------------------

def test_defaults_are_the_agreed_schedule_and_nothing_sends_by_default():
    s = _settings()
    assert (s.followup_after_days, s.followup_days, s.no_response_after_days) == (2, [7, 14], 21)
    assert s.auto_reply is False


# --- day 2: sent -> waiting ---------------------------------------------------

@pytest.mark.parametrize("days,expected", [(0, "none"), (1.9, "none"), (2, "move_waiting"), (6.9, "move_waiting")])
def test_waiting_move_boundary(days, expected):
    assert _act(_item("sent"), days).kind == expected


# --- day 7 / 14: follow-ups ---------------------------------------------------

@pytest.mark.parametrize("days", [2, 6.9])
def test_no_followup_before_day_7(days):
    assert _act(_item("waiting"), days).kind == "none"


def test_followup_1_at_day_7():
    a = _act(_item("waiting"), 7)
    assert (a.kind, a.number) == ("send_followup", 1)


def test_followup_2_not_before_day_14():
    item = _item("waiting", followup_count=1)
    sent_day7 = dict(followups_sent=1, last_followup_sent_at=at(7))
    assert _act(item, 13.9, **sent_day7).kind == "none"
    a = _act(item, 14, **sent_day7)
    assert (a.kind, a.number) == ("send_followup", 2)


def test_downtime_catch_up_sends_one_at_a_time_with_spacing():
    """Silent for 20 days with nothing sent: only #1 is due now; #2 waits for
    the 3-day spacing after #1 actually goes out."""
    item = _item("waiting")
    assert _act(item, 20).number == 1

    after_1 = _item("waiting", followup_count=1)
    just_sent = dict(followups_sent=1, last_followup_sent_at=at(20))
    assert _act(after_1, 20.5, **just_sent).kind == "none"
    assert _act(after_1, 23, **just_sent).number == 2


# --- day 21: terminal ---------------------------------------------------------

def test_terminal_only_after_followup_2_actually_sent():
    done = _item("waiting", followup_count=2)
    sent2 = dict(followups_sent=2, last_followup_sent_at=at(14))
    assert _act(done, 20.9, **sent2).kind == "none"
    assert _act(done, 21, **sent2).kind == "mark_no_response"


def test_no_terminal_when_followups_were_never_sent():
    """auto_reply off / failed sends: the card stays in Waiting."""
    item = _item("waiting", followup_count=0)
    for days in (21, 40, 90):
        assert _act(item, days, attempt_status={1: "drafted"}).kind != "mark_no_response"


def test_no_terminal_when_only_followup_1_was_sent():
    item = _item("waiting", followup_count=1)
    a = _act(item, 30, followups_sent=1, last_followup_sent_at=at(25))
    assert a.kind != "mark_no_response"


def test_late_followup_2_delays_terminal_by_the_spacing():
    item = _item("waiting", followup_count=2)
    assert _act(item, 21.5, followups_sent=2, last_followup_sent_at=at(21)).kind == "none"
    assert _act(item, 24, followups_sent=2, last_followup_sent_at=at(21)).kind == "mark_no_response"


# --- never contact ------------------------------------------------------------

@pytest.mark.parametrize("ctx", [
    dict(inbound_intents={"interested"}),
    dict(inbound_intents={"not_interested"}),
    dict(inbound_intents={"bounce"}),
    dict(inbound_intents={"unclassified"}),
    dict(has_booking=True),
], ids=["replied", "opted-out", "bounced", "unclassified-inbound", "booked-or-canceled-booking"])
def test_blockers_stop_everything_at_every_day(ctx):
    for stage in ("sent", "waiting"):
        for days in (3, 8, 15, 22, 40):
            assert _act(_item(stage, followup_count=2), days, followups_sent=2, **ctx).kind == "none"


def test_a_human_reply_blocks_even_without_an_intent_record():
    item = _item("waiting", last_inbound_at=at(3))
    assert _act(item, 30).kind == "none"


@pytest.mark.parametrize("stage", ["pending", "needs_review", "replied", "booked", "not_interested", "failed"])
def test_other_stages_are_never_touched(stage):
    assert _act(_item(stage), 30).kind == "none"


def test_out_of_office_does_not_stop_the_sequence():
    assert _act(_item("waiting"), 8, inbound_intents={"out_of_office"}).kind == "send_followup"


# --- ledger awareness -----------------------------------------------------------

@pytest.mark.parametrize("status", ["claimed", "drafted", "skipped"])
def test_a_number_already_in_the_ledger_is_not_selected_again(status):
    assert _act(_item("waiting"), 8, attempt_status={1: status}).kind == "none"


def test_failed_send_is_retried_until_the_limit():
    item = _item("waiting")
    assert _act(item, 8, attempt_status={1: "failed"}, attempt_failures={1: 2}).kind == "send_followup"
    assert _act(item, 8, attempt_status={1: "failed"}, attempt_failures={1: 3}).kind == "none"


def test_zero_followups_configured_never_sends_or_terminates():
    s = _settings(max_followups=0)
    assert _act(_item("waiting"), 30, s).kind == "none"


# --- time handling ----------------------------------------------------------------

def test_timezone_aware_sent_at_is_compared_as_utc():
    from datetime import timezone

    aware = _item("sent", sent_at=SENT_AT.replace(tzinfo=timezone.utc))
    assert _act(aware, 2).kind == "move_waiting"
