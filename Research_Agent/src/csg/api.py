"""FastAPI backend for the frontend UI.

Wraps the discover/qualify/contacts modules -- no business logic lives here,
only HTTP/WebSocket plumbing and request validation. A research run is
started with POST /discover, whose entire input is a region; everything else
comes from the ICP config. Runs locally (uvicorn) for one user at a time; job
state is kept in-process memory (see `_JOBS`), not persisted -- a server
restart loses in-flight progress tracking, but nothing in Supabase, which is
where the actual results live.

SupabaseConn is a synchronous httpx.Client (see db.py), so all DB-touching
pipeline work runs in a background thread (via the default executor) rather
than blocking the async event loop that serves the WebSocket/HTTP requests.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import contacts as contacts_mod, db, discover as discover_mod, qualify as qualify_mod
from .ats import normalize_domain
from .config import load_config, update_icp_basics
from .discovery import DiscoveryError
from .models import Prospect

_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # FastAPI dispatches sync `def` route handlers to a worker thread pool
    # (via anyio), where asyncio.get_event_loop() raises -- there is no
    # per-thread loop there. Capturing the one true loop here, from an
    # actual coroutine running on it, is what lets Job.publish() hand
    # progress events back to it safely from any thread.
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    yield


app = FastAPI(title="CSG Research Agent API", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    # Vite picks the next free port when 5173 is taken, so allow the local
    # dev range rather than pinning one port.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):5\d{3}",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# in-process job tracking (progress only -- results live in Supabase)
# ----------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    kind: str  # "discover" | "run" | "qualify" | "contacts"
    run_id: Optional[str] = None
    status: str = "pending"  # pending -> running -> done | error
    done: int = 0
    total: int = 0
    current_domain: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    loop: Optional[asyncio.AbstractEventLoop] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "kind": self.kind, "run_id": self.run_id,
            "status": self.status, "done": self.done, "total": self.total,
            "current_domain": self.current_domain, "result": self.result, "error": self.error,
        }

    def publish(self) -> None:
        snap = self.snapshot()
        for q in list(self.subscribers):
            if self.loop is not None:
                self.loop.call_soon_threadsafe(q.put_nowait, snap)


_JOBS: dict[str, Job] = {}


def _new_job(kind: str, run_id: Optional[str] = None) -> Job:
    # loop is the captured main event loop (see _capture_main_loop), not
    # whatever thread this function happens to run on -- sync `def` route
    # handlers run in an anyio worker thread with no event loop of their own,
    # and capturing the loop only when a WebSocket connects would silently
    # drop any progress event published before the frontend opens one.
    job = Job(job_id=str(uuid.uuid4()), kind=kind, run_id=run_id, loop=_MAIN_LOOP)
    _JOBS[job.job_id] = job
    return job


# ----------------------------------------------------------------------------
# request/response models
# ----------------------------------------------------------------------------

class DiscoverRequest(BaseModel):
    """The only input to an autonomous run. Everything else -- size band,
    financial thresholds, practice areas, priority rules -- is embedded in
    icp.yaml and never supplied per run.

    `auto_qualify_top`, when set, chains LLM qualification + Hunter.io contact
    discovery onto the run for the top N scored companies (Tier A/B only) --
    same selection the CLI's `--auto-qualify-top` flag drives, see
    discover.run_discovery. Costs real LLM tokens and Hunter.io quota (free
    tier: 25 searches/month), so this is opt-in per run, not a default."""
    region: Optional[str] = None      # None = the active region in icp.yaml
    limit: int = 100
    use_cache: bool = True
    label: Optional[str] = None
    auto_qualify_top: Optional[int] = None


class QualifyRequest(BaseModel):
    tiers: list[str] = ["A", "B"]
    limit_reqs: int = 8
    force: bool = False


class ContactsRequest(BaseModel):
    tiers: list[str] = ["A", "B"]
    limit_per_domain: int = 5
    force: bool = False


class SuppressRequest(BaseModel):
    domain: str
    reason: str = "client"


class SaveRequest(BaseModel):
    domain: str
    note: Optional[str] = None


class RenameRunRequest(BaseModel):
    label: str


# ----------------------------------------------------------------------------
# background job runners (executed in a worker thread)
# ----------------------------------------------------------------------------

def _run_discover_job(job: Job, req: DiscoverRequest) -> None:
    """Autonomous discovery: region in, ranked prospects out."""
    conn = db.connect()
    try:
        cfg = load_config()
        job.status = "running"
        job.total = req.limit
        job.publish()

        def progress(stage: str, done: int, total: int, detail: str) -> None:
            job.done, job.total = done, total or req.limit
            job.current_domain = f"{stage}: {detail}"
            job.publish()

        stats = asyncio.run(discover_mod.run_discovery(
            cfg, region=req.region, limit=req.limit, conn=conn,
            label=req.label, use_cache=req.use_cache, on_progress=progress,
            auto_qualify_top=req.auto_qualify_top,
        ))
        job.run_id = stats.run_id
        job.status = "done"
        job.result = stats.as_dict()
        job.publish()
    except DiscoveryError as exc:
        job.status = "error"
        job.error = str(exc)
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


def _run_resume_job(job: Job, run_id: str, domains: list[str]) -> None:
    """Continue an already-started run for the specific companies that
    stalled (see db.stalled_companies) -- does not re-run discovery, just
    re-enrichment + re-scoring for these domains under the same run_id."""
    conn = db.connect()
    try:
        cfg = load_config()
        job.status = "running"
        job.total = len(domains)
        job.publish()

        def progress(stage: str, done: int, total: int, detail: str) -> None:
            job.done, job.total = done, total or len(domains)
            job.current_domain = f"{stage}: {detail}"
            job.publish()

        stats = asyncio.run(discover_mod.resume_stalled(
            cfg, run_id, domains, conn=conn, on_progress=progress,
        ))
        job.run_id = stats.run_id
        job.status = "done"
        job.result = stats.as_dict()
        job.publish()
    except DiscoveryError as exc:
        job.status = "error"
        job.error = str(exc)
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


def _run_qualify_job(job: Job, run_id: str, req: QualifyRequest) -> None:
    conn = db.connect()
    try:
        job.status = "running"
        job.publish()

        def progress(done: int, total: int, domain: str) -> None:
            job.done, job.total, job.current_domain = done, total, domain
            job.publish()

        stats = asyncio.run(qualify_mod.qualify_run(
            conn, run_id, tiers=req.tiers, limit_reqs=req.limit_reqs,
            force=req.force, on_progress=progress,
        ))
        job.status = "done"
        job.result = stats
        job.publish()
    except qualify_mod.QualificationError as exc:
        job.status = "error"
        job.error = str(exc)
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


def _run_contacts_job(job: Job, run_id: str, req: ContactsRequest) -> None:
    conn = db.connect()
    try:
        job.status = "running"
        job.publish()

        rows = [r for r in db.get_scores(conn, run_id) if r["tier"] in req.tiers]
        domains = [r["domain"] for r in rows]
        pending = domains if req.force else [d for d in domains if not db.has_contacts(conn, d)]
        job.total = len(pending)

        def progress(done: int, total: int, domain: str) -> None:
            job.done, job.total, job.current_domain = done, total, domain
            job.publish()

        stats = asyncio.run(contacts_mod.discover_run(
            conn, pending, limit_per_domain=req.limit_per_domain, force=req.force, on_progress=progress,
        ))
        job.status = "done"
        job.result = stats
        job.publish()
    except contacts_mod.ContactDiscoveryError as exc:
        job.status = "error"
        job.error = str(exc)
        job.publish()
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.publish()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: discovery -- the whole input surface for starting a research run
# ----------------------------------------------------------------------------

@app.get("/regions")
def list_regions() -> dict[str, Any]:
    """Regions the agent can search, plus the configured default.

    This is the whole input surface for a research run -- the frontend needs
    nothing else to start one.
    """
    cfg = load_config()
    return {
        "active": cfg.active_region,
        "regions": [
            {
                "key": key,
                "label": (spec or {}).get("label") or key,
                "states": sorted(cfg.region_states(key)),
                "hubs": cfg.region_hubs(key),
            }
            for key, spec in cfg.regions.items()
        ],
        "icp": {
            "profile": cfg.raw.get("profile"),
            "size_band": [int(cfg.size.get("sweet_min", 0)), int(cfg.size.get("sweet_max", 0))],
            "revenue_min": (cfg.raw.get("financials") or {}).get("revenue_sweet_min"),
            "practice_areas": [a.key for a in cfg.enabled_areas],
        },
    }


@app.post("/discover")
def start_discover(req: DiscoverRequest) -> dict[str, Any]:
    """Start an autonomous discovery run. Region is the only input."""
    cfg = load_config()
    job = _new_job(kind="discover")
    _MAIN_LOOP.run_in_executor(None, _run_discover_job, job, req)
    return {"job_id": job.job_id, "region": cfg.resolve_region(req.region)}


# ----------------------------------------------------------------------------
# routes: runs
# ----------------------------------------------------------------------------

@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        runs = conn.select("runs", order="started_at.desc", limit=50)
        for r in runs:
            r["stalled_count"] = len(db.stalled_companies(conn, r["run_id"]))
        return runs
    finally:
        conn.close()


@app.patch("/runs/{run_id}")
def rename_run(run_id: str, req: RenameRunRequest) -> dict[str, str]:
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "label must not be empty")
    conn = db.connect()
    try:
        db.rename_run(conn, run_id, label)
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str) -> dict[str, Any]:
    """Continue a stalled run -- one where every provider (Gemini, and the
    Tavily+Groq fallback) was exhausted for some companies, leaving them
    scored at 0 with no ATS board and no firmographics. Only meaningful for a
    run that actually has such companies; the frontend only shows this action
    when stalled_count > 0 (see GET /runs), and this route re-checks that
    itself rather than trusting the caller. Re-researches just those
    companies (no re-discovery) -- see discover.resume_stalled."""
    if run_id == "all":
        raise HTTPException(400, "'all' is not a real run and cannot be resumed")
    conn = db.connect()
    try:
        run_rows = conn.select("runs", run_id=f"eq.{run_id}", limit=1)
        if not run_rows:
            raise HTTPException(404, f"run {run_id!r} not found")
        stalled = db.stalled_companies(conn, run_id)
        if not stalled:
            raise HTTPException(400, "this run has nothing left to resume -- every company was already researched")
    finally:
        conn.close()

    job = _new_job(kind="discover", run_id=run_id)
    _MAIN_LOOP.run_in_executor(None, _run_resume_job, job, run_id, stalled)
    return {"job_id": job.job_id, "run_id": run_id, "stalled_count": len(stalled)}


def _delete_from_outreach(run_id: str) -> Optional[dict[str, Any]]:
    """Best-effort cascade of a run delete to the Outreach Agent, so any
    campaigns/prospects it imported from this run (see discover.py's
    _handoff_to_outreach) don't outlive it there. Mirrors that hand-off's own
    rule: returns None (skipped) if OUTREACH_API_BASE isn't configured, and
    turns a failure into a dict rather than raising -- the caller's own data
    has already been deleted successfully by the time this runs, and that
    must not be reported as failed just because Outreach is unreachable."""
    from .outreach_client import OutreachClient, OutreachClientError

    cfg = load_config()
    base = cfg.outreach_api_base
    if not base:
        return None
    client = OutreachClient(base)
    try:
        return client.delete_research_run(run_id)
    except OutreachClientError as exc:
        return {"error": str(exc)}
    finally:
        client.close()


@app.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict[str, Any]:
    """Deletes the run plus everything exclusively scoped to it -- companies
    only this run scored, and (via ON DELETE CASCADE) their job postings,
    contacts, saved-list entries, and suppression rows. A company also scored
    by another run is left untouched so that run's Companies tab keeps
    showing it. See db.delete_run's docstring for the exact rule.

    Also cascades the delete to the Outreach Agent -- see
    _delete_from_outreach."""
    if run_id == "all":
        raise HTTPException(400, "'all' is not a real run and cannot be deleted")
    conn = db.connect()
    try:
        removed = db.delete_run(conn, run_id)
    finally:
        conn.close()

    result: dict[str, Any] = {"status": "ok", "companies_removed": removed}
    outreach_result = _delete_from_outreach(run_id)
    if outreach_result is not None:
        result["outreach"] = outreach_result
    return result


@app.get("/runs/{run_id}/companies")
def run_companies(
    run_id: str,
    tier: Optional[str] = None,
    state: Optional[str] = None,
    industry: Optional[str] = None,
    min_score: Optional[float] = None,
    keyword: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Post-run filtering: tier, hq_state, industry substring, min score, and
    a keyword match against job titles/descriptions for this domain.
    run_id == 'all' shows every scored company once (its most recent score),
    across every run."""
    conn = db.connect()
    try:
        rows = db.latest_scores_per_domain(conn) if run_id == "all" else db.get_scores(conn, run_id)
        if tier:
            want = set(tier.upper())
            rows = [r for r in rows if r["tier"] in want]
        if min_score is not None:
            rows = [r for r in rows if r["score"] >= min_score]
        if industry:
            needle = industry.lower()
            rows = [r for r in rows if r.get("industry") and needle in r["industry"].lower()]

        if state or keyword:
            domains = [r["domain"] for r in rows]
            companies = db.companies_by_domain(conn, domains) if state else {}
            postings = db.postings_by_domain(conn, domains) if keyword else {}

            filtered = []
            for r in rows:
                if state:
                    company = companies.get(r["domain"])
                    if not company or not company.hq_state or company.hq_state.upper() != state.upper():
                        continue
                if keyword:
                    needle = keyword.lower()
                    if not any(needle in (p.title or "").lower() or needle in (p.description or "").lower()
                               for p in postings.get(r["domain"], [])):
                        continue
                filtered.append(r)
            rows = filtered

        return rows
    finally:
        conn.close()


