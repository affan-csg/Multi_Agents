"""Repository functions for the Feedback Learning Agent's own three tables,
plus read-only outcome lookups over the existing outreach_items /
conversation_messages tables. Same SupabaseConn/PostgREST conventions as
../db.py -- no ORM, no business logic beyond simple filters.

This module never writes outreach_items, outreach_events, or
conversation_messages -- only reads them, to compute the denormalized
outcome_* cache columns on draft_versions and to power performance.py's
metrics.
"""

from __future__ import annotations

from typing import Any, Optional

from ..db import SupabaseConn, utcnow
from . import diffing
from .models import DraftVersion, LearningEvent, LearningInsight


def _iso(dt: Optional[Any]) -> Optional[str]:
    return dt.isoformat() if dt else None

_POSITIVE_INTENTS = {"interested", "booked"}


# --- draft_versions ----------------------------------------------------------

def insert_draft_version(conn: SupabaseConn, dv: DraftVersion) -> None:
    row = dv.model_dump(mode="json")
    row["created_at"] = _iso(dv.created_at or utcnow())
    conn.insert("draft_versions", [row])


def record_draft_edit(
    conn: SupabaseConn,
    *,
    item_id: str,
    campaign_id: str,
    source: str,
    before_subject: Optional[str],
    before_body: Optional[str],
    after_subject: Optional[str],
    after_body: Optional[str],
    instruction: Optional[str] = None,
    message_id: Optional[str] = None,
    prospect: Optional[dict[str, Any]] = None,
) -> None:
    """Composes diffing.compute_diff + insert_draft_version -- the single
    call every revise/redraft hook point makes. Callers are expected to wrap
    this in try/except (see api.py and conversation/routes.py): a failure
    here must never block the actual revise/redraft response, per
    rl_prompt.md section 12."""
    flags = diffing.compute_diff(before_subject, before_body, after_subject, after_body, prospect)
    version_id = f"{item_id}-{int(utcnow().timestamp() * 1000)}"
    insert_draft_version(conn, DraftVersion(
        version_id=version_id, item_id=item_id, campaign_id=campaign_id, source=source,
        message_id=message_id, before_subject=before_subject, before_body=before_body,
        after_subject=after_subject, after_body=after_body, instruction=instruction,
        **flags,
    ))


def get_draft_version(conn: SupabaseConn, version_id: str) -> Optional[DraftVersion]:
    rows = conn.select("draft_versions", version_id=f"eq.{version_id}", limit=1)
    return DraftVersion(**rows[0]) if rows else None


def list_draft_versions(
    conn: SupabaseConn,
    *,
    campaign_id: Optional[str] = None,
    item_id: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> list[DraftVersion]:
    params: dict[str, Any] = {"order": "created_at.desc"}
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    if item_id:
        params["item_id"] = f"eq.{item_id}"
    if source:
        params["source"] = f"eq.{source}"
    rows = conn.select("draft_versions", **params)
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return [DraftVersion(**r) for r in rows]


def outreach_outcome_summary(conn: SupabaseConn, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read-only join over the EXISTING outreach_items/conversation_messages
    tables -- the canonical source of truth for outcomes. Never writes
    either. Returns {item_id: {"stage", "replied", "positive_reply",
    "booked"}} for each id in `item_ids` that exists."""
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


def refresh_draft_version_outcomes(conn: SupabaseConn, *, campaign_id: Optional[str] = None) -> int:
    """Backfill the denormalized outcome_* columns on draft_versions from
    the current outreach_items/conversation_messages state. Returns how many
    rows were refreshed. Idempotent -- safe to call on every insight
    recompute."""
    versions = list_draft_versions(conn, campaign_id=campaign_id)
    if not versions:
        return 0
    item_ids = sorted({v.item_id for v in versions})
    outcomes = outreach_outcome_summary(conn, item_ids)
    updated = 0
    for v in versions:
        outcome = outcomes.get(v.item_id)
        if outcome is None:
            continue
        conn.update(
            "draft_versions",
            {
                "outcome_stage": outcome["stage"],
                "outcome_replied": outcome["replied"],
                "outcome_positive_reply": outcome["positive_reply"],
                "outcome_booked": outcome["booked"],
            },
            version_id=f"eq.{v.version_id}",
        )
        updated += 1
    return updated


# --- learning_insights ---------------------------------------------------------

def upsert_insight(conn: SupabaseConn, insight: LearningInsight) -> None:
    row = insight.model_dump(mode="json")
    now = _iso(utcnow())
    row["first_observed_at"] = row["first_observed_at"] or now
    row["last_updated_at"] = row["last_updated_at"] or now
    conn.upsert("learning_insights", [row], on_conflict="pattern_key")


def get_insight(conn: SupabaseConn, pattern_key: str) -> Optional[LearningInsight]:
    rows = conn.select("learning_insights", pattern_key=f"eq.{pattern_key}", limit=1)
    return LearningInsight(**rows[0]) if rows else None


def list_insights(
    conn: SupabaseConn, *, status: Optional[str] = None, category: Optional[str] = None,
) -> list[LearningInsight]:
    params: dict[str, Any] = {"order": "evidence_count.desc"}
    if status:
        params["status"] = f"eq.{status}"
    if category:
        params["category"] = f"eq.{category}"
    rows = conn.select("learning_insights", **params)
    return [LearningInsight(**r) for r in rows]


# --- learning_events -----------------------------------------------------------

def add_learning_event(conn: SupabaseConn, e: LearningEvent) -> None:
    row = e.model_dump(mode="json")
    row["created_at"] = _iso(e.created_at or utcnow())
    conn.insert("learning_events", [row])


def list_learning_events(
    conn: SupabaseConn, *, since: Optional[str] = None, limit: int = 100,
) -> list[LearningEvent]:
    params: dict[str, Any] = {"order": "created_at.desc", "limit": limit}
    if since:
        params["created_at"] = f"gt.{since}"
    rows = conn.select("learning_events", **params)
    return [LearningEvent(**r) for r in rows]
