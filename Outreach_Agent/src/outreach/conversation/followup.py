"""Follow-up policy: who deserves a nudge, and who has had enough.

This module is pure decision-making -- it reads state and answers yes or no.
It sends nothing and writes nothing; engine.py acts on its verdicts. Keeping
it side-effect free is what lets the timing rules be tested exhaustively
without a mailbox, a clock, or a database.

Two separate things happen to an unanswered item, and they are easy to
conflate:

  1. sent -> waiting. A bookkeeping move that records "enough time has passed
     that silence is now meaningful". No email is involved, so it happens
     regardless of auto_reply.

  2. Sending an actual nudge. A real email to a real person, capped at
     `max_followups` and gated on auto_reply exactly like a reply is.

An item that has received a human reply is never followed up, whatever its
stage: chasing someone who already answered is the single most damaging
thing an outreach automation can do.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..models import ConversationSettings, OutreachItem

# Minimum spacing between consecutive nudges, independent of
# followup_after_days (which measures silence since the ORIGINAL send). Without
# this, raising max_followups would fire every remaining nudge on one tick,
# since each would independently see the same long silence since the original.
_MIN_DAYS_BETWEEN_FOLLOWUPS = 3


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Drop tzinfo so stored timestamps (naive UTC, per db.utcnow) and parsed
    ones compare without raising."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def days_since(moment: Optional[datetime], *, now: datetime) -> Optional[float]:
    moment = _naive(moment)
    if moment is None:
        return None
    return (now - moment).total_seconds() / 86400.0


def should_move_to_waiting(
    item: OutreachItem, settings: ConversationSettings, *, now: datetime,
) -> bool:
    """Whether a sent item has gone unanswered long enough to count as
    Waiting. Bookkeeping only -- no email is sent by this transition."""
    if item.stage != "sent":
        return False
    if item.last_inbound_at is not None:
        return False  # they replied; silence is not the story here

    elapsed = days_since(item.sent_at, now=now)
    if elapsed is None:
        return False
    return elapsed >= settings.followup_after_days


def should_send_followup(
    item: OutreachItem,
    settings: ConversationSettings,
    last_outbound_at: Optional[datetime],
    *,
    now: datetime,
) -> bool:
    """Whether to send an actual nudge email now.

    Every condition here is a reason NOT to send; the default is silence.
    The caller must still check auto_reply -- this answers "is a nudge due?",
    not "are we allowed to send it?"."""
    # Only genuinely unanswered items, and only ones already parked in
    # waiting -- sent items get moved to waiting first, on an earlier tick,
    # so a nudge is never the very first thing that happens to a card.
    if item.stage != "waiting":
        return False
    if item.last_inbound_at is not None:
        return False

    if settings.max_followups <= 0:
        return False
    if item.followup_count >= settings.max_followups:
        return False

    elapsed_since_send = days_since(item.sent_at, now=now)
    if elapsed_since_send is None or elapsed_since_send < settings.followup_after_days:
        return False

    # Space consecutive nudges out, measured from the last thing we actually
    # sent rather than from the original email.
    if item.followup_count > 0:
        elapsed_since_last = days_since(last_outbound_at, now=now)
        if elapsed_since_last is None or elapsed_since_last < _MIN_DAYS_BETWEEN_FOLLOWUPS:
            return False

    return True


def next_followup_number(item: OutreachItem) -> int:
    """1-based index of the nudge about to be sent, for the prompt's
    "follow-up N of M" framing."""
    return item.followup_count + 1
