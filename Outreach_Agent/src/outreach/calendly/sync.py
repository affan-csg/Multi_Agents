"""Turn raw Calendly data (a REST page or a webhook payload) into
calendly_events rows, match each to an outreach item by email, and keep the
matched item's pipeline stage in sync with the booking's real status.

Two entry points:
  - run_sync(conn, cfg)         -- pulls every scheduled event via the API
                                    (paginated), for backfilling bookings made
                                    outside the agent's own flow.
  - ingest_webhook_event(...)   -- normalizes and applies one webhook payload,
                                    already signature-verified by the caller.

Both funnel through the same apply_event() so a booking behaves identically
regardless of which source reported it, and both are idempotent: re-syncing
or re-delivering the same event updates the one row in place (see
db.upsert_event's on_conflict=event_uri) rather than duplicating it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from .. import db as core_db
from ..config import Config
from ..db import SupabaseConn
from ..models import CONVERSATION_STAGES, OutreachItem
from .client import CalendlyClient, CalendlyClientError
from .models import CalendlyEvent

# Distinct from conversation_agent/system/user (see conversation/engine.py's
# ACTOR) so outreach_events can always answer whether a booking's stage move
# came from real Calendly data or from the classifier's email heuristic.
ACTOR = "calendly"


class SyncResult:
    def __init__(self) -> None:
        self.fetched = 0
        self.created = 0
        self.updated = 0
        self.unchanged = 0
        self.ignored = 0
        self.matched = 0
        self.unmatched = 0
        self.advanced_to_booked = 0
        self.reverted_from_booked = 0
        self.errors: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "ignored": self.ignored,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "advanced_to_booked": self.advanced_to_booked,
            "reverted_from_booked": self.reverted_from_booked,
            "errors": self.errors,
        }


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def normalize_scheduled_event(event: dict[str, Any], invitee: dict[str, Any]) -> CalendlyEvent:
    """Build a CalendlyEvent from one /scheduled_events resource + one of its
    /invitees resources (the REST shape) -- see webhook.parse_webhook_payload
    for the equivalent adapter over a webhook body."""
    event_uri = event["uri"]
    event_uuid = event_uri.rstrip("/").rsplit("/", 1)[-1]
    location = event.get("location") or {}
    return CalendlyEvent(
        event_uri=event_uri,
        event_uuid=event_uuid,
        invitee_uri=invitee.get("uri"),
        invitee_email=invitee.get("email"),
        invitee_name=invitee.get("name"),
        status="canceled" if invitee.get("status") == "canceled" else "active",
        event_type_name=event.get("name"),
        starts_at=_parse_dt(event.get("start_time")),
        ends_at=_parse_dt(event.get("end_time")),
        location=location.get("join_url") or location.get("location") or None,
        cancel_reason=(invitee.get("cancellation") or {}).get("reason"),
        source="sync",
        raw={"event": event, "invitee": invitee},
    )


def normalize_webhook_event(fields: dict[str, Any]) -> CalendlyEvent:
    """Build a CalendlyEvent from webhook.parse_webhook_payload's output."""
    event_uri = fields["event_uri"]
    event_uuid = event_uri.rstrip("/").rsplit("/", 1)[-1] if event_uri else ""
    return CalendlyEvent(
        event_uri=event_uri,
        event_uuid=event_uuid,
        invitee_uri=fields.get("invitee_uri"),
        invitee_email=fields.get("invitee_email"),
        invitee_name=fields.get("invitee_name"),
        status=fields.get("status") or "active",
        event_type_name=fields.get("event_type_name"),
        starts_at=_parse_dt(fields.get("starts_at")),
        ends_at=_parse_dt(fields.get("ends_at")),
        location=fields.get("location"),
        cancel_reason=fields.get("cancel_reason"),
        source="webhook",
        raw=fields,
    )


