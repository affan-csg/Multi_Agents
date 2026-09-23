"""Repository functions for the Campaign-Level Optimization layer's own two
tables, plus read-only lookups over the existing campaigns / prospects /
outreach_items / conversation_messages tables and the Learning Agent's own
draft_versions (read-only, for style evidence). Same SupabaseConn/PostgREST
conventions as ../db.py and ../learning/db.py -- no ORM, no business logic
beyond simple filters.

This module never writes campaigns, prospects, outreach_items,
outreach_events, conversation_messages, draft_versions, learning_insights, or
learning_events -- only reads them, to build StrategyProfiles and compute
outcome rates. It only writes campaign_optimization_insights and
optimization_recommendation_adoptions.
"""

from __future__ import annotations

from typing import Any, Optional

from ..db import SupabaseConn, utcnow
from .models import CampaignOptimizationInsight, RecommendationAdoption, StrategyProfile
from .strategy import compute_strategy

_POSITIVE_INTENTS = {"interested", "booked"}


def _iso(dt: Optional[Any]) -> Optional[str]:
    return dt.isoformat() if dt else None


# --- read-only lookups over existing tables ---------------------------------

def list_sent_items_with_context(
    conn: SupabaseConn, *, campaign_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Every sent outreach_item joined (in Python, not SQL -- same style as
    ../learning/performance.py) with its campaign and prospect snapshot.
    Returns raw dicts, not models, since callers need varying subsets of
    fields (stage, tier, industry, subject, body, personalized_from,
    followup_count)."""
    items = [i for i in conn.select("outreach_items") if i.get("sent_at")]
    if campaign_ids:
        wanted = set(campaign_ids)
        items = [i for i in items if i.get("campaign_id") in wanted]
    if not items:
        return []

    campaign_rows = {c["campaign_id"]: c for c in conn.select("campaigns")}
    prospect_rows = {p["research_domain"]: p for p in conn.select("prospects")}

    out = []
    for item in items:
        campaign = campaign_rows.get(item.get("campaign_id"), {})
        prospect = prospect_rows.get(item.get("research_domain"), {})
        out.append({**item, "_campaign": campaign, "_prospect": prospect})
    return out


def outcome_summary(conn: SupabaseConn, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read-only join over outreach_items/conversation_messages, identical
    semantics to ../learning/db.py's outreach_outcome_summary (duplicated
    rather than imported to keep this package independently
    failure-isolated from the Learning Agent's package -- see __init__.py
    invariant 1)."""
    if not item_ids:
        return {}
    item_rows = conn.select("outreach_items", item_id=f"in.({','.join(item_ids)})")
    summary: dict[str, dict[str, Any]] = {}
    for row in item_rows:
        summary[row["item_id"]] = {
            "stage": row.get("stage"),
            "replied": False,
            "positive_reply": False,
            "booked": row.get("stage") == "booked",
            # A clear rejection -- the terminal not_interested stage
            # (labeled "Failed" in the UI). Distinct from `replied`, which
            # stays true for a rejection too (see below): this is the
            # negative-outcome signal on its own, for campaign analysis.
            "failed": row.get("stage") == "not_interested",
        }
    msg_rows = conn.select(
        "conversation_messages", item_id=f"in.({','.join(item_ids)})", direction="eq.inbound",
    )
    for row in msg_rows:
        item_id = row.get("item_id")
        if item_id not in summary:
            continue
        intent = row.get("intent")
        if intent in ("interested", "booked", "not_interested", "unclear"):
            summary[item_id]["replied"] = True
        if intent in _POSITIVE_INTENTS:
            summary[item_id]["positive_reply"] = True
    return summary


def build_strategy_profiles(conn: SupabaseConn, *, campaign_ids: Optional[list[str]] = None) -> list[StrategyProfile]:
    """One StrategyProfile per sent item, computed fresh from the current
    outreach_items/prospects snapshot every call -- never cached, so an
    edited-then-resent draft is always classified from its current text."""
    rows = list_sent_items_with_context(conn, campaign_ids=campaign_ids)
    return [
        compute_strategy(
            row["item_id"], row["campaign_id"],
            subject=row.get("subject"), body=row.get("body"),
            personalized_from=row.get("personalized_from"), prospect=row.get("_prospect"),
        )
        for row in rows
    ]


# --- campaign_optimization_insights ------------------------------------------

def upsert_insight(conn: SupabaseConn, insight: CampaignOptimizationInsight) -> None:
    row = insight.model_dump(mode="json")
    now = _iso(utcnow())
    row["first_observed_at"] = row["first_observed_at"] or now
    row["last_updated_at"] = row["last_updated_at"] or now
    conn.upsert("campaign_optimization_insights", [row], on_conflict="pattern_key")


def get_insight(conn: SupabaseConn, pattern_key: str) -> Optional[CampaignOptimizationInsight]:
    rows = conn.select("campaign_optimization_insights", pattern_key=f"eq.{pattern_key}", limit=1)
    return CampaignOptimizationInsight(**rows[0]) if rows else None


def list_insights(
    conn: SupabaseConn, *, status: Optional[str] = None, dimension: Optional[str] = None,
    segment: Optional[str] = None,
) -> list[CampaignOptimizationInsight]:
    params: dict[str, Any] = {"order": "evidence_count.desc"}
    if status:
        params["status"] = f"eq.{status}"
    if dimension:
        params["dimension"] = f"eq.{dimension}"
    if segment is not None:
        params["segment"] = f"eq.{segment}"
    rows = conn.select("campaign_optimization_insights", **params)
    return [CampaignOptimizationInsight(**r) for r in rows]


# --- optimization_recommendation_adoptions -----------------------------------

def record_adoption(conn: SupabaseConn, adoption: RecommendationAdoption) -> None:
    row = adoption.model_dump(mode="json")
    row["created_at"] = _iso(adoption.created_at or utcnow())
    conn.insert("optimization_recommendation_adoptions", [row])


def list_adoptions(
    conn: SupabaseConn, *, pattern_key: Optional[str] = None, campaign_id: Optional[str] = None,
) -> list[RecommendationAdoption]:
    params: dict[str, Any] = {"order": "created_at.asc"}
    if pattern_key:
        params["pattern_key"] = f"eq.{pattern_key}"
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    rows = conn.select("optimization_recommendation_adoptions", **params)
    return [RecommendationAdoption(**r) for r in rows]
