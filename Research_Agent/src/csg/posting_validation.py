"""Shared current-posting eligibility rules for Research reads and scoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import JobPosting

MAX_CURRENT_POSTING_AGE_DAYS = 45
_LIVE_SOURCES = {"live_ats_board", "live_careers_listing"}
_CLOSED_STATES = {"closed", "expired", "filled", "inactive", "archived", "removed"}
_STATUS_FIELDS = ("status", "state", "jobStatus", "postingStatus", "employmentStatus")
_FALSE_FIELDS = ("isActive", "active", "is_open", "isOpen")
_EXPIRY_FIELDS = ("validThrough", "expiresAt", "expirationDate", "closedAt", "closed_at")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _explicitly_closed(raw: dict[str, Any], now: datetime) -> bool:
    for field in _STATUS_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value.strip().lower().replace(" ", "_") in _CLOSED_STATES:
            return True
    if any(raw.get(field) is False for field in _FALSE_FIELDS):
        return True
    for field in _EXPIRY_FIELDS:
        value = _parse_datetime(raw.get(field))
        if value is not None and value <= now:
            return True
    return False


def is_current_validated_posting(
    posting: JobPosting, now: datetime | None = None,
) -> bool:
    """Require verified ownership, a live listing source, and non-stale dates."""
    validation = posting.raw.get("_csg_validation") or {}
    if validation.get("ownership") != "verified" or validation.get("current") != "verified":
        return False
    if validation.get("active_source") not in _LIVE_SOURCES:
        return False

    current_time = _normalized_datetime(now or datetime.now(timezone.utc))
    if _explicitly_closed(posting.raw, current_time):
        return False

    posted_at = posting.posted_at or posting.first_seen
    if posted_at is not None:
        age_days = (current_time - _normalized_datetime(posted_at)).total_seconds() / 86400
        if age_days < -1 or age_days > MAX_CURRENT_POSTING_AGE_DAYS:
            return False
    return True