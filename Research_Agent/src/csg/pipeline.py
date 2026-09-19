"""The research funnel: companies -> detect ATS -> fetch reqs -> score.

Takes the companies discovery found (see discover.py) and researches each one.
Stages are idempotent. Company status is the checkpoint: a re-run skips
detection for anything already resolved, and the HTTP cache makes re-fetching
free. Kill it mid-run and restart without losing work.

Job discovery is a fallback chain, not a single ATS lookup -- see
jobs_discovery.py's module docstring for the full ATS -> careers_page ->
web_search -> UNKNOWN chain and why an unsupported/undetectable ATS must
never collapse into "zero jobs". `no_ats` (below) now means specifically
"no ATS board found"; it does NOT mean "no job signal" any more -- a company
in that bucket may still have postings from careers_page/web_search, and
even a company where every method failed gets `hiring_signal="unknown"`,
not a confirmed zero.

Every company that reaches scoring gets a score row for this run, whether or
not any job signal was found -- postings-driven dimensions (hiring need,
fill pain, buying signals, delivery fit) simply score as unavailable and get
rescaled out, same treatment scoring.py already gives any other missing
input (see scoring.py's module docstring). A company still ends up in the
pipeline/Companies tab even with a thin, low-tier score, rather than
silently vanishing before a score row is ever written.

Statuses:
  discovered  -- found by discovery, nothing researched yet
  no_ats      -- no ATS board found AND every fallback method also failed
                 (hiring_signal="unknown"; still scored)
  ats_found   -- confirmed job postings exist, from ANY source (ats,
                 careers_page, or web_search -- see company.job_source for
                 which); the name predates the fallback chain but the status
                 itself has always just meant "this company has reqs"
  no_reqs     -- a job source was read (ATS or careers page) but had zero
                 open reqs, OR no ATS was found but a fallback method still
                 ran to completion with nothing (still scored either way)
  suppressed  -- on the suppression list
  disqualified-- failed a hard filter (size/geo/competitor -- not "no reqs")
  scored      -- has a score row for this run
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import ats, db, delivery
from .config import Config
from .db import SupabaseConn
from .http import Fetcher
from .jobs_discovery import discover_jobs
from .models import Company, ScoreResult
from .scoring import evaluate


@dataclass
class Stats:
    companies: int = 0
    suppressed: int = 0
    ats_found: int = 0
    no_ats: int = 0
    no_reqs: int = 0
    reqs_fetched: int = 0
    disqualified: int = 0
    scored: int = 0
    tiers: dict[str, int] = field(default_factory=lambda: {"A": 0, "B": 0, "C": 0, "D": 0})
    dq_reasons: dict[str, int] = field(default_factory=dict)
    detected_by: dict[str, int] = field(default_factory=dict)
    providers: dict[str, int] = field(default_factory=dict)
    # Job discovery fallback usage -- how many companies landed on each
    # method, and how many ended up with no confirmed signal at all. See
    # jobs_discovery.py's chain; surfaced in the run summary so a run
    # leaning heavily on fallback methods (or UNKNOWN) is visible, not
    # silently indistinguishable from one where ATS coverage was strong.
    job_discovery_methods: dict[str, int] = field(default_factory=dict)
    hiring_signal_unknown: int = 0


async def _process_one(
    fetcher: Fetcher, conn: SupabaseConn, cfg: Config, company: Company,
    stats: Stats, now: datetime, run_id: str, force: bool,
) -> None:
    existing = db.get_company(conn, company.domain)

    # --- stage 1: detect ATS (unchanged -- cheap, cached, highest precision) ---
    ats_result: Optional[tuple] = None
    if existing and existing.ats_provider and not force:
        ats_result = (existing.ats_provider, existing.ats_token, existing.ats_detected_by, existing.careers_url)
    else:
        ats_result = await ats.detect(fetcher, company.domain, company.name)

    if not ats_result:
        stats.no_ats += 1

    # --- stages 2-4 of the fallback chain: careers_page -> web_search -> UNKNOWN.
    # discover_jobs() re-fetches ATS postings itself when ats_result is set, and
    # never lets one stage's failure abort the rest -- see jobs_discovery.py. ---
    job_result = await discover_jobs(fetcher, company, ats_result)

    company.ats_provider = job_result.ats_provider
    company.ats_token = job_result.ats_token
    company.ats_detected_by = job_result.ats_detected_by
    company.careers_url = job_result.careers_url
    company.job_discovery_method = job_result.method
    company.job_source = job_result.source
    company.hiring_signal = job_result.hiring_signal
    company.job_discovery_notes = job_result.notes

    stats.job_discovery_methods[job_result.method] = stats.job_discovery_methods.get(job_result.method, 0) + 1
    if job_result.hiring_signal == "unknown":
        stats.hiring_signal_unknown += 1

    postings = job_result.postings
    if job_result.method == "ats":
        stats.ats_found += 1
        stats.detected_by[company.ats_detected_by or "?"] = stats.detected_by.get(company.ats_detected_by or "?", 0) + 1
        stats.providers[company.ats_provider] = stats.providers.get(company.ats_provider, 0) + 1

    if postings:
        for p in postings:
            keys = cfg.classify(p.title)
            p.practice_area = keys[0] if keys else None
            p.delivery_fit = delivery.classify(p.title, p.description, p.location or "")
        db.upsert_postings(conn, postings)
        stats.reqs_fetched += len(postings)

    if job_result.hiring_signal == "unknown":
        company.status = "no_ats" if not ats_result else "no_reqs"
    elif not postings:
        company.status = "no_reqs"
        stats.no_reqs += 1
    else:
        company.status = "ats_found"

    db.upsert_company(conn, company)
    conn.commit()

    # Re-read so first_seen (our own age fallback) is populated.
    stored = db.get_postings(conn, company.domain)

    # --- stages 3+4: filter and score ---
    result: ScoreResult = evaluate(cfg, company, stored, now)
    db.save_score(conn, run_id, result)

    if result.disqualified:
        company.status = "disqualified"
        company.dq_reason = result.dq_reason
        db.upsert_company(conn, company)
        stats.disqualified += 1
        key = (result.dq_reason or "").split("(")[0].strip()
        stats.dq_reasons[key] = stats.dq_reasons.get(key, 0) + 1
    else:
        company.status = "scored"
        db.upsert_company(conn, company)
        stats.scored += 1
        stats.tiers[result.tier] += 1

    # Contact discovery deliberately does NOT run here. Hunter's free tier is a
    # hard 25 domain searches per MONTH, and this function runs concurrently
    # under asyncio.gather -- calling it per company would fire parallel
    # requests with no shared quota accounting and could burn a month of quota
    # in one run. It is a separate, serial, quota-aware stage:
    # contacts.discover_run(), invoked by `csg contacts` / the /contacts route.

    conn.commit()


async def run(
    cfg: Config,
    companies: list[Company],
    conn: SupabaseConn,
    run_id: str,
    limit: Optional[int] = None,
    force: bool = False,
    use_cache: bool = True,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    label: Optional[str] = None,
) -> Stats:
    if limit:
        companies = companies[:limit]

    stats = Stats(companies=len(companies))
    suppressed = db.load_suppression(conn)
    now = db.utcnow()
    h = cfg.http

    db.start_run(conn, run_id, cfg.raw.get("profile", "default"), len(companies), label=label)

    work = []
    for c in companies:
        if c.domain in suppressed:
            c.status = "suppressed"
            db.upsert_company(conn, c)
            stats.suppressed += 1
            continue
        db.upsert_company(conn, c)
        work.append(c)
    conn.commit()

    async with Fetcher(
        user_agent=h["user_agent"],
        per_host_delay=float(h["per_host_delay_seconds"]),
        timeout=float(h["timeout_seconds"]),
        cache_ttl_hours=float(h["cache_ttl_hours"]),
        max_concurrency=int(h["max_concurrency"]),
        use_cache=use_cache,
    ) as fetcher:
        sem = asyncio.Semaphore(int(h["max_concurrency"]))
        done = 0

        async def guarded(c: Company) -> None:
            nonlocal done
            async with sem:
                try:
                    await _process_one(fetcher, conn, cfg, c, stats, now, run_id, force)
                except Exception as exc:  # keep one bad domain from killing the run
                    db.set_status(conn, c.domain, "error", f"{type(exc).__name__}: {exc}")
                    conn.commit()
                done += 1
                if on_progress:
                    on_progress(done, len(work), c.domain)

        await asyncio.gather(*(guarded(c) for c in work))

    db.finish_run(conn, run_id, notes=f"cache hits={fetcher.stats['hits']} misses={fetcher.stats['misses']}")
    return stats
