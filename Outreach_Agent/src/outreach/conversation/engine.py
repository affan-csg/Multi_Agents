"""The tick: the Conversation Agent's one entry point and its only writer.

Everything else in this package decides things. This module is the only one
that changes anything -- it is the single place where a stage is written, a
reply is stored, or a send is authorized. Concentrating the writes here is
what makes the safety properties checkable in one file instead of seven.

One tick, for one campaign:

    0. Refuse immediately unless the agent is enabled for this campaign.
    1. Load only conversation-stage items (models.CONVERSATION_STAGES). An
       item in pending or needs_review is never loaded, so the Outreach
       Agent's work is structurally out of reach rather than skipped by a
       conditional.
    2. Fetch recent mail, match each message to an item, store it. The
       (item_id, provider_message_id) constraint absorbs anything already
       seen, so re-reading the same reply is free.
    3. Classify each genuinely new inbound message and advance its card.
    4. Compose replies where warranted -- sending only if auto_reply is on.
    5. Move silent items to waiting and nudge them, within the caps.

Every stage write goes through _advance, which enforces the forward-only
whitelist and records the transition through db.set_stage with
actor="conversation_agent". There is no other stage write in this package.

Failure policy: one item's failure is recorded on that item and the tick
carries on. A single prospect with a malformed address must never stop the
other forty-nine from being processed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .. import db
from ..config import Config
from ..db import SupabaseConn, utcnow
from ..models import (
    CONVERSATION_STAGES,
    CONVERSATION_TRANSITIONS,
    HUMAN_REPLY_INTENTS,
    ConversationMessage,
    ConversationSettings,
    OutreachItem,
    ReplyIntent,
    Stage,
)
from . import classifier, followup, inbox, matcher, responder
from .inbox import InboundEmail, InboxError
from .settings import get_settings, is_enabled

# Who the audit trail credits for this agent's moves. Distinct from "system"
# (the Outreach Agent) and "user", so outreach_events can always answer which
# of the three moved a card.
ACTOR = "conversation_agent"

# A lock older than this is presumed abandoned by a crashed tick and may be
# reclaimed. Comfortably longer than a normal tick, which is seconds.
_LOCK_STALE_AFTER = timedelta(minutes=10)

# Where a reply lands, by intent. out_of_office and bounce are absent on
# purpose: mail arrived, but no human replied, so the card must not move.
#
# not_interested routes to its own terminal stage (labeled "Failed" in the
# UI), not to replied: a clear rejection is a different outcome from a reply
# that is still open, and conflating the two would hide rejections inside
# the Replied column's counts everywhere they're used (analytics, learning,
# optimization). responder._REPLYABLE already excludes not_interested, so no
# automated reply is ever sent once a card lands here.
_INTENT_STAGE: dict[str, Stage] = {
    "interested": "replied",
    "unclear": "replied",
    "not_interested": "not_interested",
    "booked": "booked",
}


@dataclass
class TickResult:
    """What one tick did. Every counter is something that actually happened,
    so an all-zero result means the tick was genuinely a no-op."""

    campaign_id: str
    enabled: bool = True
    fetched: int = 0            # messages read off IMAP
    matched: int = 0            # messages tied to an item
    new_inbound: int = 0        # genuinely new (not already stored)
    duplicates: int = 0         # already-stored messages, harmlessly re-read
    unmatched: int = 0          # mail in the box belonging to no campaign item
    classified: dict[str, int] = field(default_factory=dict)
    advanced: int = 0           # cards moved
    replies_sent: int = 0
    replies_drafted: int = 0    # composed but held for approval
    followups_sent: int = 0
    followups_drafted: int = 0
    moved_to_waiting: int = 0
    skipped_locked: int = 0     # items another tick was already holding
    errors: dict[str, str] = field(default_factory=dict)
    message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "enabled": self.enabled,
            "fetched": self.fetched,
            "matched": self.matched,
            "new_inbound": self.new_inbound,
            "duplicates": self.duplicates,
            "unmatched": self.unmatched,
            "classified": self.classified,
            "advanced": self.advanced,
            "replies_sent": self.replies_sent,
            "replies_drafted": self.replies_drafted,
            "followups_sent": self.followups_sent,
            "followups_drafted": self.followups_drafted,
            "moved_to_waiting": self.moved_to_waiting,
            "skipped_locked": self.skipped_locked,
            "errors": self.errors,
            "message": self.message,
        }


def _advance(
    conn: SupabaseConn, item: OutreachItem, to_stage: Stage, *, reason: str,
    detail: Optional[dict[str, Any]] = None,
) -> bool:
    """The only stage write in this package.

    Rejects any transition not in CONVERSATION_TRANSITIONS, which makes three
    classes of bug impossible rather than unlikely: moving a card backwards,
    skipping a stage, and touching an item this agent does not own (a stage
    absent from the map has no legal destinations at all).

    Returns False for a rejected or redundant move. Delegates the actual
    write to db.set_stage, so the agent's moves are audited in outreach_events
    identically to a person's."""
    if item.stage == to_stage:
        return False

    allowed = CONVERSATION_TRANSITIONS.get(item.stage)
    if allowed is None or to_stage not in allowed:
        return False

    db.set_stage(
        conn, item.item_id, to_stage, actor=ACTOR,
        detail={"reason": reason, **(detail or {})},
    )
    return True


