"""HTTP routes for the Campaign-Level Optimization layer.

Kept in its own APIRouter, mounted onto the Outreach Agent's existing app by
one `include_router` line in api.py -- same convention as
../learning/routes.py. Every route here is read-only against the
optimization tables (plus the on-demand recompute that upserts
campaign_optimization_insights as a side effect of reading), with exactly
one write endpoint: recording a human's explicit recommendation adoption.
Nothing here writes campaigns, outreach_items, sends email, or changes a
stage.

Removing the mount line would remove this layer from the API entirely and
leave the rest of the system -- including the Feedback Learning Agent --
exactly as it was.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from . import db as opt_db
from . import tracking
from .engine import recompute_insights

router = APIRouter(prefix="/optimization", tags=["optimization"])


@router.get("/overview")
def get_overview() -> dict[str, Any]:
    conn = db.connect()
    try:
        insights = recompute_insights(conn)
        active = [i for i in insights if i.status == "active"]
        segmented = [i for i in active if i.segment]
        confidence_counts = {"low": 0, "medium": 0, "high": 0}
        for insight in active:
            confidence_counts[insight.confidence] += 1
        return {
            "total_insight_count": len(insights),
            "active_insight_count": len(active),
            "segmented_insight_count": len(segmented),
            "confidence_counts": confidence_counts,
        }
    finally:
        conn.close()


@router.get("/insights")
def list_insights_route(
    status: Optional[str] = None, dimension: Optional[str] = None, segment: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        recompute_insights(conn)
        insights = opt_db.list_insights(conn, status=status, dimension=dimension, segment=segment)
        return [i.model_dump(mode="json") for i in insights]
    finally:
        conn.close()


@router.get("/insights/{pattern_key}")
def get_insight_detail(pattern_key: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        recompute_insights(conn)
        insight = opt_db.get_insight(conn, pattern_key)
        if not insight:
            raise HTTPException(404, f"{pattern_key} not found")
        return insight.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/recommendations")
def get_recommendations(segment: Optional[str] = None) -> list[dict[str, Any]]:
    """Active insights rendered as recommendation text -- purely
    informational, no auto-apply action. When no active insight (segmented
    or global) exists yet, returns an empty list rather than a fabricated
    recommendation -- the dashboard is expected to render this as "Not
    enough data yet." (see __init__.py invariant 3)."""
    conn = db.connect()
    try:
        insights = recompute_insights(conn)
        active = [i for i in insights if i.status == "active"]
        if segment is not None:
            active = [i for i in active if i.segment == segment]
        return [
            {
                "pattern_key": i.pattern_key,
                "text": f"Consider acting on: {i.description}",
                "dimension": i.dimension,
                "segment": i.segment,
                "evidence_count": i.evidence_count,
                "confidence": i.confidence,
                "metric_comparison": i.metric_comparison,
                "evidence_campaign_ids": i.evidence_campaign_ids,
                "causality_note": "This is an observed correlation across past campaigns, not a proven cause of better performance.",
            }
            for i in active
        ]
    finally:
        conn.close()


class AdoptionRequest(BaseModel):
    pattern_key: str
    campaign_id: str
    note: Optional[str] = None


@router.post("/adoptions")
def create_adoption(req: AdoptionRequest) -> dict[str, Any]:
    """Records that a person is building/optimizing `campaign_id` following
    `pattern_key`. Purely a human-initiated marker -- this endpoint never
    changes the campaign itself, and no other part of this system calls it
    automatically. See tracking.adoption_outcome for how this is later used
    to report (never to prove) observed performance."""
    from .models import RecommendationAdoption

    conn = db.connect()
    try:
        campaign = conn.select("campaigns", campaign_id=f"eq.{req.campaign_id}", limit=1)
        if not campaign:
            raise HTTPException(404, f"campaign {req.campaign_id} not found")
        adoption = RecommendationAdoption(
            adoption_id=str(uuid.uuid4()), pattern_key=req.pattern_key,
            campaign_id=req.campaign_id, note=req.note,
        )
        opt_db.record_adoption(conn, adoption)
        return adoption.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/adoptions")
def list_adoptions_route(pattern_key: Optional[str] = None, campaign_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        adoptions = opt_db.list_adoptions(conn, pattern_key=pattern_key, campaign_id=campaign_id)
        return [a.model_dump(mode="json") for a in adoptions]
    finally:
        conn.close()


@router.get("/adoptions/{pattern_key}/outcome")
def get_adoption_outcome(pattern_key: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        return tracking.adoption_outcome(conn, pattern_key)
    finally:
        conn.close()