@app.get("/runs/{run_id}/companies/{domain}")
def company_detail(run_id: str, domain: str) -> dict[str, Any]:
    """run_id == 'all' resolves to whichever run most recently scored this
    domain, same as the 'All research' Companies list."""
    conn = db.connect()
    try:
        company = db.get_company(conn, domain)
        if not company:
            raise HTTPException(404, f"{domain} not found")
        if run_id == "all":
            matches = [r for r in db.latest_scores_per_domain(conn) if r["domain"] == domain]
            effective_run_id = matches[0]["run_id"] if matches else None
        else:
            effective_run_id = run_id
        scores = [r for r in db.get_scores(conn, effective_run_id) if r["domain"] == domain] if effective_run_id else []
        postings = db.get_postings(conn, domain)
        qual = db.get_qualification(conn, domain, effective_run_id) if effective_run_id else None
        people = db.get_contacts(conn, domain)
        return {
            "company": company.model_dump(),
            "score": scores[0] if scores else None,
            "postings": [p.model_dump(mode="json") for p in postings],
            "qualification": qual,
            "contacts": people,
            # Single source of truth with the JSONL export (see
            # export.to_jsonl / Prospect.compute_outreach_flags): email is
            # required for outreach readiness, LinkedIn is a bonus signal.
            **Prospect.compute_outreach_flags(people),
        }
    finally:
        conn.close()


