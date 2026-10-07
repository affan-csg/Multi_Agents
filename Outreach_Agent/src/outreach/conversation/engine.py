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

import hashlib
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
    followups_failed: int = 0
    moved_to_waiting: int = 0
    moved_to_no_response: int = 0
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
            "followups_failed": self.followups_failed,
            "moved_to_waiting": self.moved_to_waiting,
            "moved_to_no_response": self.moved_to_no_response,
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
        message_id=f"in-{item.item_id}-{hashlib.sha1((email_in.message_id or '').encode('utf-8')).hexdigest()[:16]}",
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


def _has_booking(conn: SupabaseConn, item: OutreachItem) -> bool:
    """Any Calendly booking tied to this prospect, active OR canceled. A
    canceled booking reverts the card to waiting, and that must not restart
    chasing someone who already engaged enough to book."""
    if conn.select("calendly_events", item_id=f"eq.{item.item_id}", limit=1):
        return True
    for email_addr in {item.to_email, (item.to_email or "").strip().lower()} - {None, ""}:
        if conn.select("calendly_events", invitee_email=f"eq.{email_addr}", limit=1):
            return True
    return False


def _build_context(
    conn: SupabaseConn, item: OutreachItem, thread: list[ConversationMessage],
) -> followup.FollowupContext:
    attempts = db.list_followup_attempts(conn, item.item_id)
    sent = [a for a in attempts if a.status == "sent"]
    intents = {
        (m.intent or "unclassified") for m in thread if m.direction == "inbound"
    }
    last_outbound = next(
        (m.sent_at for m in reversed(thread) if m.direction == "outbound" and m.sent_at),
        None,
    )
    return followup.FollowupContext(
        inbound_intents=intents,
        has_booking=_has_booking(conn, item),
        last_followup_sent_at=last_outbound,
        followups_sent=max(len(sent), item.followup_count),
        attempt_status={a.followup_number: a.status for a in attempts},
        attempt_failures={a.followup_number: a.attempts for a in attempts if a.status == "failed"},
    )


def _presend_check(
    conn: SupabaseConn, campaign_id: str, item_id: str, number: Optional[int], now: datetime,
) -> tuple[Optional[OutreachItem], Optional[ConversationSettings], Optional[followup.Action], str]:
    """Re-read everything that could have changed since the tick started and
    re-derive the action from fresh state. Returns (item, settings, action,
    why_not): `action` is None when the item must not be touched.

    This is the last gate before an email or a terminal move: the item row,
    its thread, Calendly bookings and the master switch are all read again,
    so a reply or booking that landed a moment ago wins over the schedule."""
    if not is_enabled(conn, campaign_id):
        return None, None, None, "agent was switched off mid-tick; automation stopped"
    fresh = db.get_item(conn, item_id)
    if fresh is None:
        return None, None, None, "item no longer exists"
    settings = get_settings(conn, campaign_id)
    thread = db.list_conversation_messages(conn, item_id)
    ctx = _build_context(conn, fresh, thread)
    # The caller's own claim is in the ledger as "claimed"; for the purpose of
    # re-deriving "is this number due?" that row must not count against it.
    if number is not None and ctx.attempt_status.get(number) == "claimed":
        ctx.attempt_status.pop(number)
    action = followup.next_action(fresh, settings, ctx, now=now)
    if action.kind == "none" or (number is not None and action.number != number):
        return fresh, settings, None, action.reason or "no longer due"
    return fresh, settings, action, ""


def _history_text(thread: list[ConversationMessage]) -> str:
    lines = []
    for m in thread:
        if m.draft_pending:
            continue
        who = "Prospect" if m.direction == "inbound" else "Us"
        lines.append(f"{who}: {(m.body or '').strip()[:600]}")
    return "\n\n".join(lines)


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
    """Advance one silent item along the schedule: Waiting at day 2,
    follow-ups at their configured days, terminal after the last one."""
    # A follow-up draft a person approved and sent by hand counts as sent, so
    # the sequence continues (#2, then the day-21 terminal move).
    for a in db.list_followup_attempts(conn, item.item_id):
        if a.status != "drafted" or not a.message_id:
            continue
        m = db.get_conversation_message(conn, a.message_id)
        if m and not m.draft_pending and m.sent_at:
            db.update_followup_attempt(
                conn, item.item_id, a.followup_number, status="sent",
                provider_message_id=m.provider_message_id,
            )
    ctx = _build_context(conn, item, thread)

    # Reconcile a crash between "send recorded in the ledger" and "count
    # incremented": the ledger is the truth about what was actually sent.
    if ctx.followups_sent > item.followup_count:
        db.update_item_fields(conn, item.item_id, followup_count=ctx.followups_sent)
        item.followup_count = ctx.followups_sent

    action = followup.next_action(item, settings, ctx, now=now)

    if action.kind == "move_waiting":
        if _advance(conn, item, "waiting", reason=action.reason):
            result.moved_to_waiting += 1
            item.stage = "waiting"
        # An item picked up late (downtime) may already be due for a nudge;
        # evaluate once more rather than waiting a whole interval.
        action = followup.next_action(item, settings, ctx, now=now)

    if action.kind == "mark_no_response":
        _mark_no_response(conn, campaign_id, item, now, result)
        return

    if action.kind != "send_followup":
        return

    _run_followup(conn, cfg, campaign_id, item, int(action.number or 0), result, now)


