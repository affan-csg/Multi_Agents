"""FastAPI backend for the Outreach Agent frontend.

Same shape as Research_Agent/src/csg/api.py: thin HTTP/WebSocket plumbing
over the ingest/pipeline modules, no business logic here. Job state is
in-process memory (see `_JOBS`), same as Research -- a server restart loses
progress tracking, not the actual data (that's in this project's Supabase).

Never touches the Research Agent's database directly -- only its HTTP API,
via research_client.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import db, ingest, personalize, pipeline
from .calendly import db as calendly_db
from .calendly.routes import router as calendly_router
from .config import load_config
from .conversation.routes import router as conversation_router
from .learning.routes import router as learning_router
from .models import STAGE_ORDER
from .optimization.routes import router as optimization_router
from .research_client import ResearchClient, ResearchClientError

_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    yield


app = FastAPI(title="CSG Outreach Agent API", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):5\d{3}",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Conversation Agent (Agent 3) -- owns sent -> waiting -> replied -> booked.
# Self-contained in conversation/; this mount is its only hook into this app.
# See src/outreach/conversation/__init__.py.
app.include_router(conversation_router)

# Feedback Learning / Optimization Agent -- read-only against everything but
# its own three tables. Self-contained in learning/; this mount is its only
# hook into this app. See src/outreach/learning/__init__.py.
app.include_router(learning_router)

# Campaign-Level Optimization layer -- read-only against everything but its
# own two tables (plus one opt-in adoption-recording write). Self-contained
# in optimization/; this mount is its only hook into this app. Independent
# of, and does not modify, the Feedback Learning Agent above. See
# src/outreach/optimization/__init__.py.
app.include_router(optimization_router)

# Calendly integration -- real API sync + webhook receiver, layered on top of
# the pre-existing classifier-only booking detection. Self-contained in
# calendly/; this mount is its only hook into this app (GET /bookings below
# is extended separately to surface its data). See
# src/outreach/calendly/__init__.py.
app.include_router(calendly_router)


# ----------------------------------------------------------------------------
# in-process job tracking
# ----------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    kind: str  # "import" | "send"
    campaign_id: Optional[str] = None
    status: str = "pending"
    done: int = 0
    total: int = 0
    current_domain: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    loop: Optional[asyncio.AbstractEventLoop] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "kind": self.kind, "campaign_id": self.campaign_id,
            "status": self.status, "done": self.done, "total": self.total,
            "current_domain": self.current_domain, "result": self.result, "error": self.error,
        }

    def publish(self) -> None:
        snap = self.snapshot()
        for q in list(self.subscribers):
            if self.loop is not None:
                self.loop.call_soon_threadsafe(q.put_nowait, snap)


_JOBS: dict[str, Job] = {}


def _new_job(kind: str, campaign_id: Optional[str] = None) -> Job:
    job = Job(job_id=str(uuid.uuid4()), kind=kind, campaign_id=campaign_id, loop=_MAIN_LOOP)
    _JOBS[job.job_id] = job
    return job


# ----------------------------------------------------------------------------
# request models
# ----------------------------------------------------------------------------

class ImportRequest(BaseModel):
    run_id: str
    tiers: list[str] = ["A", "B"]
    min_score: Optional[float] = None
    limit: Optional[int] = None


class CampaignCreateRequest(BaseModel):
    name: Optional[str] = None
    research_run_id: Optional[str] = None
    limit: Optional[int] = None
    tiers: Optional[list[str]] = None


class SetStageRequest(BaseModel):
    stage: str  # waiting | replied | booked | not_interested -- see item_app.set-stage in cli.py


class ScheduleRequest(BaseModel):
    scheduled_at: Optional[str] = None  # ISO datetime; null clears the schedule


class ReviseRequest(BaseModel):
    instruction: str  # free-text description of what to change, e.g. "make it shorter"


class RenameRequest(BaseModel):
    name: str


# ----------------------------------------------------------------------------
# background job runners
# ----------------------------------------------------------------------------

def _run_import_job(job: Job, req: ImportRequest) -> None:
    conn = db.connect()
    try:
        cfg = load_config()
        job.status = "running"
        job.publish()
        imported, excluded = ingest.import_prospects(
            conn, cfg, req.run_id, tiers=req.tiers, min_score=req.min_score, limit=req.limit,
        )
        job.status = "done"
        job.result = {
            "imported": len(imported),
            "domains": [p.research_domain for p in imported],
            "excluded_no_contact": excluded,
        }
        job.publish()
    except ResearchClientError as exc:
        job.status = "error"
        job.error = str(exc)
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


def _run_send_job(job: Job, campaign_id: str) -> None:
    conn = db.connect()
    try:
        cfg = load_config()
        job.status = "running"
        job.publish()

        def progress(done: int, total: int, domain: str) -> None:
            job.done, job.total, job.current_domain = done, total, domain
            job.publish()

        stats = pipeline.send_campaign(conn, cfg, campaign_id, on_progress=progress)
        job.status = "done"
        job.result = stats.as_dict()
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: research connection
# ----------------------------------------------------------------------------

@app.get("/research/runs")
def research_runs() -> list[dict[str, Any]]:
    """Research Agent runs available to import from -- lets the frontend
    offer a run picker without the user typing a run_id by hand."""
    cfg = load_config()
    client = ResearchClient(cfg)
    try:
        return client.list_runs()
    except ResearchClientError as exc:
        raise HTTPException(502, str(exc))
    finally:
        client.close()


@app.delete("/research/runs/{run_id}")
def delete_research_run(run_id: str) -> dict[str, Any]:
    """Called by the Research Agent when it deletes a run, so Outreach's own
    copy of that run's data doesn't outlive it -- cascade-deletes every
    campaign (and its items/events) created from this run, plus the cached
    prospects themselves. Idempotent: a run_id with nothing imported just
    returns zero counts, so the Research Agent can call this unconditionally
    on every run delete without checking whether a hand-off ever happened."""
    conn = db.connect()
    try:
        return {"status": "ok", **db.delete_by_research_run(conn, run_id)}
    finally:
        conn.close()


@app.post("/import")
def start_import(req: ImportRequest) -> dict[str, Any]:
    job = _new_job(kind="import")
    _MAIN_LOOP.run_in_executor(None, _run_import_job, job, req)
    return {"job_id": job.job_id}


@app.get("/prospects")
def list_prospects(run_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return [p.model_dump(mode="json") for p in db.list_prospects(conn, run_id=run_id)]
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: campaigns
# ----------------------------------------------------------------------------

@app.get("/campaigns")
def list_campaigns() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return [c.model_dump(mode="json") for c in db.list_campaigns(conn)]
    finally:
        conn.close()


@app.post("/campaigns")
def create_campaign(req: CampaignCreateRequest) -> dict[str, Any]:
    cfg = load_config()
    conn = db.connect()
    try:
        try:
            campaign, excluded = ingest.create_campaign_from_prospects(
                conn, cfg, name=req.name, research_run_id=req.research_run_id,
                limit=req.limit, tiers=req.tiers,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        items = db.list_items(conn, campaign_id=campaign.campaign_id)
        return {
            "campaign": campaign.model_dump(mode="json"),
            "item_count": len(items),
            "excluded_no_contact": excluded,
        }
    finally:
        conn.close()


@app.post("/campaigns/test")
def create_test_campaign() -> dict[str, Any]:
    """Create a fixed Test Campaign with two synthetic prospects (never real
    Research Agent data) for QA'ing the full send/review pipeline safely --
    see ingest.create_test_campaign for the exact fixture contents. Nothing
    is sent automatically; a person still has to click Send on the pending
    item, and the needs_review item stays put until manually approved."""
    cfg = load_config()
    conn = db.connect()
    try:
        campaign = ingest.create_test_campaign(conn, cfg)
        items = db.list_items(conn, campaign_id=campaign.campaign_id)
        return {"campaign": campaign.model_dump(mode="json"), "item_count": len(items)}
    finally:
        conn.close()


@app.post("/campaigns/{campaign_id}/rename")
def rename_campaign(campaign_id: str, req: RenameRequest) -> dict[str, Any]:
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        db.rename_campaign(conn, campaign_id, name)
        return {"campaign_id": campaign_id, "name": name}
    finally:
        conn.close()


@app.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str) -> dict[str, str]:
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        db.delete_campaign(conn, campaign_id)
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/campaigns/{campaign_id}/schedule")
def schedule_campaign(campaign_id: str, req: ScheduleRequest) -> dict[str, Any]:
    """Set (or clear, with scheduled_at=null) a send time for every pending
    item in the campaign at once -- the kebab menu's "Schedule campaign".
    Informational only today, same as the per-item version -- nothing polls
    this and auto-sends yet, `campaign send` still has to be clicked."""
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        count = db.schedule_campaign(conn, campaign_id, req.scheduled_at)
        return {"status": "ok", "items_updated": count}
    finally:
        conn.close()


def _aggregate_analytics(campaigns: list, items: list, prospects_by_domain: dict) -> dict[str, Any]:
    """Shared aggregation for the analytics page -- same arithmetic whether
    scoped to one campaign or 'all'. Mirrors the Research Agent's
    _aggregate_analytics shape/conventions (see csg/api.py) so the two
    analytics pages feel like the same product."""
    from collections import defaultdict

    stage_counts: dict[str, int] = {
        s: 0 for s in ("needs_review", "pending", "sent", "waiting", "replied", "booked", "not_interested", "failed")
    }
    tiers: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    industries: dict[str, dict[str, Any]] = {}
    personalized_from: dict[str, int] = {}
    sends_by_day: dict[str, int] = defaultdict(int)
    channel_counts: dict[str, int] = defaultdict(int)

    for item in items:
        stage_counts[item.stage] = stage_counts.get(item.stage, 0) + 1
        channel_counts[item.channel] = channel_counts.get(item.channel, 0) + 1

        source = item.personalized_from or "not_drafted"
        personalized_from[source] = personalized_from.get(source, 0) + 1

        if item.sent_at:
            day = item.sent_at.date().isoformat()
            sends_by_day[day] += 1

        prospect = prospects_by_domain.get(item.research_domain)
        if prospect:
            tier = prospect.tier or "D"
            tiers[tier] = tiers.get(tier, 0) + 1
            ind = prospect.industry or "Unknown"
            bucket = industries.setdefault(ind, {"industry": ind, "count": 0})
            bucket["count"] += 1

    total = len(items)
    # sent_total is every item past Sent, including the two terminal outcomes
    # (Booked and Not Interested/Failed) -- both are still items that were
    # sent and got a resolution, just opposite ones.
    sent_total = (
        stage_counts["sent"] + stage_counts["waiting"] + stage_counts["replied"]
        + stage_counts["booked"] + stage_counts["not_interested"]
    )
    booked = stage_counts["booked"]
    not_interested = stage_counts["not_interested"]
    # replied counts any item that got a human reply at all, positive or
    # negative -- booked and not_interested both imply a reply happened, same
    # convention the Learning/Optimization agents' outcome_summary() already
    # uses (see optimization/db.py and learning/db.py).
    replied = stage_counts["replied"] + stage_counts["booked"] + stage_counts["not_interested"]

    top_industries = sorted(industries.values(), key=lambda x: x["count"], reverse=True)[:8]

    return {
        "campaign_id": campaigns[0].campaign_id if len(campaigns) == 1 else "all",
        "campaign_count": len(campaigns),
        "total_items": total,
        "sent_total": sent_total,
        "reply_rate": round((replied / sent_total) * 100, 1) if sent_total else 0.0,
        "booking_rate": round((booked / sent_total) * 100, 1) if sent_total else 0.0,
        # Share of sent items that ended in a clear rejection -- a negative
        # campaign-outcome metric alongside booking_rate, not a replacement
        # for reply_rate (which still counts a rejection as "replied").
        "failure_rate": round((not_interested / sent_total) * 100, 1) if sent_total else 0.0,
        "stage_funnel": [{"stage": s, "count": stage_counts[s]} for s in STAGE_ORDER],
        "failed_count": stage_counts["failed"],
        "not_interested_count": not_interested,
        "needs_review_count": stage_counts["needs_review"],
        "tiers": [{"tier": k, "count": v} for k, v in sorted(tiers.items())],
        "industries": top_industries,
        "personalized_from": [{"source": k, "count": v} for k, v in sorted(personalized_from.items(), key=lambda kv: -kv[1])],
        "sends_over_time": [{"date": d, "count": c} for d, c in sorted(sends_by_day.items())],
        "channels": [{"channel": k, "count": v} for k, v in sorted(channel_counts.items(), key=lambda kv: -kv[1])],
    }


@app.get("/analytics")
def analytics(campaign_id: str = "all") -> dict[str, Any]:
    """Pre-aggregated chart data for the Outreach analytics page.
    campaign_id == 'all' aggregates across every campaign's items combined,
    otherwise scoped to one campaign -- same 'all' convention as the Research
    Agent's /runs/{run_id}/analytics."""
    conn = db.connect()
    try:
        all_campaigns = db.list_campaigns(conn)
        if campaign_id == "all":
            campaigns = all_campaigns
            items = db.list_items(conn)
        else:
            campaign = db.get_campaign(conn, campaign_id)
            if not campaign:
                raise HTTPException(404, f"{campaign_id} not found")
            campaigns = [campaign]
            items = db.list_items(conn, campaign_id=campaign_id)

        domains = list({i.research_domain for i in items})
        prospects_by_domain = db.prospects_by_domain(conn, domains)
        return _aggregate_analytics(campaigns, items, prospects_by_domain)
    finally:
        conn.close()