def _domains_scored_in(conn: db.SupabaseConn, run_id: str) -> Optional[set[str]]:
    """None means "no filtering" (run_id == 'all'); otherwise the set of
    domains scored in that run. saved_companies/contacts/suppression have no
    run_id column of their own (see schema.sql) -- every /runs/{run_id}/...
    route below filters them by intersecting with this set instead."""
    if run_id == "all":
        return None
    return {r["domain"] for r in db.get_scores(conn, run_id)}


# ----------------------------------------------------------------------------
# routes: saved companies -- nested under /runs/{run_id}/saved ('all' = every run)
# ----------------------------------------------------------------------------

@app.get("/runs/{run_id}/saved")
def list_saved(run_id: str) -> list[dict[str, Any]]:
    """Saved companies, enriched with company metadata and their most recent
    score (whichever run produced it) so the UI can render full cards."""
    conn = db.connect()
    try:
        saved = db.get_saved(conn)
        scoped = _domains_scored_in(conn, run_id)
        if scoped is not None:
            saved = [row for row in saved if row["domain"] in scoped]

        companies = db.companies_by_domain(conn, [row["domain"] for row in saved])
        # Group by saved_from_run so each distinct run is fetched once,
        # instead of once per saved company.
        scores_by_run: dict[str, dict[str, dict[str, Any]]] = {}
        for run in {row["saved_from_run"] for row in saved if row.get("saved_from_run")}:
            scores_by_run[run] = {r["domain"]: r for r in db.get_scores(conn, run)}

        out = []
        for row in saved:
            company = companies.get(row["domain"])
            saved_from_run = row.get("saved_from_run")
            score = scores_by_run.get(saved_from_run, {}).get(row["domain"]) if saved_from_run else None
            out.append({
                "domain": row["domain"],
                "saved_from_run": saved_from_run,
                "note": row.get("note"),
                "saved_at": row["saved_at"],
                "company": company.model_dump() if company else None,
                "score": score,
            })
        return out
    finally:
        conn.close()


