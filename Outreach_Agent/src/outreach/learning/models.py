"""Domain models for the Feedback Learning / Optimization Agent.

Mirrors the shape/conventions of ../models.py -- plain Pydantic models over
the three additive tables in migrations/005_learning_agent.sql.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

DraftSource = Literal[
    "revise", "redraft", "redraft_generic", "conversation_revise", "conversation_redraft",
]

Confidence = Literal["low", "medium", "high"]
InsightStatus = Literal["active", "insufficient_data", "retired"]
InsightCategory = Literal[
    "cta", "length", "personalization", "opening", "subject", "followup", "human_edit_pattern",
]


class DraftVersion(BaseModel):
    """One snapshot of an AI-draft-mutating action that had a real "before"
    to diff against -- see the revise/redraft hooks in api.py and
    conversation/routes.py. Never written for the very first draft at
    campaign-creation time (no prior version exists yet)."""

    version_id: str
    item_id: str
    campaign_id: str
    source: DraftSource
    message_id: Optional[str] = None
    before_subject: Optional[str] = None
    before_body: Optional[str] = None
    after_subject: Optional[str] = None
    after_body: Optional[str] = None
    instruction: Optional[str] = None

    # --- precomputed structured diff flags, see diffing.compute_diff -------
    diff_version: int = 1
    subject_changed: bool = False
    opening_changed: bool = False
    cta_changed: bool = False
    cta_shortened: bool = False
    length_delta_chars: Optional[int] = None
    length_delta_words: Optional[int] = None
    shortened: bool = False
    lengthened: bool = False
    sentences_removed_count: int = 0
    personalization_delta: int = 0
    formatting_changed: bool = False

    # --- denormalized outcome cache, refreshed on each insight recompute ---
    outcome_stage: Optional[str] = None
    outcome_replied: Optional[bool] = None
    outcome_positive_reply: Optional[bool] = None
    outcome_booked: Optional[bool] = None

    created_at: Optional[datetime] = None


class LearningInsight(BaseModel):
    """Current state of one detected pattern. One row per pattern_key,
    upserted on recompute -- the history of how it changed over time lives in
    LearningEvent, not here."""

    pattern_key: str
    pattern_label: str
    category: InsightCategory
    description: str
    evidence_count: int = 0
    metric_comparison: dict[str, Any] = Field(default_factory=dict)
    confidence: Confidence = "low"
    status: InsightStatus = "insufficient_data"
    evidence_campaign_ids: list[str] = Field(default_factory=list)
    evidence_item_ids: list[str] = Field(default_factory=list)
    first_observed_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None


class LearningEvent(BaseModel):
    """One append-only timeline entry, purely for the dashboard's Learning
    History section -- not a source of truth for current state."""

    event_id: str
    pattern_key: str
    event_type: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