@app.get("/bookings")
def list_bookings(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "booked_at",
    order: str = "desc",
    page: int = 1,
    page_size: Optional[int] = None,
) -> Any:
    """Every prospect currently in the `booked` stage, across every
    campaign, for the Bookings page -- the Conversation Agent's (or a
    person's) end result: a prospect who agreed to a call. Plus any Calendly
    booking synced/webhooked in that could not be matched to a pipeline item
    at all (a booking made through some channel outside this agent's
    outreach) -- these still appear, per the Bookings page's requirement to
    show every real booking, matched or not.

    Each matched entry pairs the pipeline item with the campaign it belongs
    to, the cached prospect/contact it's for, and the exact stage-change
    event that moved it to `booked` (see db.set_stage -- every transition is
    recorded in outreach_events, so this is the same audit trail the item
    drawer already reads from, not a separate booking record). `booked_at`
    and `booked_detail` come from that event and are None only for a booking
    made before this endpoint existed. When a real calendly_events row is
    also matched to the item (see calendly/sync.py), its event-specific
    fields (meeting_starts_at/ends_at, join location, source) are merged in,
    replacing the one-size-fits-all account-wide `calendly_url` with the
    booking's own real data where available.

    All query params are optional and strictly additive: calling this with
    none of them reproduces the exact pre-Phase-1 behavior (flat array,
    active-only, most-recently-booked first) -- see test_bookings.py's
    test_bookings_default_matches_current_behavior_exactly, a regression pin
    written before this signature grew params, specifically to guarantee
    that.

    `status`: 'active' (default omitted-behavior) or 'canceled'. A matched
    item that got reverted off `booked` on cancellation is normally skipped
    entirely (see the `continue` below) since it's no longer a live booking
    -- but status=canceled explicitly opts into seeing those, which is the
    whole point of being able to look at canceled bookings at all.
    `date_from`/`date_to`: ISO date strings, filtered against
    meeting_starts_at falling back to booked_at (the same `whenOf` semantics
    the Bookings page already used client-side pre-Phase-1).
    `search`: substring match against company/contact/email/campaign name.
    `sort`/`order`: 'booked_at' (default) or 'meeting_starts_at'; 'desc'
    (default) or 'asc'.
    `page`/`page_size`: pagination is opt-in -- passing `page_size` switches
    the response from a flat list to a `{items, total, page, page_size}`
    envelope; omitting it keeps the flat-array shape today's one real
    consumer (Dashboard's listBookings()) depends on."""
    conn = db.connect()
    try:
        items = db.list_items(conn, stage="booked")

        # The account's own Calendly scheduling page -- the fallback link for
        # any booking that has no matched calendly_events row (e.g. the
        # classifier's email-heuristic path, or the sync/webhook integration
        # not being configured at all). A matched calendly_events row's own
        # `location` (its real join/meeting link) takes precedence per-entry.
        calendly_url = load_config().calendly_url or None

        calendly_events_by_item = {
            e.item_id: e for e in calendly_db.list_events(conn) if e.item_id
        }
        want_canceled = status == "canceled"

        entries: list[dict[str, Any]] = []
        if items:
            campaigns_by_id = {c.campaign_id: c for c in db.list_campaigns(conn)}
            domains = list({i.research_domain for i in items})
            prospects_by_domain = db.prospects_by_domain(conn, domains)

            for item in items:
                campaign = campaigns_by_id.get(item.campaign_id)
                prospect = prospects_by_domain.get(item.research_domain)

                # The event that actually moved this item to `booked` -- may
                # not be the LAST event on the item (a person could add a
                # note-like event afterward in the future), so this finds the
                # most recent transition landing on `booked` specifically,
                # not just the latest event of any kind.
                booked_event = next(
                    (e for e in reversed(db.list_events(conn, item.item_id))
                     if e.event_type == "stage_change" and e.to_stage == "booked"),
                    None,
                )
                cal_event = calendly_events_by_item.get(item.item_id)
                # A matched item's own calendly_events row can be canceled
                # even while the item itself is still `booked` (a cancel
                # made moments ago hasn't reverted the stage yet, or the
                # revert legitimately doesn't happen for every actor path) --
                # keep the default (active) view showing only non-canceled
                # meetings, matching what "currently booked" should mean.
                if (cal_event.status if cal_event else "active") != ("canceled" if want_canceled else "active"):
                    continue

                entries.append({
                    "item_id": item.item_id,
                    "campaign_id": item.campaign_id,
                    "campaign_name": campaign.name if campaign else None,
                    "research_domain": item.research_domain,
                    "company_name": prospect.name if prospect else None,
                    "contact_name": prospect.contact_name if prospect else None,
                    "contact_email": prospect.contact_email if prospect else item.to_email,
                    "contact_position": prospect.contact_position if prospect else None,
                    "industry": prospect.industry if prospect else None,
                    "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                    "booked_at": booked_event.created_at.isoformat() if booked_event and booked_event.created_at else None,
                    # 'reason' from db.set_stage's detail (e.g. "reply classified
                    # as booked") and, when the Conversation Agent made the move,
                    # the classified intent and the inbound message's provider_message_id.
                    "booked_detail": booked_event.detail if booked_event else {},
                    "booked_by": booked_event.actor if booked_event else None,
                    "calendly_url": (cal_event.location if cal_event else None) or calendly_url,
                    "meeting_starts_at": cal_event.starts_at.isoformat() if cal_event and cal_event.starts_at else None,
                    "meeting_ends_at": cal_event.ends_at.isoformat() if cal_event and cal_event.ends_at else None,
                    "meeting_status": cal_event.status if cal_event else None,
                    "booking_source": cal_event.source if cal_event else None,
                    "event_uri": cal_event.event_uri if cal_event else None,
                    "no_show": cal_event.no_show if cal_event else False,
                    "matched": True,
                })

        # Real Calendly bookings that never matched any pipeline item --
        # someone booked through a channel outside this agent's outreach
        # (e.g. the account-wide scheduling link shared elsewhere). Still
        # shown, per the requirement that unmatched bookings must appear.
        matched_item_ids = {e["item_id"] for e in entries}
        for cal_event in calendly_db.list_events(conn):
            if cal_event.item_id and cal_event.item_id in matched_item_ids:
                continue
            if cal_event.item_id:
                # Matched to an item, but that item isn't in this view's
                # entries -- either its stage isn't `booked` at all, or (see
                # the `continue` above the matched-loop's append) its status
                # doesn't match what this call is asking for. A reverted
                # matched booking still needs to surface when the caller
                # explicitly asked for status=canceled -- that's the entire
                # point of the filter -- so only skip it in the active
                # (default) view.
                if not (want_canceled and cal_event.status == "canceled"):
                    continue
                # Unlike a genuinely unmatched booking, this one still has a
                # real item/campaign/prospect to show -- a canceled booking
                # a caller explicitly asked to see should keep its context,
                # not collapse to a bare "unmatched" row.
                reverted_item = db.get_item(conn, cal_event.item_id)
                reverted_campaign = db.list_campaigns(conn) if reverted_item else []
                campaign = next((c for c in reverted_campaign if c.campaign_id == reverted_item.campaign_id), None) if reverted_item else None
                prospect = db.prospects_by_domain(conn, [reverted_item.research_domain]).get(reverted_item.research_domain) if reverted_item else None
                entries.append({
                    "item_id": reverted_item.item_id if reverted_item else None,
                    "campaign_id": reverted_item.campaign_id if reverted_item else None,
                    "campaign_name": campaign.name if campaign else None,
                    "research_domain": reverted_item.research_domain if reverted_item else None,
                    "company_name": prospect.name if prospect else None,
                    "contact_name": prospect.contact_name if prospect else cal_event.invitee_name,
                    "contact_email": prospect.contact_email if prospect else cal_event.invitee_email,
                    "contact_position": prospect.contact_position if prospect else None,
                    "industry": prospect.industry if prospect else None,
                    "sent_at": reverted_item.sent_at.isoformat() if reverted_item and reverted_item.sent_at else None,
                    "booked_at": cal_event.created_at.isoformat() if cal_event.created_at else None,
                    "booked_detail": {"event_uri": cal_event.event_uri},
                    "booked_by": "calendly",
                    "calendly_url": cal_event.location or calendly_url,
                    "meeting_starts_at": cal_event.starts_at.isoformat() if cal_event.starts_at else None,
                    "meeting_ends_at": cal_event.ends_at.isoformat() if cal_event.ends_at else None,
                    "meeting_status": cal_event.status,
                    "booking_source": cal_event.source,
                    "event_uri": cal_event.event_uri,
                    "no_show": cal_event.no_show,
                    "matched": reverted_item is not None,
                })
                continue
            elif cal_event.status != ("canceled" if want_canceled else "active"):
                continue
            entries.append({
                "item_id": None,
                "campaign_id": None,
                "campaign_name": None,
                "research_domain": None,
                "company_name": None,
                "contact_name": cal_event.invitee_name,
                "contact_email": cal_event.invitee_email,
                "contact_position": None,
                "industry": None,
                "sent_at": None,
                "booked_at": cal_event.created_at.isoformat() if cal_event.created_at else None,
                "booked_detail": {"event_uri": cal_event.event_uri},
                "booked_by": "calendly",
                "calendly_url": cal_event.location or calendly_url,
                "meeting_starts_at": cal_event.starts_at.isoformat() if cal_event.starts_at else None,
                "meeting_ends_at": cal_event.ends_at.isoformat() if cal_event.ends_at else None,
                "meeting_status": cal_event.status,
                "booking_source": cal_event.source,
                "event_uri": cal_event.event_uri,
                "no_show": cal_event.no_show,
                "matched": False,
            })

        if date_from:
            entries = [e for e in entries if (e["meeting_starts_at"] or e["booked_at"] or "") >= date_from]
        if date_to:
            # A bare date (no time) as an inclusive upper bound must still
            # catch a same-day timestamp with a later time-of-day -- compare
            # against the end of that day rather than its start.
            date_to_bound = date_to if len(date_to) > 10 else f"{date_to}T23:59:59"
            entries = [e for e in entries if (e["meeting_starts_at"] or e["booked_at"] or "") <= date_to_bound]
        if search:
            needle = search.strip().lower()
            if needle:
                def _matches(e: dict[str, Any]) -> bool:
                    haystack = " ".join(str(e.get(f) or "") for f in (
                        "company_name", "research_domain", "contact_name", "contact_email", "campaign_name",
                    )).lower()
                    return needle in haystack
                entries = [e for e in entries if _matches(e)]

        # Stable sort. `enumerate` breaks ties on identical sort-key values
        # by insertion order rather than leaving Python's sort to (stably,
        # but silently) leave same-timestamp entries in whatever order
        # db.list_items returned them -- two bookings can legitimately land
        # in the same tick and end up with an identical timestamp down to
        # the microsecond (observed on this machine: two back-to-back
        # db.set_stage calls can resolve to the same wall-clock reading), so
        # relying on strict inequality alone is not safe.
        sort_field = "meeting_starts_at" if sort == "meeting_starts_at" else "booked_at"
        reverse = order != "asc"
        indexed = list(enumerate(entries))
        indexed.sort(key=lambda pair: (pair[1][sort_field] or "", -pair[0] if reverse else pair[0]), reverse=reverse)
        sorted_entries = [entry for _, entry in indexed]

        if page_size is None:
            return sorted_entries

        page = max(page, 1)
        page_size = max(page_size, 1)
        start = (page - 1) * page_size
        return {
            "items": sorted_entries[start:start + page_size],
            "total": len(sorted_entries),
            "page": page,
            "page_size": page_size,
        }
    finally:
        conn.close()