def _mark_no_response(
    conn: SupabaseConn, campaign_id: str, item: OutreachItem, now: datetime, result: TickResult,
) -> None:
    fresh, _settings, action, why = _presend_check(conn, campaign_id, item.item_id, None, now)
    if action is None or action.kind != "mark_no_response" or fresh is None:
        if why:
            result.message = result.message or why
        return
    if _advance(
        conn, fresh, "not_interested", reason="no_response",
        detail={
            "followups_sent": fresh.followup_count,
            "days_since_sent": round(followup.days_since(fresh.sent_at, now=now) or 0, 1),
        },
    ):
        result.moved_to_no_response += 1
        item.stage = "not_interested"


def _run_followup(
    conn: SupabaseConn,
    cfg: Config,
    campaign_id: str,
    item: OutreachItem,
    number: int,
    result: TickResult,
    now: datetime,
) -> None:
    """Claim, re-verify, compose, then send (or draft) follow-up `number`."""
    to_email = item.to_email
    if not to_email or number < 1:
        return

    # --- idempotency: exactly one winner per (item, number), ever ------------
    # Settings are read fresh: auto_reply decides between send and draft, and
    # may have been flipped since the tick started.
    live_settings = get_settings(conn, campaign_id)
    prior = db.get_followup_attempt(conn, item.item_id, number)
    if prior is None:
        if db.claim_followup_attempt(conn, item.item_id, number) is None:
            return  # another tick/replica claimed it between our read and insert
        failures_so_far = 0
    elif (
        prior.status == "failed"
        and prior.attempts < followup.MAX_SEND_ATTEMPTS
        and live_settings.enabled
        and live_settings.auto_reply
    ):
        if not db.reclaim_failed_followup(conn, item.item_id, number):
            return
        failures_so_far = prior.attempts
    else:
        return

    def release(reason: str) -> None:
        # A benign abort (reply landed, agent switched off, no LLM): drop the
        # claim so the number can be attempted again if it becomes due again.
        # A previously failed attempt keeps its failure count instead.
        if failures_so_far:
            db.update_followup_attempt(
                conn, item.item_id, number, status="failed", attempts=failures_so_far,
            )
        else:
            conn.delete(
                "followup_attempts", item_id=f"eq.{item.item_id}",
                followup_number=f"eq.{number}", status="eq.claimed",
            )
        result.message = result.message or reason

    # --- last look before anything leaves the building -----------------------
    fresh, fresh_settings, action, why = _presend_check(conn, campaign_id, item.item_id, number, now)
    if action is None or action.kind != "send_followup" or fresh is None or fresh_settings is None:
        release(why)
        return

    thread = db.list_conversation_messages(conn, fresh.item_id)
    previous = []
    for a in db.list_followup_attempts(conn, fresh.item_id):
        if a.status == "sent" and a.message_id:
            m = db.get_conversation_message(conn, a.message_id)
            if m and m.body:
                previous.append(m.body)

    composed = responder.compose_followup(
        cfg, fresh, db.get_prospect(conn, fresh.research_domain),
        followup_number=number, max_followups=followup.total_followups(fresh_settings),
        previous_followups=previous, conversation_history=_history_text(thread),
    )
    if composed is None:
        msg = "could not draft a follow-up (no LLM available or unparseable response)"
        release(msg)
        result.errors[fresh.research_domain] = msg
        return
    subject, body = composed

    # The composition took a while (an LLM call): look again at everything --
    # the switch, replies, bookings -- immediately before the send.
    fresh2, fresh_settings, action, why = _presend_check(conn, campaign_id, item.item_id, number, now)
    if action is None or fresh2 is None or fresh_settings is None:
        release(why)
        return
    fresh = fresh2

    if not fresh_settings.auto_reply:
        # Draft-only (agent ON, Auto Reply OFF): a person sends it like any
        # other draft. One draft per number -- the ledger row stays as
        # "drafted", so later ticks do not write another.
        outcome = responder.store_and_maybe_send(
            conn, cfg, fresh, fresh_settings,
            subject=subject, body=body, to_email=to_email, now=now,
            in_reply_to=fresh.provider_message_id,
        )
        db.update_followup_attempt(
            conn, fresh.item_id, number, status="drafted", message_id=outcome.message_id,
        )
        result.followups_drafted += 1
        return

    outcome = responder.store_and_maybe_send(
        conn, cfg, fresh, fresh_settings,
        subject=subject, body=body, to_email=to_email, now=now,
        # Threads against the ORIGINAL outreach send so it lands in the same
        # conversation instead of starting a new one.
        in_reply_to=fresh.provider_message_id,
        keep_draft_on_error=False,
    )
    if outcome.error:
        db.update_followup_attempt(
            conn, fresh.item_id, number, status="failed",
            attempts=failures_so_far + 1, error=outcome.error[:500],
        )
        result.followups_failed += 1
        result.errors[fresh.research_domain] = outcome.error
        return

    # Ledger first, count second: a crash between the two is reconciled from
    # the ledger on the next tick (see _process_followup).
    db.update_followup_attempt(
        conn, fresh.item_id, number, status="sent", attempts=failures_so_far + 1,
        provider_message_id=outcome.provider_message_id, message_id=outcome.message_id,
        error=None,
    )
    db.update_item_fields(conn, fresh.item_id, followup_count=fresh.followup_count + 1)
    result.followups_sent += 1