def match_item(conn: SupabaseConn, invitee_email: Optional[str]) -> Optional[OutreachItem]:
    """Find the outreach item this booking belongs to, by invitee email
    against outreach_items.to_email -- the same address the outreach was
    actually sent to, which is a stronger match than the cached prospect's
    contact_email (that can drift after import; to_email is what was really
    used). Same "most recently sent wins" tiebreak as
    conversation/matcher.py's address-based strategies, for a person who
    appears in more than one campaign. Returns None (booking still recorded,
    just unmatched) rather than guessing when no item's to_email matches at
    all -- an unmatched booking must still appear on the Bookings page per
    the requirement, not be discarded."""
    if not invitee_email:
        return None
    email = invitee_email.strip().lower()
    candidates = [
        item for item in core_db.list_items(conn)
        if (item.to_email or "").strip().lower() == email
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda i: (i.sent_at is not None, i.sent_at or i.created_at))


def _unchanged(existing: CalendlyEvent, incoming: CalendlyEvent) -> bool:
    """Whether a re-fetched event carries nothing new -- the incremental-sync
    short-circuit. Compares only what Calendly can change after the fact."""
    def norm(dt: Optional[datetime]) -> Optional[datetime]:
        return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt

    return (
        existing.status == incoming.status
        and norm(existing.starts_at) == norm(incoming.starts_at)
        and norm(existing.ends_at) == norm(incoming.ends_at)
        and existing.cancel_reason == incoming.cancel_reason
        and (existing.invitee_email or "").lower() == (incoming.invitee_email or "").lower()
        and existing.item_id is not None  # an unmatched row is re-tried: an item may exist now
    )


def apply_event(
    conn: SupabaseConn, raw_event: CalendlyEvent, result: SyncResult,
    *, move_stages: Optional[Callable[[OutreachItem], bool]] = None,
    skip_unchanged: bool = False,
) -> CalendlyEvent:
    """Store/update one normalized event, match it to an item, and keep that
    item's pipeline stage in sync -- the single path both run_sync and
    ingest_webhook_event funnel through, so a booking behaves identically
    regardless of source."""
    from . import db as calendly_db
    # A booking the user explicitly deleted is never stored again, whatever
    # Calendly still reports for it (sync lists canceled events too).
    if calendly_db.is_deleted(conn, raw_event.event_uri):
        result.ignored += 1
        return raw_event

    existing = None
    try:
        existing = calendly_db.get_event(conn, raw_event.event_uri)
    except Exception:
        pass

    if skip_unchanged and existing is not None and _unchanged(existing, raw_event):
        # Unchanged data is only skippable if the card already reflects it: a
        # booking recorded while the agent was OFF must still move the card
        # once the agent is switched on.
        linked = core_db.get_item(conn, existing.item_id) if existing.item_id else None
        needs_move = linked is not None and (move_stages is None or move_stages(linked)) and (
            (existing.status == "active" and linked.stage != "booked"
             and linked.stage in CONVERSATION_STAGES)
            or (existing.status == "canceled" and linked.stage == "booked")
        )
        if not needs_move:
            result.unchanged += 1
            return existing

    item = match_item(conn, raw_event.invitee_email)
    stored = raw_event.model_copy(update={
        "item_id": item.item_id if item else None,
        "research_domain": item.research_domain if item else None,
        "created_at": existing.created_at if existing else None,
        # A re-sync/re-delivery normalizes a fresh CalendlyEvent from
        # Calendly's event+invitee payload, which knows nothing about a
        # no-show flag this integration set independently (mark_no_show
        # isn't reported back by invitee.created/invitee.canceled) -- carry
        # the existing local flag forward so re-syncing never silently
        # un-flags a no-show.
        "no_show": existing.no_show if existing else False,
        "no_show_uri": existing.no_show_uri if existing else None,
    })

    from . import db as calendly_db
    stored = calendly_db.upsert_event(conn, stored)

    if existing is None:
        result.created += 1
    else:
        result.updated += 1

    if item:
        result.matched += 1
        # The booking row above is always recorded. Moving the card is the
        # part the scheduled run gates on the agent being ON for the campaign.
        if move_stages is None or move_stages(item):
            _sync_item_stage(conn, item, stored, result)
    else:
        result.unmatched += 1

    return stored


