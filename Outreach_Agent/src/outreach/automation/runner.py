"""One polling cycle.

    1. Take the DB lease (no overlap, across processes and replicas).
    2. Sync Calendly (bookings are always recorded; cards move only for
       campaigns whose Conversation Agent is ON).
    3. Read the inbox once, incrementally, shared by every campaign.
    4. Tick each enabled campaign. The tick only runs its follow-up phase when
       BOTH reads above succeeded -- a failed sync never leads to a blind send.
    5. Persist what happened and when to run next (UTC), release the lease.

Times are naive UTC throughout (db.utcnow), matching every other timestamp in
the system.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .. import db
from ..config import Config
from ..conversation import engine, inbox
from ..conversation.inbox import InboxError
from ..conversation.settings import is_enabled
from ..db import SupabaseConn, utcnow
from ..models import AutomationState, OutreachItem

logger = logging.getLogger("outreach.automation")

JOB = "sync"
DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60

# A cycle that dies without releasing its lease frees itself after this long.
LEASE_MINUTES = 20
# The scheduled Calendly sync only re-walks recent events; the manual button
# and webhook still cover older history.
CALENDLY_LOOKBACK_DAYS = 30
# Mail is re-read from one day before the last good read: IMAP SINCE has day
# granularity, and ingestion dedupes by provider message id.
INBOX_OVERLAP = timedelta(days=1)
# After a failed cycle, retry sooner than a full interval (but never sooner
# than the first backoff step), doubling per consecutive failure.
_BACKOFF_FIRST = timedelta(minutes=5)


def env_interval_minutes() -> int:
    raw = os.environ.get("AUTO_SYNC_INTERVAL_MINUTES")
    try:
        value = int(raw) if raw else DEFAULT_INTERVAL_MINUTES
    except ValueError:
        value = DEFAULT_INTERVAL_MINUTES
    return clamp_interval(value)


def clamp_interval(minutes: int) -> int:
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(minutes)))


def scheduler_enabled() -> bool:
    return os.environ.get("AUTO_SYNC_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CycleResult:
    status: str  # ok | partial | failed | skipped_locked
    started_at: datetime
    finished_at: Optional[datetime] = None
    calendly: Optional[dict[str, Any]] = None
    inbox: Optional[dict[str, Any]] = None
    campaigns: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "calendly": self.calendly,
            "inbox": self.inbox,
            "campaigns": self.campaigns,
            "errors": self.errors,
        }


def ensure_state(conn: SupabaseConn) -> AutomationState:
    """The state row, created on first use with the interval seeded from
    AUTO_SYNC_INTERVAL_MINUTES. Afterwards the database value wins, so the
    dashboard's interval edit is not overwritten by the env var on restart."""
    rows = conn.select("automation_state", job=f"eq.{JOB}", limit=1)
    if not rows:
        db.update_automation_state(conn, JOB, interval_minutes=env_interval_minutes())
    return db.get_automation_state(conn, JOB)


