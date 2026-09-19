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

    This agent only ever writes pending -> sent (or failed on a send error).
    waiting/replied/booked exist here so the pipeline board can render and a
    person (or a future reply-tracking agent) can advance them -- nothing in
    this codebase sets them automatically.
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


class OutreachEvent(BaseModel):
    event_id: str
    item_id: str
    event_type: str
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    actor: str = "system"
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
