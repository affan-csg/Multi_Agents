"""HTTP routes for the Feedback Learning / Optimization Agent.

Kept in its own APIRouter, mounted onto the Outreach Agent's existing app by
one `include_router` line in api.py -- same convention as
conversation/routes.py. Every route here is read-only against the learning
tables (plus the on-demand recompute that upserts learning_insights/
learning_events as a side effect of reading) -- nothing here writes
outreach_items, sends email, or changes a stage.

Removing the mount line would remove this agent from the API entirely and
leave the rest of the system exactly as it was.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from .. import db
from . import db as learning_db
from . import performance
from .engine import recompute_insights

router = APIRouter(prefix="/learning", tags=["learning"])


@router.get("/overview")
def get_overview() -> dict[str, Any]:
    conn = db.connect()
    try:
        insights = recompute_insights(conn)
        versions = learning_db.list_draft_versions(conn)
        edits = [v for v in versions if v.source in ("revise", "redraft", "redraft_generic", "conversation_revise", "conversation_redraft")]

        confidence_counts = {"low": 0, "medium": 0, "high": 0}
        active = [i for i in insights if i.status == "active"]
        for insight in active:
            confidence_counts[insight.confidence] += 1

        return {
            "total_draft_versions": len(versions),
            "active_insight_count": len(active),
            "drafts_analyzed": len(versions),
            "human_edits_analyzed": len(edits),
            "confidence_counts": confidence_counts,
        }
    finally:
        conn.close()


@router.get("/insights")
def list_insights_route(status: Optional[str] = None, category: Optional[str] = None) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        recompute_insights(conn)
        insights = learning_db.list_insights(conn, status=status, category=category)
        return [i.model_dump(mode="json") for i in insights]
    finally:
        conn.close()


@router.get("/insights/{pattern_key}")
def get_insight_detail(pattern_key: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        recompute_insights(conn)
        insight = learning_db.get_insight(conn, pattern_key)
        if not insight:
            raise HTTPException(404, f"{pattern_key} not found")
        sample_versions = []
        for item_id in insight.evidence_item_ids[:20]:
            matches = learning_db.list_draft_versions(conn, item_id=item_id, limit=1)
            sample_versions.extend(matches)
        payload = insight.model_dump(mode="json")
        payload["evidence_sample"] = [v.model_dump(mode="json") for v in sample_versions]
        return payload
    finally:
        conn.close()


@router.get("/history")
def get_history(since: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        events = learning_db.list_learning_events(conn, since=since, limit=limit)
        return [e.model_dump(mode="json") for e in events]
    finally:
        conn.close()


@router.get("/human-edits")
def list_human_edits(
    campaign_id: Optional[str] = None, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        versions = learning_db.list_draft_versions(conn, campaign_id=campaign_id, limit=limit, offset=offset)
        return [v.model_dump(mode="json") for v in versions]
    finally:
        conn.close()


@router.get("/human-edits/{version_id}")
def get_human_edit_detail(version_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        version = learning_db.get_draft_version(conn, version_id)
        if not version:
            raise HTTPException(404, f"{version_id} not found")
        return version.model_dump(mode="json")
    finally:
        conn.close()


@router.get("/performance/before-after")
def get_before_after(metric: str = "reply_rate", window: int = 30) -> dict[str, Any]:
    conn = db.connect()
    try:
        return performance.before_after_comparison(conn, metric=metric, window=window)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@router.get("/performance/learning-era")
def get_learning_era(metric: str = "reply_rate") -> dict[str, Any]:
    conn = db.connect()
    try:
        return performance.learning_era_comparison(conn, metric=metric)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@router.get("/performance/campaigns")
def get_campaign_comparison() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return performance.campaign_comparison(conn)
    finally:
        conn.close()


@router.get("/performance/followups")
def get_followup_performance() -> dict[str, Any]:
    conn = db.connect()
    try:
        return performance.followup_performance(conn)
    finally:
        conn.close()


@router.get("/recommendations")
def get_recommendations() -> list[dict[str, Any]]:
    """Active insights rendered as recommendation text -- purely
    informational, no auto-apply action (rl_prompt.md section 8: "Do not
    automatically apply high-impact changes without an appropriate safety
    mechanism")."""
    conn = db.connect()
    try:
        insights = recompute_insights(conn)
        active = [i for i in insights if i.status == "active"]
        return [
            {
                "pattern_key": i.pattern_key,
                "text": f"Consider acting on: {i.description}",
                "evidence_count": i.evidence_count,
                "confidence": i.confidence,
            }
            for i in active
        ]
    finally:
        conn.close()