def is_due(state: AutomationState, now: datetime) -> bool:
    if state.next_run_at is None:
        return True
    nxt = state.next_run_at.replace(tzinfo=None) if state.next_run_at.tzinfo else state.next_run_at
    return nxt <= now


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def run_cycle(
    conn: SupabaseConn, cfg: Config, *,
    owner: Optional[str] = None, now: Optional[datetime] = None,
) -> CycleResult:
    """Run one polling cycle. Safe to call from anywhere, at any time: if
    another cycle holds the lease this returns `skipped_locked` and does
    nothing. Never raises for operational failures -- they are recorded."""
    now = now or utcnow()
    owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    result = CycleResult(status="ok", started_at=now)

    state = ensure_state(conn)
    interval = timedelta(minutes=clamp_interval(state.interval_minutes))

    if not db.claim_automation_lease(
        conn, JOB, owner, now=now, until=now + timedelta(minutes=LEASE_MINUTES),
    ):
        result.status = "skipped_locked"
        result.finished_at = now
        logger.info("automation cycle skipped: another run holds the lease")
        return result

    calendly_ok = True
    inbox_ok = True
    fresh_state: dict[str, Any] = {}
    try:
        logger.info("automation cycle start owner=%s", owner)

        # --- Calendly -------------------------------------------------------
        calendly_ok = _sync_calendly(conn, cfg, result, now, fresh_state)
        db.renew_automation_lease(
            conn, JOB, owner, until=utcnow() + timedelta(minutes=LEASE_MINUTES),
        )

        # --- inbox (one fetch, shared by every campaign) --------------------
        campaigns = db.list_enabled_conversation_campaigns(conn)
        emails: list = []
        if campaigns:
            inbox_ok, emails = _fetch_inbox(conn, cfg, result, now, state, fresh_state)
        else:
            result.inbox = {"skipped": "no campaign has the conversation agent enabled"}
        db.renew_automation_lease(
            conn, JOB, owner, until=utcnow() + timedelta(minutes=LEASE_MINUTES),
        )

        # --- conversation ticks ----------------------------------------------
        for campaign_id in campaigns:
            try:
                tick = engine.tick(
                    conn, cfg, campaign_id, emails=emails, now=now,
                    inbox_ok=inbox_ok, calendly_ok=calendly_ok,
                )
                result.campaigns.append(tick.as_dict())
                for domain, err in tick.errors.items():
                    result.errors.append(f"{campaign_id}/{domain}: {err}")
            except Exception as exc:  # one campaign must not stop the others
                logger.exception("automation tick failed for %s", campaign_id)
                result.errors.append(f"{campaign_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        logger.exception("automation cycle crashed")
        result.errors.append(f"{type(exc).__name__}: {exc}")
        inbox_ok = calendly_ok = False
    finally:
        _persist(conn, owner, result, now, interval, state, inbox_ok, calendly_ok, fresh_state)
    return result


def _sync_calendly(
    conn: SupabaseConn, cfg: Config, result: CycleResult, now: datetime, fresh: dict[str, Any],
) -> bool:
    if not cfg.calendly_api_token:
        result.calendly = {"skipped": "CALENDLY_API_TOKEN not set"}
        return True

    from ..calendly import sync as calendly_sync

    enabled_cache: dict[str, bool] = {}

    def move_stages(item: OutreachItem) -> bool:
        # Bookings are always recorded; moving a card is the agent's job, so
        # only for campaigns where it is switched ON.
        if item.campaign_id not in enabled_cache:
            enabled_cache[item.campaign_id] = is_enabled(conn, item.campaign_id)
        return enabled_cache[item.campaign_id]

    try:
        sync = calendly_sync.run_sync(
            conn, cfg, lookback_days=CALENDLY_LOOKBACK_DAYS,
            move_stages=move_stages, skip_unchanged=True,
        )
    except Exception as exc:
        logger.exception("calendly sync crashed")
        result.calendly = {"errors": [f"{type(exc).__name__}: {exc}"]}
        result.errors.append(f"calendly: {type(exc).__name__}: {exc}")
        return False

    result.calendly = sync.as_dict()
    if sync.errors:
        result.errors.extend(f"calendly: {e}" for e in sync.errors[:5])
        return False
    fresh["last_calendly_success_at"] = now
    return True


def _fetch_inbox(
    conn: SupabaseConn, cfg: Config, result: CycleResult, now: datetime,
    state: AutomationState, fresh: dict[str, Any],
) -> tuple[bool, list]:
    if not inbox.imap_configured():
        result.inbox = {"error": "IMAP is not configured"}
        result.errors.append("inbox: IMAP is not configured")
        return False, []

    since = _naive(state.last_inbox_success_at)
    since = since - INBOX_OVERLAP if since else None
    try:
        emails = inbox.fetch_all_replies(cfg, lookback_days=cfg.imap_lookback_days, since=since)
    except InboxError as exc:
        result.inbox = {"error": str(exc)}
        result.errors.append(f"inbox: {exc}")
        return False, []
    except Exception as exc:
        logger.exception("inbox fetch crashed")
        result.inbox = {"error": f"{type(exc).__name__}: {exc}"}
        result.errors.append(f"inbox: {type(exc).__name__}: {exc}")
        return False, []

    result.inbox = {"fetched": len(emails), "since": since.isoformat() if since else None}
    fresh["last_inbox_success_at"] = now
    return True, emails


def _persist(
    conn: SupabaseConn, owner: str, result: CycleResult, now: datetime, interval: timedelta,
    prior: AutomationState, inbox_ok: bool, calendly_ok: bool, fresh: dict[str, Any],
) -> None:
    """Write the cycle's outcome and the next run time, then release the
    lease. Persisting is what makes a restart resume the schedule."""
    ok = inbox_ok and calendly_ok and not result.errors
    result.status = "ok" if ok else ("partial" if fresh else "failed")
    result.finished_at = utcnow()

    failures = 0 if ok else prior.consecutive_failures + 1
    if ok:
        next_run = now + interval
    else:
        backoff = _BACKOFF_FIRST * (2 ** max(0, failures - 1))
        next_run = now + min(interval, backoff)

    fields: dict[str, Any] = {
        "next_run_at": next_run,
        "consecutive_failures": failures,
        "last_error": "; ".join(result.errors[:3])[:1000] if result.errors else None,
        "last_result": result.as_dict(),
        **fresh,
    }
    if ok:
        fields["last_success_at"] = result.finished_at
    try:
        db.update_automation_state(conn, JOB, **fields)
    finally:
        db.release_automation_lease(conn, JOB, owner)
    logger.info(
        "automation cycle %s errors=%d next_run_at=%s",
        result.status, len(result.errors), next_run.isoformat(),
    )