def _load_threads(conn: SupabaseConn, items: list[OutreachItem]) -> dict[str, list[ConversationMessage]]:
    return {item.item_id: db.list_conversation_messages(conn, item.item_id) for item in items}


def _store_inbound(
    conn: SupabaseConn, item: OutreachItem, email_in: InboundEmail, now: datetime,
) -> Optional[ConversationMessage]:
    """Store one inbound reply. Returns None if it was already there.

    That None is the idempotency guarantee in action: a message already on
    file is not re-classified and does not move the card a second time, so a
    tick can be run as often as you like with no cumulative effect."""
    message = ConversationMessage(
        message_id=f"in-{item.item_id}-{abs(hash(email_in.message_id))}",
        item_id=item.item_id,
        direction="inbound",
        provider_message_id=email_in.message_id,
        in_reply_to=email_in.in_reply_to,
        subject=email_in.subject,
        body=email_in.body,
        from_email=email_in.from_email,
        to_email=email_in.to_email,
        received_at=email_in.received_at or now,
        created_at=now,
    )
    stored = db.add_conversation_message(conn, message)
    return message if stored else None


def _process_inbound(
    conn: SupabaseConn,
    cfg: Config,
    campaign_id: str,
    item: OutreachItem,
    message: ConversationMessage,
    email_in: InboundEmail,
    settings: ConversationSettings,
    thread: list[ConversationMessage],
    result: TickResult,
    now: datetime,
) -> None:
    """Classify one new reply, move its card, and answer it if warranted."""
    intent = classifier.classify(cfg, email_in)
    db.update_conversation_message(conn, message.message_id, intent=intent)
    result.classified[intent] = result.classified.get(intent, 0) + 1

    # An autoresponder or a bounce is not a reply. Recording it is useful
    # history, but it must not move the card or mark the prospect as having
    # engaged -- last_inbound_at deliberately stays unset so follow-ups
    # continue for someone who was merely on holiday.
    if intent not in HUMAN_REPLY_INTENTS:
        return

    db.update_item_fields(
        conn, item.item_id,
        last_inbound_at=(message.received_at or now).isoformat(),
    )

    target = _INTENT_STAGE.get(intent)
    if target and _advance(
        conn, item, target, reason=f"reply classified as {intent}",
        detail={"intent": intent, "provider_message_id": email_in.message_id},
    ):
        result.advanced += 1
        item.stage = target  # keep the in-memory copy honest for later checks

    if not responder.should_reply(intent):
        return

    to_email = item.to_email or email_in.from_email
    if not to_email:
        result.errors[item.research_domain] = "no recipient address to reply to"
        return

    composed = responder.compose_reply(
        cfg, item, db.get_prospect(conn, item.research_domain),
        thread + [message], message, intent,
    )
    if composed is None:
        result.errors[item.research_domain] = "could not draft a reply (no LLM available or unparseable response)"
        return

    subject, body = composed

    # Re-read the master switch immediately before sending. A tick that
    # started before someone flipped the agent off must not send.
    if not is_enabled(conn, campaign_id):
        result.message = "agent was switched off mid-tick; sending stopped"
        return

    outcome = responder.store_and_maybe_send(
        conn, cfg, item, settings,
        subject=subject, body=body, to_email=to_email,
        in_reply_to=email_in.message_id, now=now,
    )
    if outcome.error:
        result.errors[item.research_domain] = outcome.error
    elif outcome.draft_pending:
        result.replies_drafted += 1
    else:
        result.replies_sent += 1


def _process_followup(
    conn: SupabaseConn,
    cfg: Config,
    campaign_id: str,
    item: OutreachItem,
    settings: ConversationSettings,
    thread: list[ConversationMessage],
    result: TickResult,
    now: datetime,
) -> None:
    """Move a silent item to waiting, and nudge it if one is due."""
    if followup.should_move_to_waiting(item, settings, now=now):
        if _advance(
            conn, item, "waiting",
            reason=f"no reply after {settings.followup_after_days} days",
        ):
            result.moved_to_waiting += 1
            item.stage = "waiting"

    last_outbound = next(
        (m.sent_at for m in reversed(thread) if m.direction == "outbound" and m.sent_at),
        None,
    )
    if not followup.should_send_followup(item, settings, last_outbound, now=now):
        return

    to_email = item.to_email
    if not to_email:
        return

    number = followup.next_followup_number(item)
    composed = responder.compose_followup(
        cfg, item, db.get_prospect(conn, item.research_domain),
        followup_number=number, max_followups=settings.max_followups,
    )
    if composed is None:
        result.errors[item.research_domain] = "could not draft a follow-up (no LLM available or unparseable response)"
        return

    subject, body = composed

    if not is_enabled(conn, campaign_id):
        result.message = "agent was switched off mid-tick; sending stopped"
        return

    outcome = responder.store_and_maybe_send(
        conn, cfg, item, settings,
        subject=subject, body=body, to_email=to_email, now=now,
        # A follow-up answers nothing yet (no reply has arrived) -- thread it
        # against the ORIGINAL outreach send instead, so it still lands in
        # the same conversation rather than starting a new one.
        in_reply_to=item.provider_message_id,
    )
    if outcome.error:
        result.errors[item.research_domain] = outcome.error
        return

    if outcome.draft_pending:
        result.followups_drafted += 1
        # Not counted against the cap: nothing was sent, so the prospect has
        # not been nudged. Counting it here would silently consume an
        # allowance a person may never approve.
        return

    result.followups_sent += 1
    db.update_item_fields(conn, item.item_id, followup_count=item.followup_count + 1)


