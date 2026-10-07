"""Domain models for the Outreach Agent.

`ImportedProspect` is the local, cached shape of a Research Agent `Prospect`
(see Research_Agent/src/csg/models.py) -- Outreach's own copy, keyed by the
same `domain` identity Research uses. It is a snapshot, not a live view: once
imported, it can go stale relative to Research's own DB until re-imported.
Nothing here writes back to Research.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Stage = Literal[
    "needs_review", "pending", "sent", "waiting", "replied", "booked", "failed", "not_interested",
]
Channel = Literal["email"]

# Pipeline order, mirrors Dashboard/src/agents/outreach.ts STAGE_ORDER -- keep
# the two in sync if either changes. needs_review sits before pending: an
# item lands there instead of pending only when it was drafted from the
# static template_only fallback -- i.e. there was no research data at all to
# personalize from (not even industry/description/open_reqs), or every LLM
# call failed. See personalize.draft_message, ingest.create_campaign_from_prospects,
# and api.approve_item.
#
# 'not_interested' (displayed as "Failed" in the UI -- see Dashboard's
# STAGE_LABEL) is the Conversation Agent's terminal rejection outcome, placed
# after 'booked'. It is a DIFFERENT concept from 'failed', which is the
# Outreach Agent's send-error stage (SMTP/delivery failure, never reached the
# prospect at all) -- the two names collide in plain English but never in
# code: 'failed' stays in OUTREACH_STAGES, 'not_interested' lives in
# CONVERSATION_STAGES below, and CONVERSATION_TRANSITIONS keeps the Outreach
# Agent's 'failed' completely out of the Conversation Agent's reach.
STAGE_ORDER: list[Stage] = [
    "needs_review", "pending", "sent", "waiting", "replied", "booked", "not_interested", "failed",
]

# --- Stage ownership, split between the two agents ---------------------------
# This split is the core safety property of running both agents against the
# same table: they can never race because they operate on disjoint stage sets.
#
# The Outreach Agent (ingest.py/pipeline.py) writes only OUTREACH_STAGES.
# The Conversation Agent (conversation/engine.py) reads and writes only
# CONVERSATION_STAGES, and its item query is filtered on exactly this list --
# so an item sitting in pending or needs_review is structurally invisible to
# it, not merely skipped by a conditional.
#
# 'sent' is the handover point and the one stage both touch: the Outreach
# Agent writes it (as the end of its job) and the Conversation Agent reads it
# (as the start of its own). Nothing else overlaps, and the Conversation
# Agent never writes 'sent'.
OUTREACH_STAGES: list[Stage] = ["needs_review", "pending", "sent", "failed"]
CONVERSATION_STAGES: list[Stage] = ["sent", "waiting", "replied", "booked", "not_interested"]

# Forward-only transitions the Conversation Agent is permitted to make. Any
# move not in this map is rejected before it reaches the database -- a
# classifier that returns nonsense can never send a card backwards, skip
# 'replied' on the way to 'booked', or resurrect a booked prospect.
#
# 'not_interested' is terminal (empty outbound list, same as 'booked') except
# for the manual escape hatch: a person can always re-open a card via
# api.set_item_stage / the drawer's stage buttons, which calls db.set_stage
# directly with actor="user" and is not bound by this whitelist -- see
# WORKFLOW.md's "manual override always wins" guarantee. This map only limits
# what the Conversation Agent may do on its own.
CONVERSATION_TRANSITIONS: dict[Stage, list[Stage]] = {
    "sent": ["waiting", "replied", "booked", "not_interested"],
    "waiting": ["replied", "booked", "not_interested"],
    "replied": ["booked", "not_interested"],
    "booked": [],
    "not_interested": [],
}


class ImportedProspect(BaseModel):
    """Cached snapshot of a qualified Research Agent prospect."""

    research_domain: str
    research_run_id: Optional[str] = None
    # Every run_id that has ever imported this domain, oldest first --
    # NOT just the latest one. `research_run_id` above is overwritten on each
    # re-import (it's "the run this snapshot's data currently reflects"), but
    # a person viewing the Prospects page filtered to an OLDER run must still
    # see this prospect if that run is the reason it ever got imported at
    # all -- see db.upsert_prospect and the bug this fixes: re-running
    # discovery for "the same research" always gets a fresh run_id
    # (discover.run_discovery has no way to resume an old one), and
    # re-importing the same company then silently flipped research_run_id to
    # the new run, making the prospect vanish from the old run's filtered
    # view even though nothing was deleted. list_prospects(run_id=...) now
    # filters on membership in this list, not equality against the single
    # column, so a prospect stays visible under every run that ever surfaced
    # it. Defaults to [] only for prospects created before this field existed
    # (or the synthetic test fixtures, which pass research_run_id=None).
    research_run_ids: list[str] = Field(default_factory=list)
    name: Optional[str] = None
    tier: Optional[str] = None
    score: Optional[float] = None
    industry: Optional[str] = None
    region: Optional[str] = None
    description: Optional[str] = None
    pain_summary: Optional[str] = None
    recommended_angle: Optional[str] = None
    open_reqs: list[dict[str, Any]] = Field(default_factory=list)
    # 'confirmed' | 'unknown' | None -- whether `open_reqs` being empty means
    # Research actually read a job board/careers page and found nothing, or
    # every discovery method failed and the true count is simply unknown
    # (see Research_Agent's models.HiringSignal). None only for prospects
    # predating this field or the synthetic test fixtures. Personalization
    # must never word an email (or a human reviewing it) around "they have no
    # open roles" when this is "unknown" -- see personalize.py.
    hiring_signal: Optional[Literal["confirmed", "unknown"]] = None

    # True when the Research Agent's last qualification attempt for this
    # domain FAILED (LLM/API error surviving retries) and no successful
    # qualification has landed since -- see Research_Agent's
    # qualification_attempts table / migrations/004. None/False both render
    # the same ("no failure known"); the distinction only matters for
    # prospects imported before this field existed, which read as False
    # (never fetched a failure), not True. Lets the Prospects table show
    # "qualification failed, retry in Research" instead of a bare "-" that
    # looks identical to "never attempted". Refreshed on the next re-import
    # from the same run (see ingest._to_imported_prospect) -- Outreach never
    # calls the Research Agent's qualify endpoints itself, only re-reads
    # whatever Research currently has.
    qualification_failed: bool = False

    # Best contact only -- one person per company, chosen by
    # ingest._best_contact (verification status, then seniority, then
    # Hunter's confidence score). Research's own outreach_ready gate
    # guarantees at least one contact exists when a domain is exposed as a
    # prospect at all (see Prospect.compute_outreach_flags in the Research
    # Agent), but Outreach itself decides which one gets emailed.
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_position: Optional[str] = None
    contact_linkedin_url: Optional[str] = None

    imported_at: Optional[datetime] = None
    raw_prospect: dict[str, Any] = Field(default_factory=dict)

    @property
    def has_contact(self) -> bool:
        return bool(self.contact_email)


class Campaign(BaseModel):
    campaign_id: str
    name: Optional[str] = None
    research_run_id: Optional[str] = None
    channel: Channel = "email"
    selection: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    notes: Optional[str] = None


class OutreachItem(BaseModel):
    """The pipeline unit: one row per (prospect, campaign).

    The Outreach Agent only ever writes pending -> sent (or failed on a send
    error). waiting/replied/booked are owned by the Conversation Agent
    (src/outreach/conversation/) when it is switched on for the campaign, and
    by a person dragging cards otherwise -- see CONVERSATION_STAGES below.
    """

    item_id: str
    campaign_id: str
    research_domain: str
    stage: Stage = "pending"
    channel: Channel = "email"
    subject: Optional[str] = None
    body: Optional[str] = None
    personalized_from: Optional[str] = None
    to_email: Optional[str] = None
    provider_message_id: Optional[str] = None
    # The mailbox that actually sent the first email (None for rows sent
    # before sender tracking that were never backfilled).
    sender_email: Optional[str] = None
    error: Optional[str] = None
    scheduled_at: Optional[datetime] = None  # user-picked send time; informational only, nothing auto-fires it yet
    created_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    stage_updated_at: Optional[datetime] = None

    # --- Conversation Agent fields (see conversation/engine.py) -------------
    # The Outreach Agent never reads or writes these three.
    followup_count: int = 0
    last_inbound_at: Optional[datetime] = None
    # Advisory lock: non-null means a conversation tick is currently
    # processing this item. Prevents two overlapping ticks from both acting
    # on it. Reclaimable once stale -- see conversation.engine._LOCK_STALE_AFTER.
    conversation_locked_at: Optional[datetime] = None


class OutreachEvent(BaseModel):
    event_id: str
    item_id: str
    event_type: str
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    actor: str = "system"
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Conversation Agent models
# ---------------------------------------------------------------------------

# What an inbound reply means, as decided by conversation/classifier.py.
#   interested     -- wants to talk; worth a reply
#   booked         -- has accepted/proposed a concrete meeting -> Booked
#   not_interested -- a clear no ("not interested", "no thanks", "remove me",
#                     "not a fit", ...) -> the terminal Not Interested stage
#                     (labeled "Failed" in the UI); stop following up, no
#                     automated reply is ever sent for this intent (see
#                     responder._REPLYABLE)
#   out_of_office  -- an autoresponder, NOT a real reply; item stays put
#   bounce         -- undeliverable; not a reply either
#   unclear        -- a genuine human reply the model could not categorize
# out_of_office and bounce are deliberately distinct from every other value:
# they are the two cases where mail arrived but a human did not actually
# respond, so treating them as a reply would corrupt the funnel.
ReplyIntent = Literal[
    "interested", "booked", "not_interested", "out_of_office", "bounce", "unclear"
]

# Intents that represent a real human reply, i.e. ones that may advance a card
# out of sent/waiting. An autoresponder or a bounce must never do that.
HUMAN_REPLY_INTENTS: list[ReplyIntent] = ["interested", "booked", "not_interested", "unclear"]

Direction = Literal["inbound", "outbound"]


class ConversationSettings(BaseModel):
    """Per-campaign ON/OFF toggle and behavior knobs for the Conversation
    Agent. `enabled` false (the default, and what an absent row means) makes
    the agent a complete no-op for that campaign."""

    campaign_id: str
    enabled: bool = False
    # Independent of `enabled`: with enabled=true and auto_reply=false the
    # agent still detects, classifies, moves cards and drafts replies -- it
    # just never sends one itself. See conversation/responder.py.
    auto_reply: bool = False
    # Silence after which a sent card moves to Waiting (No Reply). Compared
    # against exact elapsed time since sent_at, not a calendar-day boundary.
    # Bookkeeping only -- no email. config/outreach.yaml seeds new campaigns.
    followup_after_days: int = 2
    # Hard cap on nudges per prospect; the effective count is
    # min(max_followups, len(followup_days)).
    max_followups: int = 2
    # Days since the ORIGINAL send at which follow-up #1, #2, ... are due.
    followup_days: list[int] = Field(default_factory=lambda: [7, 14])
    # Day on which an unanswered card, after its last follow-up actually went
    # out, moves to the terminal Failed / No Response state.
    no_response_after_days: int = 21
    updated_at: Optional[datetime] = None


FollowupStatus = Literal["claimed", "sent", "drafted", "failed", "skipped"]


class FollowupAttempt(BaseModel):
    """Idempotency ledger row: one per (item, follow-up number), ever."""

    attempt_id: str
    item_id: str
    followup_number: int
    status: FollowupStatus = "claimed"
    attempts: int = 0
    provider_message_id: Optional[str] = None
    message_id: Optional[str] = None
    error: Optional[str] = None
    claimed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AutomationState(BaseModel):
    """Persisted scheduler state for one job (the polling cycle). Lives in the
    database so a restart resumes the schedule instead of resetting it."""

    job: str = "sync"
    interval_minutes: int = 30
    # Automatic Sync toggle. Off stops scheduled cycles only; manual
    # actions and all stored state are untouched.
    enabled: bool = True
    locked_by: Optional[str] = None
    locked_until: Optional[datetime] = None
    last_started_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_inbox_success_at: Optional[datetime] = None
    last_calendly_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    last_result: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class ConversationMessage(BaseModel):
    """One message in an item's thread, in either direction. Append-only."""

    message_id: str
    item_id: str
    direction: Direction
    provider_message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    from_email: Optional[str] = None
    to_email: Optional[str] = None
    intent: Optional[ReplyIntent] = None
    # True on an outbound row the agent composed but did not send because
    # auto_reply was off -- it is waiting for a person to approve it.
    draft_pending: bool = False
    sent_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
