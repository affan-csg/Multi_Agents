"""Repository functions for the calendly_events table -- the raw synced/
webhook-received Calendly booking record, one row per (event_uri, invitee_uri)
pair (an event can have multiple invitees; this integration only tracks
one-on-one meetings, the same assumption the classifier-only path already
makes, so in practice this is one row per event).

Same pattern as db.py's own repository functions: thin wrappers around
SupabaseConn, no business logic -- matching/stage-advancing lives in sync.py.
"""

from __future__ import annotations

from typing import Any, Optional

from ..db import SupabaseConn, _iso, utcnow
from .models import CalendlyEvent


def upsert_event(conn: SupabaseConn, e: CalendlyEvent) -> CalendlyEvent:
    """Insert or update by event_uri -- the idempotency key. A `sync` re-run
    or a webhook re-delivery of the same event must update the one row in
    place (e.g. status flipping active -> canceled), never create a
    duplicate."""
    row = e.model_dump(mode="json")
    row["created_at"] = _iso(e.created_at or utcnow())
    row["updated_at"] = _iso(utcnow())
    out = conn.upsert("calendly_events", [row], on_conflict="event_uri")
    return CalendlyEvent(**out[0]) if out else e


def get_event(conn: SupabaseConn, event_uri: str) -> Optional[CalendlyEvent]:
    rows = conn.select("calendly_events", event_uri=f"eq.{event_uri}", limit=1)
    return CalendlyEvent(**rows[0]) if rows else None


def list_events(conn: SupabaseConn, *, status: Optional[str] = None) -> list[CalendlyEvent]:
    params: dict[str, Any] = {"order": "starts_at.desc"}
    if status:
        params["status"] = f"eq.{status}"
    rows = conn.select("calendly_events", **params)
    return [CalendlyEvent(**r) for r in rows]


def list_events_by_item(conn: SupabaseConn, item_id: str) -> list[CalendlyEvent]:
    rows = conn.select("calendly_events", item_id=f"eq.{item_id}", order="starts_at.desc")
    return [CalendlyEvent(**r) for r in rows]


def delete_event(conn: SupabaseConn, event_uri: str) -> None:
    """Permanently remove this booking's local row -- see sync.delete_booking.
    Does not touch the matched outreach item, same "unlink, don't cascade"
    choice as the table's item_id ON DELETE SET NULL: deleting a booking
    record is not the same as deleting the pipeline item it was matched to."""
    conn.delete("calendly_events", event_uri=f"eq.{event_uri}")


def is_deleted(conn: SupabaseConn, event_uri: str) -> bool:
    """True if the user explicitly deleted this booking (a tombstone exists)."""
    return bool(conn.select("calendly_deleted_events", event_uri=f"eq.{event_uri}", limit=1))


def mark_deleted(conn: SupabaseConn, event_uri: str) -> None:
    """Record a tombstone so sync/webhooks never recreate this event."""
    conn.upsert(
        "calendly_deleted_events",
        [{"event_uri": event_uri, "deleted_at": _iso(utcnow())}],
        on_conflict="event_uri",
    )