def _sync_item_stage(conn: SupabaseConn, item: OutreachItem, event: CalendlyEvent, result: SyncResult) -> None:
    """Move a matched item's stage to reflect a confirmed Calendly booking,
    or back off `booked` on a cancellation -- mirrors, but is independent of,
    conversation/engine.py's CONVERSATION_TRANSITIONS-gated moves (this is a
    real calendar fact, not a classified reply, so it is allowed to both
    advance to `booked` from any live conversation stage AND revert off it,
    which the Conversation Agent's own forward-only whitelist deliberately
    never permits for itself -- see models.CONVERSATION_TRANSITIONS's
    docstring on booked/not_interested being terminal for THAT agent)."""
    from ..models import CONVERSATION_STAGES

    if event.status == "active":
        if item.stage != "booked" and item.stage in CONVERSATION_STAGES:
            core_db.set_stage(
                conn, item.item_id, "booked", actor=ACTOR,
                detail={
                    "reason": "Calendly booking confirmed",
                    "event_uri": event.event_uri,
                    "invitee_email": event.invitee_email,
                    "starts_at": event.starts_at.isoformat() if event.starts_at else None,
                },
            )
            result.advanced_to_booked += 1
    elif event.status == "canceled":
        if item.stage == "booked":
            # Revert to `waiting` (not back to `sent`/`replied`) -- a
            # cancellation means the meeting fell through, not that the
            # conversation never happened; `waiting` is the pipeline's
            # existing "nothing currently pending, but the thread is live"
            # state, and follow-up logic already knows how to act on it.
            core_db.set_stage(
                conn, item.item_id, "waiting", actor=ACTOR,
                detail={
                    "reason": "Calendly booking canceled",
                    "event_uri": event.event_uri,
                    "cancel_reason": event.cancel_reason,
                },
            )
            result.reverted_from_booked += 1


class CancelError(RuntimeError):
    pass


def cancel_booking(conn: SupabaseConn, cfg: Config, event_uri: str, *, reason: Optional[str] = None) -> CalendlyEvent:
    """Cancel a real Calendly booking -- the Bookings tab's "Cancel booking"
    action. Cancels the event in Calendly itself (not just a local row delete, which
    would desync from Calendly and silently reappear on the next sync/
    webhook), then applies the same status-flip + stage-revert path a real
    invitee.canceled webhook would take, via apply_event, so this and an
    organic cancellation behave identically and are equally idempotent.

    Raises CancelError if the event is unknown locally, already canceled
    (nothing to do), or already in the past -- Calendly itself refuses to
    cancel a past event ("Permission Denied: Event in the past"), so this is
    checked up front rather than surfaced as an opaque 502 from a call that
    was never going to succeed. Raises CalendlyClientError if the Calendly
    API call itself fails for any other reason (network/4xx/5xx) -- the
    local row is left untouched in that case, so a failed cancel never lies
    about the booking's real status."""
    from . import db as calendly_db

    event = calendly_db.get_event(conn, event_uri)
    if event is None:
        raise CancelError(f"No Calendly booking found for {event_uri}")
    if event.status == "canceled":
        raise CancelError("This booking is already canceled")
    if event.starts_at is not None and event.starts_at < core_db.utcnow():
        raise CancelError("This meeting has already happened and can no longer be canceled")

    client = CalendlyClient(cfg)
    if not client.configured:
        raise CancelError("CALENDLY_API_TOKEN is not set -- see .env.example")
    try:
        client.cancel_event(event.event_uri, reason=reason)
    finally:
        client.close()

    canceled = event.model_copy(update={"status": "canceled", "cancel_reason": reason or event.cancel_reason})
    result = SyncResult()
    return apply_event(conn, canceled, result)


class DeleteBookingError(RuntimeError):
    pass


