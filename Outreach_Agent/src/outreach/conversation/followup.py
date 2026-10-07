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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

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


@dataclass
class FollowupContext:
    """Facts outside the item row that decide eligibility. Gathered by the
    engine from the thread and Calendly tables, so this module stays pure."""

    inbound_intents: set[str] = field(default_factory=set)
    has_booking: bool = False  # any calendly_events row, active OR canceled
    last_followup_sent_at: Optional[datetime] = None
    followups_sent: int = 0    # ledger rows with status "sent" (or the item's count)
    # followup_number -> ledger status / failed-send count, so a number that is
    # already claimed, drafted or out of retries is never selected again.
    attempt_status: dict[int, str] = field(default_factory=dict)
    attempt_failures: dict[int, int] = field(default_factory=dict)


NextActionKind = Literal["none", "move_waiting", "send_followup", "mark_no_response"]


@dataclass
class Action:
    kind: NextActionKind
    reason: str = ""
    number: Optional[int] = None       # follow-up number for send_followup
    due_in_days: Optional[float] = None  # for "none": days until the next action

    @property
    def is_due(self) -> bool:
        return self.kind != "none"


# Intents that end a sequence the same way a reply does. An out-of-office is
# deliberately NOT here: it is not a human decision, so the sequence continues.
# An inbound message nobody classified yet is treated as blocking too: until
# we know what it says, we do not email the person who wrote it.
_BLOCKING_INTENTS = frozenset(
    {"bounce", "not_interested", "interested", "booked", "unclear", "unclassified"}
)

# A failed send is retried on later ticks up to this many times in total.
MAX_SEND_ATTEMPTS = 3


def total_followups(settings: ConversationSettings) -> int:
    """Number of follow-ups this campaign will ever send."""
    return max(0, min(settings.max_followups, len(settings.followup_days)))


def ineligible_reason(item: OutreachItem, ctx: FollowupContext) -> Optional[str]:
    """Why this item must never be contacted automatically, or None."""
    if item.stage not in ("sent", "waiting"):
        return f"stage is {item.stage}"
    if item.last_inbound_at is not None:
        return "prospect replied"
    blocking = ctx.inbound_intents & _BLOCKING_INTENTS
    if blocking:
        return f"inbound intent: {sorted(blocking)[0]}"
    if ctx.has_booking:
        return "has a Calendly booking"
    return None


def next_action(
    item: OutreachItem,
    settings: ConversationSettings,
    ctx: FollowupContext,
    *,
    now: datetime,
) -> Action:
    """The single next thing the schedule calls for. Pure; sends nothing.

    Schedule (UTC, all measured from the ORIGINAL sent_at):
        followup_after_days  -> sent -> waiting
        followup_days[n-1]   -> follow-up n (one per tick, 3 days apart at least)
        no_response_after_days -> terminal, only after the LAST follow-up was
                                  actually sent (ledger status "sent")"""
    reason = ineligible_reason(item, ctx)
    if reason:
        return Action("none", reason)

    elapsed = days_since(item.sent_at, now=now)
    if elapsed is None:
        return Action("none", "no sent_at")

    if item.stage == "sent":
        if elapsed >= settings.followup_after_days:
            return Action("move_waiting", f"no reply after {settings.followup_after_days} days")
        return Action("none", "waiting period not reached",
                      due_in_days=settings.followup_after_days - elapsed)

    # stage == waiting from here on
    total = total_followups(settings)
    sent = item.followup_count
    since_last = days_since(ctx.last_followup_sent_at, now=now) if sent > 0 else None
    spaced = sent == 0 or (since_last is not None and since_last >= _MIN_DAYS_BETWEEN_FOLLOWUPS)
    spacing_wait = (
        0.0 if sent == 0 or since_last is None else max(0.0, _MIN_DAYS_BETWEEN_FOLLOWUPS - since_last)
    )

    if sent < total:
        n = sent + 1
        status = ctx.attempt_status.get(n)
        if status in ("claimed", "drafted", "skipped", "sent"):
            return Action("none", f"follow-up #{n} already {status}", number=n)
        if status == "failed" and ctx.attempt_failures.get(n, 0) >= MAX_SEND_ATTEMPTS:
            return Action("none", f"follow-up #{n} failed {MAX_SEND_ATTEMPTS} times", number=n)
        due_day = settings.followup_days[n - 1]
        if elapsed < due_day:
            return Action("none", f"follow-up #{n} not due", number=n,
                          due_in_days=max(due_day - elapsed, spacing_wait))
        if not spaced:
            return Action("none", f"follow-up #{n} waiting for spacing", number=n,
                          due_in_days=spacing_wait)
        return Action("send_followup", f"follow-up #{n} due at day {due_day}", number=n)

    # Every follow-up has gone out. The terminal move needs the last one to be
    # a real SENT email (not a draft, not a failure) -- ctx.followups_sent is
    # the ledger count, item.followup_count is incremented only on a real send.
    if total == 0 or ctx.followups_sent < total:
        return Action("none", "last follow-up was not actually sent")
    if elapsed < settings.no_response_after_days:
        return Action("none", "no-response day not reached",
                      due_in_days=max(settings.no_response_after_days - elapsed, spacing_wait))
    if not spaced:
        return Action("none", "waiting for spacing after last follow-up", due_in_days=spacing_wait)
    return Action("mark_no_response", f"no response after {settings.no_response_after_days} days")


def next_followup_number(item: OutreachItem) -> int:
    """1-based index of the nudge about to be sent, for the prompt's
    "follow-up N of M" framing."""
    return item.followup_count + 1