@app.get("/campaigns/{campaign_id}/items")
def campaign_items(campaign_id: str) -> list[dict[str, Any]]:
    """The pipeline board's data: every item in a campaign with its stage."""
    conn = db.connect()
    try:
        return [i.model_dump(mode="json") for i in db.list_items(conn, campaign_id=campaign_id)]
    finally:
        conn.close()


@app.post("/campaigns/{campaign_id}/send")
def start_send(campaign_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        pending = [i for i in db.list_items(conn, campaign_id=campaign_id) if i.stage == "pending"]
        if not pending:
            return {"job_id": None, "message": "nothing pending to send"}
    finally:
        conn.close()

    job = _new_job(kind="send", campaign_id=campaign_id)
    job.total = len(pending)
    _MAIN_LOOP.run_in_executor(None, _run_send_job, job, campaign_id)
    return {"job_id": job.job_id, "items": len(pending)}


# ----------------------------------------------------------------------------
# routes: items -- manual stage advancement past 'sent'
# ----------------------------------------------------------------------------

@app.get("/items/{item_id}")
def item_detail(item_id: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")
        events = db.list_events(conn, item_id)
        return {"item": item.model_dump(mode="json"), "events": [e.model_dump(mode="json") for e in events]}
    finally:
        conn.close()


@app.get("/items/{item_id}/preview")
def item_preview(item_id: str) -> dict[str, Any]:
    """Return the subject/body this item will send -- drafted exactly once,
    at campaign-creation time (see ingest.create_campaign_from_prospects).
    Purely reads the stored draft; never regenerates it, so what a user
    reviews here is guaranteed to be what `campaign send` actually sends.
    Only falls back to drafting on the spot for older items created before
    upfront drafting existed (subject/body would be empty)."""
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")
        if item.subject and item.body:
            return {"subject": item.subject, "body": item.body, "personalized_from": item.personalized_from}
        prospect = db.get_prospect(conn, item.research_domain)
        if not prospect:
            raise HTTPException(404, f"no cached prospect for {item.research_domain}")
        cfg = load_config()
        draft = personalize.draft_message(cfg, prospect)
        db.update_item_fields(
            conn, item_id, subject=draft.subject, body=draft.body,
            personalized_from=draft.personalized_from,
        )
        return {"subject": draft.subject, "body": draft.body, "personalized_from": draft.personalized_from}
    finally:
        conn.close()


@app.post("/items/{item_id}/revise")
def revise_item(item_id: str, req: ReviseRequest) -> dict[str, Any]:
    """Edit the already-drafted email per a free-text instruction, in place
    -- e.g. "mention our AI hiring experience" or "make it shorter". Updates
    the stored subject/body so the drawer preview and the actual send both
    reflect the edit; does not regenerate the email from scratch."""
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")
        if item.stage not in ("pending", "needs_review"):
            raise HTTPException(400, "only pending or needs-review items can be revised")
        if not item.subject or not item.body:
            raise HTTPException(400, "this item has no draft yet to revise")
        prospect = db.get_prospect(conn, item.research_domain)
        if not prospect:
            raise HTTPException(404, f"no cached prospect for {item.research_domain}")
        cfg = load_config()
        learning_ctx = None
        try:
            from .learning.context import get_learning_context_summary
            learning_ctx = get_learning_context_summary(conn)
        except Exception:
            pass  # learning unavailable -- draft proceeds unmodified, see rl_prompt.md section 12
        from .optimization.context import fetch_optimization_context_safely
        optimization_ctx = fetch_optimization_context_safely(conn)
        combined_ctx = "\n\n".join(c for c in (learning_ctx, optimization_ctx) if c) or None
        try:
            draft = personalize.revise_message(
                cfg, prospect, current_subject=item.subject, current_body=item.body,
                instruction=req.instruction, learning_context=combined_ctx,
            )
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
        before_subject, before_body = item.subject, item.body
        db.update_item_fields(
            conn, item_id, subject=draft.subject, body=draft.body,
            personalized_from=draft.personalized_from,
        )
        try:
            from .learning.db import record_draft_edit
            record_draft_edit(
                conn, item_id=item_id, campaign_id=item.campaign_id, source="revise",
                before_subject=before_subject, before_body=before_body,
                after_subject=draft.subject, after_body=draft.body,
                instruction=req.instruction, prospect=prospect.model_dump(mode="json"),
            )
        except Exception:
            pass  # learning write failure must never break the revise response
        return {"subject": draft.subject, "body": draft.body, "personalized_from": draft.personalized_from}
    finally:
        conn.close()


@app.post("/items/{item_id}/redraft")
def redraft_item(item_id: str) -> dict[str, Any]:
    """Fully regenerate this item's draft from its CURRENT cached prospect
    data, overwriting whatever was drafted before -- unlike /revise, this
    throws away the old wording rather than editing it. Meant for an item
    that was drafted as a generic 'template_only' email because the prospect
    had no usable research data at all at campaign-creation time (see
    personalize.draft_message), and has since gained some -- e.g.
    `csg qualify` ran after the campaign was created, or a re-import picked
    up a freshly-enriched description/open_reqs -- re-running the draft now
    can produce a personalized one.

    Passes use_web_search=True: unlike campaign creation, a manual redraft is
    a single, deliberate, user-initiated action, so it's worth spending a
    live web-search call to have the LLM go find more information about the
    company when nothing is cached locally, rather than settling for the
    static template immediately (see personalize.draft_message's tier 3).

    If the item was sitting in needs_review and the new draft clears BOTH
    gates campaign creation checks (see ingest.create_campaign_from_prospects:
    a real contact email AND a genuinely personalized draft), it's
    auto-promoted to pending -- same "earn your way into Pending" rule, just
    re-checked on demand instead of only once at creation time. Also
    refreshes to_email/contact_name from the current cached prospect: a
    contact can legitimately appear after the item was created (e.g. a
    re-import after Hunter.io contact discovery finds someone), and the item
    must not stay stuck on its stale "no contact" snapshot from creation
    time forever."""
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")
        if item.stage not in ("pending", "needs_review"):
            raise HTTPException(400, "only pending or needs-review items can be redrafted")
        prospect = db.get_prospect(conn, item.research_domain)
        if not prospect:
            raise HTTPException(404, f"no cached prospect for {item.research_domain}")
        cfg = load_config()
        learning_ctx = None
        try:
            from .learning.context import get_learning_context_summary
            learning_ctx = get_learning_context_summary(conn)
        except Exception:
            pass  # learning unavailable -- draft proceeds unmodified, see rl_prompt.md section 12
        from .optimization.context import fetch_optimization_context_safely
        optimization_ctx = fetch_optimization_context_safely(conn)
        combined_ctx = "\n\n".join(c for c in (learning_ctx, optimization_ctx) if c) or None
        before_subject, before_body = item.subject, item.body
        draft = personalize.draft_message(cfg, prospect, use_web_search=True, learning_context=combined_ctx)
        db.update_item_fields(
            conn, item_id, subject=draft.subject, body=draft.body,
            personalized_from=draft.personalized_from,
            to_email=prospect.contact_email,
        )
        try:
            from .learning.db import record_draft_edit
            record_draft_edit(
                conn, item_id=item_id, campaign_id=item.campaign_id, source="redraft",
                before_subject=before_subject, before_body=before_body,
                after_subject=draft.subject, after_body=draft.body,
                prospect=prospect.model_dump(mode="json"),
            )
        except Exception:
            pass  # learning write failure must never break the redraft response
        ready = prospect.has_contact and draft.personalized_from != "template_only"
        if item.stage == "needs_review" and ready:
            db.set_stage(conn, item_id, "pending", actor="system", detail={"reason": "redraft now personalized"})
        return {
            "subject": draft.subject, "body": draft.body, "personalized_from": draft.personalized_from,
            "to_email": prospect.contact_email,
            "stage": "pending" if item.stage == "needs_review" and ready else item.stage,
        }
    finally:
        conn.close()


@app.post("/campaigns/{campaign_id}/redraft-generic")
def redraft_generic_items(campaign_id: str) -> dict[str, Any]:
    """Bulk version of /items/{item_id}/redraft: re-drafts every pending or
    needs-review item in the campaign that's still on a 'template_only'
    generic draft, using each item's current cached prospect data (and, same
    as the single-item redraft, a live web-search-augmented LLM call when
    nothing cached is enough -- see personalize.draft_message's tier 3).
    Items that are already personalized (or already sent) are left
    untouched, so a search call is only ever spent on items that are still
    stuck on the static template -- not the whole campaign. Any needs_review
    item that becomes genuinely personalized AND already has a contact email
    is auto-promoted to pending -- a redraft only ever fixes personalization,
    not contact discovery, so an item still missing a contact stays in
    needs_review even once its draft is personalized (see
    /items/{item_id}/redraft's docstring for the same two-gate rule).
    Returns how many were upgraded vs. still generic (every tier, including
    web search, still failed to produce a usable draft)."""
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        items = [
            i for i in db.list_items(conn, campaign_id=campaign_id)
            if i.stage in ("pending", "needs_review") and i.personalized_from == "template_only"
        ]
        cfg = load_config()
        learning_ctx = None
        try:
            from .learning.context import get_learning_context_summary
            learning_ctx = get_learning_context_summary(conn)
        except Exception:
            pass  # learning unavailable -- drafts proceed unmodified, see rl_prompt.md section 12
        from .optimization.context import fetch_optimization_context_safely
        optimization_ctx = fetch_optimization_context_safely(conn)
        combined_ctx = "\n\n".join(c for c in (learning_ctx, optimization_ctx) if c) or None
        upgraded = 0
        still_generic = 0
        for item in items:
            prospect = db.get_prospect(conn, item.research_domain)
            if not prospect:
                continue
            before_subject, before_body = item.subject, item.body
            draft = personalize.draft_message(cfg, prospect, use_web_search=True, learning_context=combined_ctx)
            db.update_item_fields(
                conn, item.item_id, subject=draft.subject, body=draft.body,
                personalized_from=draft.personalized_from,
            )
            try:
                from .learning.db import record_draft_edit
                record_draft_edit(
                    conn, item_id=item.item_id, campaign_id=campaign_id, source="redraft_generic",
                    before_subject=before_subject, before_body=before_body,
                    after_subject=draft.subject, after_body=draft.body,
                    prospect=prospect.model_dump(mode="json"),
                )
            except Exception:
                pass  # one item's learning-write failure must never stop the rest of the batch
            if draft.personalized_from == "template_only":
                still_generic += 1
            else:
                upgraded += 1
                if item.stage == "needs_review" and prospect.has_contact:
                    db.set_stage(conn, item.item_id, "pending", actor="system", detail={"reason": "redraft now personalized"})
        return {"checked": len(items), "upgraded": upgraded, "still_generic": still_generic}
    finally:
        conn.close()


@app.post("/items/{item_id}/approve")
def approve_item(item_id: str) -> dict[str, str]:
    """Manually move a needs_review item into pending -- the escape hatch
    for a prospect that will never get real research data (e.g. Research
    Agent qualification isn't going to run for it) but a person has read the
    generic draft and is fine sending it anyway. Requires a person's
    deliberate action; nothing does this automatically."""
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")
        if item.stage != "needs_review":
            raise HTTPException(400, "only needs-review items can be approved")
        db.set_stage(conn, item_id, "pending", actor="user", detail={"reason": "manually approved from needs review"})
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/items/{item_id}/schedule")
def schedule_item(item_id: str, req: ScheduleRequest) -> dict[str, str]:
    """Set (or clear, with scheduled_at=null) when this item should send.
    Informational only today -- nothing polls this and auto-sends yet;
    `campaign send` still has to be clicked."""
    conn = db.connect()
    try:
        if not db.get_item(conn, item_id):
            raise HTTPException(404, f"{item_id} not found")
        db.update_item_fields(conn, item_id, scheduled_at=req.scheduled_at)
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/items/{item_id}")
def delete_item(item_id: str) -> dict[str, str]:
    """Remove a single prospect from its campaign. The campaign and the
    cached prospect row are untouched."""
    conn = db.connect()
    try:
        if not db.get_item(conn, item_id):
            raise HTTPException(404, f"{item_id} not found")
        db.delete_item(conn, item_id)
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/items/{item_id}/stage")
def set_item_stage(item_id: str, req: SetStageRequest) -> dict[str, str]:
    """Manually advance (or re-open) an item past 'sent', as a person.

    The Outreach Agent never calls this itself -- pending/sent/failed are the
    only stages it sets (see pipeline.send_campaign). waiting/replied/booked/
    not_interested belong to the Conversation Agent when it is switched on
    for the campaign (see conversation/engine.py), and to a person otherwise.

    This route stays available either way, deliberately: dragging a card by
    hand is exactly how you drive the pipeline with the agent switched off,
    and an `actor="user"` move is always allowed to override the agent's --
    including moving a card OUT of not_interested (e.g. a misclassified
    rejection) since this route is not bound by CONVERSATION_TRANSITIONS,
    only the Conversation Agent's own automated moves are."""
    if req.stage not in ("waiting", "replied", "booked", "not_interested"):
        raise HTTPException(400, "only waiting/replied/booked/not_interested can be set via this route")
    conn = db.connect()
    try:
        db.set_stage(conn, item_id, req.stage, actor="user")
        return {"status": "ok"}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# job status + progress streaming
# ----------------------------------------------------------------------------

@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.snapshot()


@app.websocket("/jobs/{job_id}/ws")
async def job_progress_ws(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    job = _JOBS.get(job_id)
    if not job:
        await websocket.close(code=4404)
        return

    queue: asyncio.Queue = asyncio.Queue()
    job.subscribers.append(queue)

    try:
        await websocket.send_text(json.dumps(job.snapshot()))
        while True:
            snap = await queue.get()
            await websocket.send_text(json.dumps(snap))
            if snap["status"] in ("done", "error"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        if queue in job.subscribers:
            job.subscribers.remove(queue)