def delete_booking(conn: SupabaseConn, cfg: Config, event_uri: str) -> None:
    """Permanently remove a booking, for any status -- the Bookings page's
    delete action (distinct from cancel_booking above, which only flips
    status to 'canceled' and keeps the row). If the event is still 'active'
    on Calendly, cancel it there first (same client.cancel_event call
    cancel_booking makes) so it doesn't linger as a real upcoming meeting on
    the account; a past/already-canceled event has nothing left to cancel,
    so this step is skipped for those. Either way, the local row is deleted
    last, so a failed Calendly cancel never leaves this integration lying
    about a booking that's still actually live there.

    Raises DeleteBookingError if the event is unknown locally. Raises
    CalendlyClientError if the Calendly cancel call itself fails for a
    still-active event -- the local row is left untouched in that case."""
    from . import db as calendly_db

    event = calendly_db.get_event(conn, event_uri)
    if event is None:
        raise DeleteBookingError(f"No Calendly booking found for {event_uri}")

    if event.status == "active" and (event.starts_at is None or event.starts_at >= core_db.utcnow()):
        client = CalendlyClient(cfg)
        if not client.configured:
            raise DeleteBookingError("CALENDLY_API_TOKEN is not set -- see .env.example")
        try:
            client.cancel_event(event.event_uri)
        finally:
            client.close()

    # Tombstone first: even if the row delete fails, no sync can recreate it,
    # and a sync racing this call cannot slip the event back in afterwards.
    calendly_db.mark_deleted(conn, event_uri)
    calendly_db.delete_event(conn, event_uri)

    # The Bookings list is built from items in `booked`, so a booked item whose
    # only meeting was just deleted would still show up as a booking. Back it
    # off `booked` (same move a cancellation makes) unless another live
    # booking still backs it. The item itself is never deleted.
    if event.item_id:
        item = core_db.get_item(conn, event.item_id)
        others = [e for e in calendly_db.list_events_by_item(conn, event.item_id) if e.status == "active"]
        if item is not None and item.stage == "booked" and not others:
            core_db.set_stage(
                conn, item.item_id, "waiting", actor=ACTOR,
                detail={"reason": "Calendly booking deleted", "event_uri": event_uri},
            )


class NoShowError(RuntimeError):
    pass


def mark_no_show(conn: SupabaseConn, cfg: Config, event_uri: str) -> CalendlyEvent:
    """Flag this event's invitee as a no-show in Calendly, then record it
    locally. Unlike cancel_booking, this bypasses apply_event/match_item --
    a no-show is an annotation on an existing booking, not a status
    transition, so it must never touch the matched item's pipeline stage.

    Raises NoShowError if the event is unknown locally, has no invitee to
    flag, is canceled (nothing to mark a no-show against), is already
    flagged, or still starts in the future -- Calendly only considers an
    invitee a no-show once the meeting time has passed, so this mirrors
    cancel_booking's past-event guard, inverted. Raises CalendlyClientError
    if the Calendly API call itself fails -- the local row is left
    untouched in that case."""
    from . import db as calendly_db

    event = calendly_db.get_event(conn, event_uri)
    if event is None:
        raise NoShowError(f"No Calendly booking found for {event_uri}")
    if not event.invitee_uri:
        raise NoShowError("This booking has no invitee to mark as a no-show")
    if event.status == "canceled":
        raise NoShowError("A canceled booking cannot be marked as a no-show")
    if event.no_show:
        raise NoShowError("This booking is already marked as a no-show")
    if event.starts_at is not None and event.starts_at > core_db.utcnow():
        raise NoShowError("This meeting hasn't happened yet and can't be marked as a no-show")

    client = CalendlyClient(cfg)
    if not client.configured:
        raise NoShowError("CALENDLY_API_TOKEN is not set -- see .env.example")
    try:
        resource = client.mark_no_show(event.invitee_uri)
    finally:
        client.close()

    updated = event.model_copy(update={"no_show": True, "no_show_uri": resource.get("uri")})
    return calendly_db.upsert_event(conn, updated)


