"""HTTP routes for the Calendly integration.

Kept in its own APIRouter, mounted onto the Outreach Agent's existing app by
one `include_router` line in api.py, same convention as conversation/,
learning/, optimization/.

    POST /calendly/webhook          -- Calendly calls this on invitee.created /
                                        invitee.canceled. Signature-verified
                                        before anything else runs.
    POST /calendly/sync             -- manual/cron-triggered backfill pull via
                                        the REST API, for bookings made before
                                        a webhook subscription existed or
                                        while it was down.
    POST/DELETE /calendly/events/*  -- cancel / delete / mark-no-show a
                                        specific booking (see sync.py).
    GET /calendly/event-types       -- Phase 2: view-only list of the
    GET /calendly/availability         account's meeting types and
                                        availability schedule(s) -- Calendly's
                                        API has no write endpoint for either.
    POST /calendly/scheduling-links  -- Phase 2: generate a one-off
                                        scheduling link for one event type --
                                        a real, working mutation.

GET /bookings (in api.py) is extended separately to surface calendly_events
data; nothing here changes that route directly. Every route below
event-types onward calls CalendlyClient directly with no local persistence
-- unlike calendly_events, there's no sync/webhook loop for these resources,
so every request just reflects Calendly's live state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from pydantic import BaseModel

from .. import db
from ..config import load_config
from . import sync
from .client import CalendlyClient, CalendlyClientError
from .models import AvailabilitySchedule, EventType, SchedulingLink
from .webhook import WebhookVerificationError, verify_signature


class CancelBookingRequest(BaseModel):
    event_uri: str
    reason: str | None = None


class DeleteBookingRequest(BaseModel):
    event_uri: str


class NoShowRequest(BaseModel):
    event_uri: str


class SchedulingLinkRequest(BaseModel):
    event_type_uri: str


router = APIRouter(tags=["calendly"])


def _scope_rewritten_detail(exc: CalendlyClientError, *, required_scope: str) -> str:
    """Shared with the cancel endpoint's own inline rewrite -- turns Calendly's
    raw 'Insufficient scope' JSON body into an actionable instruction naming
    the specific scope this action needs, rather than leaving the user to
    guess which of Calendly's many scopes was missing."""
    detail = str(exc)
    if "Insufficient scope" in detail or required_scope in detail:
        return (
            f"Your CALENDLY_API_TOKEN doesn't have permission for this action. "
            f"Generate a new Personal Access Token with the '{required_scope}' "
            f"scope (Calendly -> Integrations -> API & Webhooks) and update it in "
            f"the Outreach Agent's .env, then restart the backend."
        )
    return detail


@router.post("/calendly/webhook")
async def calendly_webhook(request: Request) -> dict[str, Any]:
    """Receive one Calendly webhook delivery. Verifies
    Calendly-Webhook-Signature against CALENDLY_WEBHOOK_SIGNING_KEY before
    touching the body at all -- an unverified payload is never parsed or
    written, only rejected with 401. A recognized-but-unactionable event type
    (see sync.ingest_webhook_event) still returns 200: Calendly should not
    see that as a delivery failure and start retrying/disabling the
    subscription for something this integration simply doesn't model."""
    raw_body = await request.body()
    cfg = load_config()

    try:
        verify_signature(
            raw_body, request.headers.get("Calendly-Webhook-Signature"),
            cfg.calendly_webhook_signing_key,
        )
    except WebhookVerificationError as exc:
        raise HTTPException(401, str(exc)) from exc

    payload = await request.json()
    conn = db.connect()
    try:
        event = sync.ingest_webhook_event(conn, payload)
    finally:
        conn.close()

    if event is None:
        return {"status": "ignored"}
    return {"status": "ok", "event_uri": event.event_uri, "matched": event.item_id is not None}


@router.post("/calendly/sync")
def calendly_sync(lookback_days: int = 90) -> dict[str, Any]:
    """Manually trigger a backfill pull from Calendly's API -- for bookings
    made before a webhook subscription existed, made while the app was down,
    or made through any other channel a webhook might have missed."""
    cfg = load_config()
    conn = db.connect()
    try:
        result = sync.run_sync(conn, cfg, lookback_days=lookback_days)
        return result.as_dict()
    finally:
        conn.close()


@router.post("/calendly/events/cancel")
def calendly_cancel_booking(body: CancelBookingRequest) -> dict[str, Any]:
    """Cancel a real Calendly booking (the Bookings tab's "Cancel booking"
    action). `event_uri` (Calendly's own resource URI, matched against
    calendly_events.event_uri) is passed in the body rather than the path --
    it's a full URL, which would need fragile encoding as a path segment.
    Cancels in Calendly itself and syncs the local row + matched item's
    stage in the same request -- see sync.cancel_booking."""
    cfg = load_config()
    conn = db.connect()
    try:
        event = sync.cancel_booking(conn, cfg, body.event_uri, reason=body.reason)
        return {"status": "canceled", "event_uri": event.event_uri, "item_id": event.item_id}
    except sync.CancelError as exc:
        raise HTTPException(404, str(exc)) from exc
    except CalendlyClientError as exc:
        detail = str(exc)
        if "Insufficient scope" in detail or "scheduled_events:write" in detail:
            detail = (
                "Your CALENDLY_API_TOKEN doesn't have permission to cancel events. "
                "Generate a new Personal Access Token with the 'scheduled_events:write' "
                "scope (Calendly -> Integrations -> API & Webhooks) and update it in "
                "the Outreach Agent's .env, then restart the backend."
            )
        elif "Event in the past" in detail:
            detail = "This meeting has already happened and can no longer be canceled."
        raise HTTPException(409 if "Event in the past" in str(exc) else 502, detail) from exc
    finally:
        conn.close()