@app.get("/runs/{run_id}/saved/domains")
def list_saved_domains(run_id: str) -> list[str]:
    """Just the domains -- what the companies table needs to render bookmark state."""
    conn = db.connect()
    try:
        domains = db.saved_domains(conn)
        scoped = _domains_scored_in(conn, run_id)
        if scoped is not None:
            domains &= scoped
        return sorted(domains)
    finally:
        conn.close()


@app.post("/runs/{run_id}/saved")
def save_company(run_id: str, req: SaveRequest) -> dict[str, str]:
    conn = db.connect()
    try:
        db.add_saved(conn, normalize_domain(req.domain), run_id if run_id != "all" else None, req.note)
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/runs/{run_id}/saved/{domain}")
def unsave_company(run_id: str, domain: str) -> dict[str, str]:
    conn = db.connect()
    try:
        db.remove_saved(conn, normalize_domain(domain))
        return {"status": "ok"}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: suppression -- nested under /runs/{run_id}/suppression for URL
# consistency; suppression itself is not run-scoped data (a do-not-contact
# list, see schema.sql), so run_id is accepted but never filters the list --
# only POST records it, purely as provenance of which run you suppressed from.
# ----------------------------------------------------------------------------

@app.get("/runs/{run_id}/suppression")
def list_suppression(run_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return db.list_suppression(conn)
    finally:
        conn.close()


@app.post("/runs/{run_id}/suppression")
def suppress_company(run_id: str, req: SuppressRequest) -> dict[str, str]:
    conn = db.connect()
    try:
        db.add_suppression(conn, normalize_domain(req.domain), req.reason)
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/runs/{run_id}/suppression/{domain}")
def unsuppress_company(run_id: str, domain: str) -> dict[str, str]:
    conn = db.connect()
    try:
        db.remove_suppression(conn, normalize_domain(domain))
        return {"status": "ok"}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: settings
# ----------------------------------------------------------------------------

def _icp_basics(cfg) -> dict[str, Any]:
    """Every ICP criterion the Settings page's Built-in ICP card displays.
    Only sweet_min/sweet_max (Employee Size Band) and revenue_sweet_min
    (Revenue Target) are actually editable from there -- see
    config.update_icp_basics for the write side and its docstring for why
    the rest (region, tiers, weights, practice areas, disqualifiers) stay
    file-only edits. Everything below is still returned so the card can show
    the complete picture, just read-only for those fields."""
    financials = cfg.raw.get("financials") or {}
    disqualifiers = cfg.raw.get("disqualifiers") or {}
    return {
        "active_region": cfg.active_region,
        "available_regions": [
            {"key": key, "label": (spec or {}).get("label") or key}
            for key, spec in cfg.regions.items()
        ],
        "sweet_min": int(cfg.size.get("sweet_min", 0)),
        "sweet_max": int(cfg.size.get("sweet_max", 0)),
        "hard_min": int(cfg.size.get("hard_min", 0)),
        "hard_max": int(cfg.size.get("hard_max", 0)),
        "revenue_sweet_min": financials.get("revenue_sweet_min"),
        "funding_preferred": bool(financials.get("funding_preferred")),
        "tier_a_threshold": cfg.tiers.get("A"),
        "tier_b_threshold": cfg.tiers.get("B"),
        "tier_c_threshold": cfg.tiers.get("C"),
        "target_industries": cfg.raw.get("target_industries") or [],
        "practice_areas": [a.key for a in cfg.enabled_areas],
        "require_us_presence": bool(cfg.geo.get("require_us_presence")),
        "min_relevant_reqs": disqualifiers.get("min_relevant_reqs"),
        "competitor_recruiter_req_share": disqualifiers.get("competitor_recruiter_req_share"),
    }


@app.get("/settings")
def get_settings() -> dict[str, Any]:
    """Read-only view of the scoring profile and which LLM/contact-discovery
    API keys are configured. Never returns real key values -- only whether
    each is set, so this is safe to expose over the local API with no auth."""
    cfg = load_config()

    def key_status(name: str) -> dict[str, Any]:
        value = os.environ.get(name)
        return {"name": name, "configured": bool(value), "masked": _mask(value) if value else None}

    return {
        "profile": cfg.raw.get("profile"),
        "weights": cfg.weights,
        "tiers": cfg.tiers,
        "icp_basics": _icp_basics(cfg),
        "api_keys": [
            key_status("ANTHROPIC_API_KEY"),
            key_status("GEMINI_API_KEY"),
            key_status("GROQ_API_KEY"),
            key_status("TAVILY_API_KEY"),
            key_status("HUNTER_API_KEY"),
            key_status("SUPABASE_URL"),
            key_status("SUPABASE_SERVICE_KEY"),
        ],
    }


class IcpBasicsUpdate(BaseModel):
    """Every field is optional -- only the ones provided are changed. These
    are the only two icp.yaml values the Settings page can edit (Employee
    Size Band and Revenue Target) -- see config.ICP_BASIC_FIELD_PATTERNS."""
    sweet_min: Optional[int] = None
    sweet_max: Optional[int] = None
    revenue_sweet_min: Optional[int] = None


@app.patch("/settings/icp")
def update_settings_icp(req: IcpBasicsUpdate) -> dict[str, Any]:
    """Edit a narrow set of safe icp.yaml values in place from the Settings
    page's Built-in ICP card -- see config.update_icp_basics. Only fields
    actually provided (non-None) are changed; the rest of icp.yaml, including
    every hand-written comment, is left byte-for-byte untouched."""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    try:
        cfg = update_icp_basics(fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok", "icp_basics": _icp_basics(cfg)}


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


# ----------------------------------------------------------------------------
# routes: settings > usage (Gemini/Groq/Tavily/Hunter.io quota tracking)
# ----------------------------------------------------------------------------

_USAGE_PROVIDERS = ("gemini", "groq", "tavily", "hunter")


def _empty_provider_summary() -> dict[str, Any]:
    return {
        "requests": 0, "successes": 0, "failures": 0, "retries": 0,
        "rate_limited_429": 0, "quota_exhausted": 0, "connection_errors": 0,
        "parse_errors": 0, "other_errors": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "avg_latency_ms": None,
    }


@app.get("/settings/usage")
def get_usage(hours: Optional[int] = None, since: Optional[str] = None) -> dict[str, Any]:
    """Usage/quota rollup for Gemini, Groq, Tavily, and Hunter.io, for the
    Settings > Usage tab. Reads api_usage rows written best-effort by
    usage_tracker.py -- this table is purely observational, so an
    empty/missing table just means "no data yet", never an error surfaced to
    the caller.

    Three ways to scope the window, matching the tab's filter:
    - `since` (an ISO timestamp, e.g. a run's started_at) -- used verbatim as
      the cutoff. This is how "Latest Research Run" is implemented: the
      frontend already has every run's started_at from listRuns(), so it
      passes the latest one's directly rather than this endpoint needing to
      know anything about runs.
    - `hours` alone -- cutoff is now-hours (e.g. 24*7 for "Last 7 Days").
    - neither -- "All Time", no lower time bound at all.
    `since` wins if both are somehow passed.

    Returns both a per-provider summary (for the tab's top-line cards) and a
    day-bucketed time series per provider (for the "usage over time" chart).
    """
    from datetime import datetime, timedelta, timezone

    if since is None and hours is not None:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    try:
        conn = db.connect()
        try:
            rows = db.api_usage_since(conn, since) if since else db.api_usage_all(conn)
        finally:
            conn.close()
    except db.SupabaseError:
        # api_usage may not exist yet on a database that hasn't run the
        # latest schema.sql -- degrade to "no usage data" rather than 500.
        rows = []

    summary: dict[str, dict[str, Any]] = {p: _empty_provider_summary() for p in _USAGE_PROVIDERS}
    daily: dict[str, dict[str, dict[str, int]]] = {p: {} for p in _USAGE_PROVIDERS}
    latency_totals: dict[str, list[int]] = {p: [] for p in _USAGE_PROVIDERS}

    for r in rows:
        provider = r.get("provider")
        if provider not in summary:
            continue
        s = summary[provider]
        s["requests"] += 1
        if r.get("success"):
            s["successes"] += 1
        else:
            s["failures"] += 1
            kind = r.get("error_kind") or "other"
            key = {
                "rate_limited": "rate_limited_429",
                "quota_exhausted": "quota_exhausted",
                "connection_error": "connection_errors",
                "parse_error": "parse_errors",
            }.get(kind, "other_errors")
            s[key] += 1
        s["retries"] += r.get("retry_count") or 0
        s["prompt_tokens"] += r.get("prompt_tokens") or 0
        s["completion_tokens"] += r.get("completion_tokens") or 0
        s["total_tokens"] += r.get("total_tokens") or 0
        if r.get("latency_ms") is not None:
            latency_totals[provider].append(r["latency_ms"])

        day = (r.get("created_at") or "")[:10]
        if day:
            bucket = daily[provider].setdefault(day, {"requests": 0, "failures": 0, "total_tokens": 0})
            bucket["requests"] += 1
            if not r.get("success"):
                bucket["failures"] += 1
            bucket["total_tokens"] += r.get("total_tokens") or 0

    for p in _USAGE_PROVIDERS:
        lat = latency_totals[p]
        if lat:
            summary[p]["avg_latency_ms"] = round(sum(lat) / len(lat))

    return {
        "window_hours": hours,
        "since": since,
        "providers": {
            p: {
                **summary[p],
                "daily": [
                    {"date": day, **counts}
                    for day, counts in sorted(daily[p].items())
                ],
            }
            for p in _USAGE_PROVIDERS
        },
    }


# ----------------------------------------------------------------------------
# routes: analytics
# ----------------------------------------------------------------------------

def _aggregate_analytics(rows: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """Shared aggregation for both the single-run and all-runs analytics
    routes -- same arithmetic, just given a different set of score rows."""
    tiers: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    industries: dict[str, dict[str, Any]] = {}
    providers: dict[str, int] = {}
    dimension_totals: dict[str, float] = {}
    dimension_counts: dict[str, int] = {}
    score_buckets = {f"{lo}-{lo + 9}": 0 for lo in range(0, 100, 10)}
    scatter = []

    for r in rows:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

        ind = r.get("industry") or "Unknown"
        bucket = industries.setdefault(ind, {"industry": ind, "count": 0, "score_sum": 0.0})
        bucket["count"] += 1
        bucket["score_sum"] += r["score"]

        provider = r.get("ats_provider") or "none"
        providers[provider] = providers.get(provider, 0) + 1

        for dim, val in (r.get("dimensions") or {}).items():
            dimension_totals[dim] = dimension_totals.get(dim, 0.0) + val
            dimension_counts[dim] = dimension_counts.get(dim, 0) + 1

        lo = min(int(r["score"] // 10) * 10, 90)
        score_buckets[f"{lo}-{lo + 9}"] += 1

        metrics = r.get("metrics") or {}
        reqs = metrics.get("req_count")
        if isinstance(reqs, (int, float)):
            scatter.append({
                "domain": r["domain"],
                "reqs": reqs,
                "score": r["score"],
                "tier": r["tier"],
                "median_age": metrics.get("median_age_days"),
            })

    top_industries = sorted(
        (
            {
                "industry": v["industry"],
                "count": v["count"],
                "avg_score": round(v["score_sum"] / v["count"], 1),
            }
            for v in industries.values()
        ),
        key=lambda x: x["count"],
        reverse=True,
    )[:8]

    return {
        "run_id": run_id,
        "total": len(rows),
        "avg_score": round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0,
        "tiers": [{"tier": k, "count": v} for k, v in sorted(tiers.items())],
        "industries": top_industries,
        "providers": [{"provider": k, "count": v} for k, v in sorted(providers.items(), key=lambda kv: -kv[1])],
        "dimensions": [
            {"dimension": k, "avg": round(dimension_totals[k] / dimension_counts[k], 2)}
            for k in sorted(dimension_totals)
        ],
        "score_distribution": [{"bucket": k, "count": v} for k, v in score_buckets.items()],
        "scatter": scatter,
    }


@app.get("/runs/{run_id}/analytics")
def run_analytics(run_id: str) -> dict[str, Any]:
    """Pre-aggregated chart data for the analytics page. run_id == 'all'
    aggregates across every run's scores combined."""
    conn = db.connect()
    try:
        rows = db.all_scores(conn) if run_id == "all" else db.get_scores(conn, run_id)
        return _aggregate_analytics(rows, run_id)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# routes: qualify / contacts -- nested under /runs/{run_id}/...
# ----------------------------------------------------------------------------

@app.post("/runs/{run_id}/qualify")
def start_qualify(run_id: str, req: QualifyRequest) -> dict[str, Any]:
    conn = db.connect()
    try:
        pending = qualify_mod.pending_rows(conn, run_id, req.tiers, force=req.force)
        if not pending:
            return {"job_id": None, "message": "nothing to qualify"}
        provider = qualify_mod.backend_name()
    except qualify_mod.QualificationError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()

    job = _new_job(kind="qualify", run_id=run_id)
    job.total = len(pending)
    _MAIN_LOOP.run_in_executor(None, _run_qualify_job, job, run_id, req)
    return {"job_id": job.job_id, "provider": provider, "companies": len(pending)}


@app.post("/runs/{run_id}/contacts")
def start_contacts(run_id: str, req: ContactsRequest) -> dict[str, Any]:
    conn = db.connect()
    try:
        rows = [r for r in db.get_scores(conn, run_id) if r["tier"] in req.tiers]
        domains = [r["domain"] for r in rows]
        pending = domains if req.force else [d for d in domains if not db.has_contacts(conn, d)]
        if not pending:
            return {"job_id": None, "message": "nothing to look up"}
    finally:
        conn.close()

    job = _new_job(kind="contacts", run_id=run_id)
    job.total = len(pending)
    _MAIN_LOOP.run_in_executor(None, _run_contacts_job, job, run_id, req)
    return {"job_id": job.job_id, "companies": len(pending)}


@app.get("/runs/{run_id}/contacts")
def list_contacts(run_id: str) -> list[dict[str, Any]]:
    """Every contact discovered so far -- what the Contacts page renders as a
    flat table. run_id == 'all' returns every contact across every run;
    otherwise filtered to domains scored in that run (contacts has no run_id
    column of its own -- a person's email doesn't change between runs)."""
    conn = db.connect()
    try:
        rows = db.all_contacts(conn)
        scoped = _domains_scored_in(conn, run_id)
        if scoped is not None:
            rows = [r for r in rows if r["domain"] in scoped]
        return rows
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