def unmark_no_show(conn: SupabaseConn, cfg: Config, event_uri: str) -> CalendlyEvent:
    """Undo a mark_no_show call. Raises NoShowError if the event is unknown
    locally or isn't currently flagged. Raises CalendlyClientError if the
    Calendly API call fails -- the local row is left untouched."""
    from . import db as calendly_db

    event = calendly_db.get_event(conn, event_uri)
    if event is None:
        raise NoShowError(f"No Calendly booking found for {event_uri}")
    if not event.no_show:
        raise NoShowError("This booking is not marked as a no-show")

    client = CalendlyClient(cfg)
    if not client.configured:
        raise NoShowError("CALENDLY_API_TOKEN is not set -- see .env.example")
    try:
        client.unmark_no_show(event.no_show_uri or "")
    finally:
        client.close()

    updated = event.model_copy(update={"no_show": False, "no_show_uri": None})
    return calendly_db.upsert_event(conn, updated)


def run_sync(
    conn: SupabaseConn, cfg: Config, *, lookback_days: int = 90,
    move_stages: Optional[Callable[[OutreachItem], bool]] = None,
    skip_unchanged: bool = False,
) -> SyncResult:
    """Pull every scheduled event from Calendly's API (paginated) and apply
    each one. `lookback_days` bounds min_start_time so a long-lived account
    doesn't re-walk years of history on every sync -- 90 days comfortably
    covers this pipeline's own follow-up window (max ~a few weeks, see
    ConversationSettings.followup_after_days/max_followups) with margin."""
    result = SyncResult()
    client = CalendlyClient(cfg)
    if not client.configured:
        result.errors.append("CALENDLY_API_TOKEN is not set -- see .env.example")
        return result

    from datetime import timedelta
    min_start = (core_db.utcnow() - timedelta(days=lookback_days)).isoformat() + "Z"

    try:
        # Calendly's /scheduled_events defaults to active-only when `status`
        # is omitted -- a event canceled outside our own cancel button (in
        # Calendly directly, or before this integration's webhook/sync
        # existed) would otherwise never be seen again by this loop and stay
        # stuck at status="active" locally forever. Two passes (one per
        # status Calendly supports) so both are picked up.
        for status in ("active", "canceled"):
            page_token: Optional[str] = None
            while True:
                page = client.list_scheduled_events(min_start_time=min_start, status=status, page_token=page_token)
                for event in page.get("collection", []):
                    result.fetched += 1
                    try:
                        invitees_page = client.list_event_invitees(event["uri"])
                        for invitee in invitees_page.get("collection", []):
                            apply_event(
                                conn, normalize_scheduled_event(event, invitee), result,
                                move_stages=move_stages, skip_unchanged=skip_unchanged,
                            )
                    except CalendlyClientError as exc:
                        result.errors.append(f"{event.get('uri')}: {exc}")
                next_token = (page.get("pagination") or {}).get("next_page_token")
                if not next_token:
                    break
                page_token = next_token
    except CalendlyClientError as exc:
        result.errors.append(str(exc))
    finally:
        client.close()

    return result


def ingest_webhook_event(conn: SupabaseConn, payload: dict[str, Any]) -> Optional[CalendlyEvent]:
    """Apply one already-signature-verified webhook payload. Returns None (no
    error, nothing to do) for a webhook event type this integration does not
    act on -- Calendly can be subscribed to more event types than
    invitee.created/invitee.canceled in the future, and an unrecognized type
    must be a silent no-op, not a 4xx, so Calendly does not disable the
    subscription for repeated failures on events we simply don't model yet."""
    from .webhook import parse_webhook_payload

    fields = parse_webhook_payload(payload)
    if fields.get("webhook_event_type") not in ("invitee.created", "invitee.canceled"):
        return None
    if not fields.get("event_uri"):
        return None

    from . import db as calendly_db
    if calendly_db.is_deleted(conn, fields["event_uri"]):
        return None

    result = SyncResult()
    return apply_event(conn, normalize_webhook_event(fields), result)
