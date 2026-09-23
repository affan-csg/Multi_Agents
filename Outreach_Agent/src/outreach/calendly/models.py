"""The calendly_events row: a normalized record of one real Calendly booking,
independent of which source told us about it (a `sync` API pull, or a
webhook delivery) and independent of the item it may or may not be matched to
yet."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

CalendlyEventStatus = Literal["active", "canceled"]
# Where this row's data most recently came from -- informational, both
# sources write the same row shape and either can update it.
CalendlySource = Literal["sync", "webhook"]


class CalendlyEvent(BaseModel):
    event_uri: str  # Calendly's own resource URI, e.g. https://api.calendly.com/scheduled_events/{uuid} -- the idempotency key
    event_uuid: str
    invitee_uri: Optional[str] = None
    invitee_email: Optional[str] = None
    invitee_name: Optional[str] = None
    status: CalendlyEventStatus = "active"
    event_type_name: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = None  # join URL / meeting location, when Calendly provides one
    cancel_reason: Optional[str] = None
    source: CalendlySource = "sync"

    # Set via CalendlyClient.mark_no_show/unmark_no_show -- an annotation on
    # a past event, independent of status (which stays 'active'/'canceled').
    no_show: bool = False
    no_show_uri: Optional[str] = None  # the invitee_no_shows resource's own uri, needed to DELETE it later

    # Matching outcome -- both None until matched (see sync.match_event).
    item_id: Optional[str] = None
    research_domain: Optional[str] = None

    raw: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("starts_at", "ends_at", "created_at", "updated_at", mode="after")
    @classmethod
    def _strip_tzinfo(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Calendly's API and Supabase both round-trip these as
        timezone-aware ISO strings (e.g. "...+00:00"), but db.utcnow() --
        which sync.py compares every one of these fields against
        (cancel_booking/delete_booking/mark_no_show's past/future checks) --
        deliberately returns a naive UTC datetime. Normalizing here, once, at
        construction keeps every comparison in sync.py naive-vs-naive instead
        of each call site needing its own tzinfo-stripping; all these
        timestamps are UTC already, so this loses no information."""
        return v.replace(tzinfo=None) if v is not None else None


# ---------------------------------------------------------------------------
# Phase 2: event types / availability / scheduling links. Unlike
# CalendlyEvent above, these are deliberately NOT persisted -- there's no
# webhook or sync loop for them, nothing to reconcile against a local table,
# so every request just reflects Calendly's live state fresh. See
# calendly/client.py's list_event_types/list_availability_schedules/
# create_scheduling_link and calendly/routes.py's GET /calendly/event-types,
# GET /calendly/availability, POST /calendly/scheduling-links.
# ---------------------------------------------------------------------------

class EventType(BaseModel):
    uri: str
    name: str
    active: bool
    duration: int  # minutes
    color: Optional[str] = None
    # Calendly's own booking page for this event type -- the honest
    # "activate/deactivate" affordance: Calendly's REST API has no
    # endpoint to toggle `active`, only its own web UI does, so the
    # Dashboard links out here instead of pretending to support it.
    scheduling_url: Optional[str] = None


class AvailabilitySchedule(BaseModel):
    uri: str
    name: str
    timezone: str
    default: bool
    # Passed through as Calendly's own rule shape (one entry per day, with
    # its bookable intervals) rather than fully modeled -- Phase 2 only
    # displays this read-only, so there's no local logic that needs a typed
    # breakdown of a rule's fields.
    rules: list[dict[str, Any]] = Field(default_factory=list)


class SchedulingLink(BaseModel):
    booking_url: str
    owner: str  # the event type's uri this link is scoped to
    owner_type: str
