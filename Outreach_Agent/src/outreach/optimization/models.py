"""Domain models for the Campaign-Level Optimization layer.

Mirrors the shape/conventions of ../learning/models.py -- plain Pydantic
models over the two additive tables in migrations/006_campaign_optimization.sql.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]
InsightStatus = Literal["active", "insufficient_data", "retired"]

# Every dimension the optimization engine can ever compare -- fixed and
# enumerable, same "no auto-discovered free text" discipline as
# ../learning/engine.py's PATTERN_DETECTORS catalog.
OptimizationDimension = Literal[
    "email_style", "subject_style", "opening_style", "cta_style",
    "length_bucket", "personalization_level", "followup_count", "followup_timing",
]

# Rule-based, auto-derived per-item strategy classification -- see
# strategy.py. Every value here is an OBSERVABLE measurement of the stored
# subject/body/personalized_from, never an inferred psychological judgment,
# matching ../learning/diffing.py's design principle.
LengthBucket = Literal["short", "medium", "long"]
SubjectStyle = Literal["question", "personalized", "statement"]
OpeningStyle = Literal["question", "personalized_fact", "generic"]
CtaStyle = Literal["direct", "soft", "none"]
PersonalizationLevel = Literal["none", "light", "deep"]
EmailStyle = Literal["research_backed", "template_only"]


class StrategyProfile(BaseModel):
    """One item's auto-derived strategy attributes, computed on-demand from
    its stored subject/body/personalized_from -- never stored as its own
    row; always recomputed from the current outreach_items snapshot so it
    can never drift out of sync with an edited draft."""

    item_id: str
    campaign_id: str
    length_bucket: LengthBucket
    subject_style: SubjectStyle
    opening_style: OpeningStyle
    cta_style: CtaStyle
    personalization_level: PersonalizationLevel
    email_style: EmailStyle
    word_count: int


class CampaignOptimizationInsight(BaseModel):
    """Current state of one detected campaign/strategy-level pattern. One row
    per pattern_key, upserted on recompute -- same shape/spirit as
    ../learning/models.py's LearningInsight, scoped to campaign-level
    dimensions instead of single-edit diffs."""

    pattern_key: str
    pattern_label: str
    dimension: OptimizationDimension
    segment: str = ""  # "" = computed across all campaigns; else e.g. "tier=A"
    description: str
    evidence_count: int = 0
    metric_comparison: dict[str, Any] = Field(default_factory=dict)
    confidence: Confidence = "low"
    status: InsightStatus = "insufficient_data"
    evidence_campaign_ids: list[str] = Field(default_factory=list)
    evidence_item_ids: list[str] = Field(default_factory=list)
    first_observed_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None


class RecommendationAdoption(BaseModel):
    """One human-recorded "this campaign was built following pattern_key"
    marker -- always an explicit, opt-in action (see routes.py), never
    inferred or auto-tagged by this package."""

    adoption_id: str
    pattern_key: str
    campaign_id: str
    note: Optional[str] = None
    created_at: Optional[datetime] = None