@router.post("/calendly/events/delete")
def calendly_delete_booking(body: DeleteBookingRequest) -> dict[str, Any]:
    """Permanently delete a booking, any status -- the Bookings page's
    "Delete" action. If the event is still active on Calendly, cancels it
    there first (see sync.delete_booking), then removes the local row so it
    stops appearing anywhere in this dashboard. Unlike
    /calendly/events/cancel, this never leaves a row behind."""
    cfg = load_config()
    conn = db.connect()
    try:
        sync.delete_booking(conn, cfg, body.event_uri)
        return {"status": "deleted", "event_uri": body.event_uri}
    except sync.DeleteBookingError as exc:
        raise HTTPException(404, str(exc)) from exc
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="scheduled_events:write")) from exc
    finally:
        conn.close()


@router.post("/calendly/events/no-show")
def calendly_mark_no_show(body: NoShowRequest) -> dict[str, Any]:
    """Flag a booking's invitee as a no-show -- see sync.mark_no_show. Same
    error-mapping shape as the cancel endpoint: a local precondition failure
    (unknown event, still in the future, already flagged, etc.) is a 404;
    a real Calendly API failure is a 502 with a scope-aware detail rewrite."""
    cfg = load_config()
    conn = db.connect()
    try:
        event = sync.mark_no_show(conn, cfg, body.event_uri)
        return {"status": "marked", "event_uri": event.event_uri, "item_id": event.item_id}
    except sync.NoShowError as exc:
        raise HTTPException(404, str(exc)) from exc
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="invitee_no_shows:write")) from exc
    finally:
        conn.close()


@router.delete("/calendly/events/no-show")
def calendly_unmark_no_show(body: NoShowRequest) -> dict[str, Any]:
    """Undo a no-show flag -- see sync.unmark_no_show."""
    cfg = load_config()
    conn = db.connect()
    try:
        event = sync.unmark_no_show(conn, cfg, body.event_uri)
        return {"status": "unmarked", "event_uri": event.event_uri, "item_id": event.item_id}
    except sync.NoShowError as exc:
        raise HTTPException(404, str(exc)) from exc
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="invitee_no_shows:write")) from exc
    finally:
        conn.close()


@router.get("/calendly/event-types")
def calendly_list_event_types() -> list[EventType]:
    """The account's meeting types (e.g. "30 Minute Meeting") -- view-only.
    Calendly's REST API has no endpoint to activate/deactivate an event
    type (only its own web UI can), so this just reflects each type's real
    current `active` state; the Dashboard links out to `scheduling_url` for
    anyone who wants to actually change it."""
    cfg = load_config()
    client = CalendlyClient(cfg)
    try:
        event_types: list[EventType] = []
        page_token: str | None = None
        while True:
            page = client.list_event_types(page_token=page_token)
            for et in page.get("collection", []):
                event_types.append(EventType(
                    uri=et["uri"], name=et.get("name") or "", active=bool(et.get("active")),
                    duration=et.get("duration") or 0, color=et.get("color"),
                    scheduling_url=et.get("scheduling_url"),
                ))
            page_token = (page.get("pagination") or {}).get("next_page_token")
            if not page_token:
                break
        return event_types
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="event_types:read")) from exc
    finally:
        client.close()


@router.get("/calendly/availability")
def calendly_list_availability() -> list[AvailabilitySchedule]:
    """The account's availability schedule(s) -- view-only, same reason as
    event types: Calendly's API has no endpoint to edit availability."""
    cfg = load_config()
    client = CalendlyClient(cfg)
    try:
        page = client.list_availability_schedules()
        return [
            AvailabilitySchedule(
                uri=s["uri"], name=s.get("name") or "", timezone=s.get("timezone") or "",
                default=bool(s.get("default")), rules=s.get("rules") or [],
            )
            for s in page.get("collection", [])
        ]
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="availability:read")) from exc
    finally:
        client.close()


@router.post("/calendly/scheduling-links")
def calendly_create_scheduling_link(body: SchedulingLinkRequest) -> SchedulingLink:
    """Generate a one-off (single-use) scheduling link scoped to one event
    type -- a real, working Calendly mutation, unlike activate/deactivate or
    editing availability."""
    cfg = load_config()
    client = CalendlyClient(cfg)
    try:
        resource = client.create_scheduling_link(body.event_type_uri)
        return SchedulingLink(
            booking_url=resource["booking_url"], owner=resource.get("owner") or body.event_type_uri,
            owner_type=resource.get("owner_type") or "EventType",
        )
    except CalendlyClientError as exc:
        raise HTTPException(502, _scope_rewritten_detail(exc, required_scope="scheduling_links:write")) from exc
    finally:
        client.close()


@router.get("/calendly/status")
def calendly_status() -> dict[str, Any]:
    """Whether the API token / webhook signing key are configured -- lets the
    Dashboard show a real "not connected" state instead of guessing from an
    empty events list."""
    cfg = load_config()
    return {
        "api_configured": bool(cfg.calendly_api_token),
        "webhook_configured": bool(cfg.calendly_webhook_signing_key),
    }