def tick(
    conn: SupabaseConn,
    cfg: Config,
    campaign_id: str,
    *,
    emails: Optional[list[InboundEmail]] = None,
    now: Optional[datetime] = None,
) -> TickResult:
    """Run one conversation cycle for one campaign.

    `emails` bypasses IMAP with a supplied batch -- the seam the offline
    tests drive the whole reply path through. `now` does the same for the
    clock, so follow-up timing can be tested without waiting days.

    Never raises for ordinary operational problems (agent off, IMAP not
    configured, one item failing): those are reported in the result so the
    caller can show them. Only a programming error propagates."""
    now = now or utcnow()
    result = TickResult(campaign_id=campaign_id)

    settings = get_settings(conn, campaign_id)
    if not settings.enabled:
        # The master switch. Nothing is read, nothing is sent, nothing moves.
        result.enabled = False
        result.message = "conversation agent is off for this campaign"
        return result

    items = db.list_conversation_items(conn, campaign_id, CONVERSATION_STAGES)
    if not items:
        result.message = "no items in a conversation stage yet"
        return result

    threads = _load_threads(conn, items)

    # --- inbound ------------------------------------------------------------
    if emails is None:
        if not inbox.imap_configured():
            result.message = (
                "IMAP is not configured -- set IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD "
                "in .env to detect replies (see .env.example)"
            )
            emails = []
        else:
            try:
                emails = inbox.fetch_replies(lookback_days=cfg.imap_lookback_days)
            except InboxError as exc:
                result.message = str(exc)
                emails = []

    result.fetched = len(emails)
    outbound_index = matcher.build_outbound_index(threads)

    for email_in in emails:
        item = matcher.match_to_item(email_in, items, outbound_index)
        if item is None:
            result.unmatched += 1
            continue

        result.matched += 1
        stale_before = now - _LOCK_STALE_AFTER
        if not db.claim_conversation_lock(conn, item.item_id, stale_before=stale_before):
            result.skipped_locked += 1
            continue

        try:
            message = _store_inbound(conn, item, email_in, now)
            if message is None:
                result.duplicates += 1
                continue

            result.new_inbound += 1
            _process_inbound(
                conn, cfg, campaign_id, item, message, email_in, settings,
                threads.get(item.item_id, []), result, now,
            )
        except Exception as exc:
            result.errors[item.research_domain] = f"{type(exc).__name__}: {exc}"
        finally:
            db.release_conversation_lock(conn, item.item_id)

    # --- follow-ups ---------------------------------------------------------
    # Re-read items: the inbound pass above may have moved several of them,
    # and a card that just received a reply must not then be followed up.
    for item in db.list_conversation_items(conn, campaign_id, CONVERSATION_STAGES):
        stale_before = now - _LOCK_STALE_AFTER
        if not db.claim_conversation_lock(conn, item.item_id, stale_before=stale_before):
            result.skipped_locked += 1
            continue
        try:
            # Re-read the thread rather than reusing the snapshot taken at
            # the top of the tick: the inbound pass may have appended to it,
            # and the spacing rule between consecutive nudges is measured
            # from the last outbound message. A stale thread would report no
            # previous follow-up and let a second nudge go out immediately.
            _process_followup(
                conn, cfg, campaign_id, item, settings,
                db.list_conversation_messages(conn, item.item_id), result, now,
            )
        except Exception as exc:
            result.errors[item.research_domain] = f"{type(exc).__name__}: {exc}"
        finally:
            db.release_conversation_lock(conn, item.item_id)

    return result


def tick_all(
    conn: SupabaseConn, cfg: Config, *,
    emails: Optional[list[InboundEmail]] = None,
    now: Optional[datetime] = None,
) -> list[TickResult]:
    """Tick every campaign that has the agent switched on.

    Campaigns with it off are not merely skipped -- they are never listed,
    because the query itself filters on enabled=true."""
    return [
        tick(conn, cfg, campaign_id, emails=emails, now=now)
        for campaign_id in db.list_enabled_conversation_campaigns(conn)
    ]
