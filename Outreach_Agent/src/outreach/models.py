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

Stage = Literal["needs_review", "pending", "sent", "waiting", "replied", "booked", "failed"]
Channel = Literal["email"]

# Pipeline order, mirrors Dashboard/src/agents/outreach.ts STAGE_ORDER -- keep
# the two in sync if either changes. needs_review sits before pending: an
# item lands there instead of pending only when it was drafted from the
# static template_only fallback -- i.e. there was no research data at all to
# personalize from (not even industry/description/open_reqs), or every LLM
# call failed. See personalize.draft_message, ingest.create_campaign_from_prospects,
# and api.approve_item.
STAGE_ORDER: list[Stage] = ["needs_review", "pending", "sent", "waiting", "replied", "booked", "failed"]

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
CONVERSATION_STAGES: list[Stage] = ["sent", "waiting", "replied", "booked"]

# Forward-only transitions the Conversation Agent is permitted to make. Any
# move not in this map is rejected before it reaches the database -- a
# classifier that returns nonsense can never send a card backwards, skip
# 'replied' on the way to 'booked', or resurrect a booked prospect.
CONVERSATION_TRANSITIONS: dict[Stage, list[Stage]] = {
    "sent": ["waiting", "replied", "booked"],
    "waiting": ["replied", "booked"],
    "replied": ["booked"],
    "booked": [],
}


class ImportedProspect(BaseModel):
    """Cached snapshot of a qualified Research Agent prospect."""

    research_domain: str
    research_run_id: Optional[str] = None
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
#   not_interested -- a clear no; stop following up, leave in Replied
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
    # 24 hours of silence after sending moves a card from Sent to Waiting
    # (No Reply). The comparison in followup.should_move_to_waiting is
    # against exact elapsed time, so this is a real 24h, not a calendar-day
    # boundary. See config/outreach.yaml's conversation section, the actual
    # source of truth for a brand-new campaign's seed value.
    followup_after_days: int = 1
    max_followups: int = 2
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