def _calendly_fresh(conn: SupabaseConn, cfg: Config, now: datetime) -> bool:
    """Whether our picture of Calendly bookings is recent enough to trust.
    Not configured counts as fresh (there is nothing to be stale about)."""
    if not getattr(cfg, "calendly_api_token", None):
        return True
    state = db.get_automation_state(conn)
    if state.last_calendly_success_at is None:
        return False
    age = now - followup._naive(state.last_calendly_success_at)
    return age <= timedelta(minutes=2 * max(1, state.interval_minutes))


def tick(
    conn: SupabaseConn,
    cfg: Config,
    campaign_id: str,
    *,
    emails: Optional[list[InboundEmail]] = None,
    now: Optional[datetime] = None,
    inbox_ok: Optional[bool] = None,
    calendly_ok: Optional[bool] = None,
) -> TickResult:
    """Run one conversation cycle for one campaign.

    `inbox_ok` / `calendly_ok` say whether the mailbox and Calendly were
    successfully read for this cycle. The follow-up phase (waiting move,
    follow-ups, terminal) runs only if both are good: with a failed or stale
    sync we cannot know whether the prospect replied or booked, so nothing is
    sent "blindly". `None` means "work it out": a supplied `emails` batch
    counts as a good inbox read, a fetch is judged by whether it raised, and
    Calendly is judged by the last recorded successful sync.

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
    inbox_good = True if inbox_ok is None else inbox_ok
    if emails is None:
        if not inbox.imap_configured():
            result.message = (
                "IMAP is not configured -- set IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD "
                "in .env to detect replies (see .env.example)"
            )
            emails = []
            inbox_good = False
        else:
            try:
                emails = inbox.fetch_all_replies(cfg, lookback_days=cfg.imap_lookback_days)
            except InboxError as exc:
                result.message = str(exc)
                emails = []
                inbox_good = False

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
    calendly_good = _calendly_fresh(conn, cfg, now) if calendly_ok is None else calendly_ok
    if not inbox_good or not calendly_good:
        skipped = "inbox" if not inbox_good else "Calendly"
        result.message = (
            (result.message + " -- " if result.message else "")
            + f"follow-ups skipped: {skipped} state could not be verified this cycle"
        )
        return result

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
    inbox_ok: Optional[bool] = None,
    calendly_ok: Optional[bool] = None,
) -> list[TickResult]:
    """Tick every campaign that has the agent switched on.

    Campaigns with it off are not merely skipped -- they are never listed,
    because the query itself filters on enabled=true."""
    return [
        tick(conn, cfg, campaign_id, emails=emails, now=now,
             inbox_ok=inbox_ok, calendly_ok=calendly_ok)
        for campaign_id in db.list_enabled_conversation_campaigns(conn)
    ]
