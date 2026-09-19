"""Multi-level job discovery fallback.

ATS detection is one *method* of finding a company's open jobs, not a
requirement for scoring one. Before this module existed, `pipeline.py` did:

    ats.detect() -> found? fetch reqs : stop, company has zero job signal

An unsupported/undetectable ATS (an enterprise platform with no public API,
a JS-rendered careers page, a wrong/stale domain) collapsed straight into
"zero reqs", which four of scoring.py's six dimensions read as "no hiring
need" -- capping an otherwise strong company near D-tier for a data-source
gap, not a real quality signal (see ats/__init__.py's "Why this matters for
scoring" note). `discover_jobs()` is the fix: a bounded chain that keeps
trying cheaper, more reliable methods before falling through to a paid
external search, and represents total failure as UNKNOWN rather than a
silent zero.

    ATS  ->  careers-page job scrape  ->  external web search  ->  UNKNOWN

1. ATS (`ats.detect` + `ats.fetch_postings`, unchanged) -- highest
   precision, zero incremental cost (same HTTP fetches detection already
   made), exact structured data.
2. Careers-page scrape (`_scrape_careers_page_jobs`) -- when no ATS is
   found, re-scan the same careers-page HTML `ats.detect()` already fetched
   (or fetch it directly if detection never got that far) for job links/
   cards that don't match any known ATS pattern: plain "/careers/<slug>"
   detail links, or a JSON-LD `JobPosting` block many sites embed for SEO.
   Free (no paid API), lower structure than an ATS but still first-party.
3. External search (`_search_jobs_external`) -- Tavily, same provider/
   pattern as discovery's and enrichment's fallback (see
   discovery/groq_tavily.py, discovery/research.py). Paid/quota-metered, so
   it only runs once the free methods have both failed, and its result is
   openly weaker evidence (a search snippet, not a first-party page) --
   recorded as such via JobSource.
4. UNKNOWN -- every method above failed or found nothing. The company is
   NOT scored as having zero hiring need; `hiring_signal="unknown"` is
   recorded on the company row and scoring's existing missing-data handling
   (marks the dimension unavailable, rescales over what IS available -- see
   scoring.py's module docstring) takes it from there. This is exactly the
   same treatment a genuinely-empty ATS board already got; UNKNOWN just
   makes sure "we never looked" is never confused with "we looked and it
   was empty" in the stored provenance, even though both currently score
   the same way.

One failed method never aborts the chain -- an exception in any stage is
caught, classified via usage_tracker.classify_error, and logged as that
stage's failure reason before moving to the next one (see discover_jobs()).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import ats
from .ats._common import strip_html
from .discovery.groq_tavily import GroqTavilyProvider, _TAVILY_URL
from .http import Fetcher
from .models import Company, JobPosting
from .usage_tracker import classify_error, track

# Tavily's free tier is per-minute metered, same shape of transient failure
# as discovery/enrichment's own Tavily calls -- reuse the same retry budget.
_TAVILY_RATE_LIMIT_RETRIES = 2
_TAVILY_RATE_LIMIT_DEFAULT_WAIT = 5.0

# Transport-layer failures (DNS lookup, TCP connect, TLS handshake, read/pool
# timeout -- classify_error's "timeout"/"connection_error" buckets) are just
# as transient as a 429 but were previously not retried at all: one blip
# (e.g. Windows' `[Errno 11002] getaddrinfo failed`) would burn the ONLY
# fallback method left and drop a company straight to hiring_signal=unknown.
# Kept small and short since this is the paid, last-resort stage of the
# chain -- a real outage still fails fast rather than burning quota looping.
_TAVILY_CONNECTION_ERROR_RETRIES = 2
_TAVILY_CONNECTION_ERROR_BASE_WAIT = 2.0

# How many external search results to ask Tavily for. Small -- this is a
# fallback signal, not a primary source; keep the paid call cheap.
_SEARCH_MAX_RESULTS = 6


@dataclass
class JobDiscoveryResult:
    postings: list[JobPosting] = field(default_factory=list)
    method: str = "unknown"          # 'ats' | 'careers_page' | 'web_search' | 'unknown'
    source: Optional[str] = None     # ats_provider name, "careers page scrape", "tavily search"
    hiring_signal: str = "unknown"   # 'confirmed' | 'unknown'
    notes: Optional[str] = None      # populated when hiring_signal == 'unknown', or to record a
                                      # skipped/failed stage even on eventual success
    ats_provider: Optional[str] = None
    ats_token: Optional[str] = None
    ats_detected_by: Optional[str] = None
    careers_url: Optional[str] = None


# --------------------------------------------------------------------------
# stage 2: careers-page job scrape (no ATS found)
# --------------------------------------------------------------------------

# A same-domain link whose path looks like an individual job posting, not a
# listing/index page -- e.g. "/careers/senior-backend-engineer",
# "/jobs/12345-product-manager". Deliberately permissive: this only runs
# once ats.detect() has already failed to find a real ATS board, so a false
# positive here just yields a low-confidence careers_page posting, not a
# wrong ATS attribution.
_JOB_LINK_RE = re.compile(
    r'href=["\']([^"\']*/(?:careers?|jobs?|openings?|positions?)/[a-zA-Z0-9][a-zA-Z0-9_-]{3,}/?)["\']',
    re.I,
)
# Titles/slugs that are index pages, not a specific job -- skip these so the
# careers hub page itself never gets counted as a "job".
_NON_JOB_SLUGS = re.compile(
    r"^(careers?|jobs?|openings?|positions?|open-positions|current-openings|all-jobs|view-jobs)/?$", re.I,
)
_JSONLD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)


def _extract_jsonld_jobs(body: str) -> list[dict]:
    """Job postings from schema.org JobPosting JSON-LD -- a common SEO
    pattern (Google for Jobs) many career sites embed even without a
    third-party ATS. Best-effort: any block that isn't valid/relevant JSON
    is silently skipped, never raised."""
    jobs: list[dict] = []
    for m in _JSONLD_RE.finditer(body):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                jobs.append(item)
    return jobs


def _jsonld_to_posting(domain: str, item: dict, url: str) -> Optional[JobPosting]:
    title = item.get("title")
    if not title:
        return None
    loc = None
    jl = item.get("jobLocation")
    if isinstance(jl, dict):
        addr = jl.get("address")
        if isinstance(addr, dict):
            loc = ", ".join(str(v) for v in (addr.get("addressLocality"), addr.get("addressRegion")) if v)
    elif isinstance(jl, list) and jl:
        first = jl[0]
        if isinstance(first, dict):
            addr = first.get("address")
            if isinstance(addr, dict):
                loc = ", ".join(str(v) for v in (addr.get("addressLocality"), addr.get("addressRegion")) if v)

    external_id = item.get("identifier")
    if isinstance(external_id, dict):
        external_id = external_id.get("value")
    if not external_id:
        external_id = f"jsonld:{abs(hash((title, url)))}"

    return JobPosting(
        domain=domain, ats_provider=None, source="careers_page",
        external_id=str(external_id), title=str(title),
        location=loc, department=item.get("hiringOrganization", {}).get("name")
        if isinstance(item.get("hiringOrganization"), dict) else None,
        employment_type=item.get("employmentType") if isinstance(item.get("employmentType"), str) else None,
        url=item.get("url") or url,
        description=strip_html(item.get("description")),
    )


def _extract_job_links(base_url: str, body: str) -> list[str]:
    domain = re.sub(r"^https?://", "", base_url).split("/")[0]
    out: list[str] = []
    seen: set[str] = set()
    for href in _JOB_LINK_RE.findall(body):
        path = href.split("?")[0].rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        if _NON_JOB_SLUGS.match(slug):
            continue
        url = href if "://" in href else f"https://{domain}{href if href.startswith('/') else '/' + href}"
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _link_to_posting(domain: str, url: str) -> JobPosting:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    title = re.sub(r"[-_]+", " ", slug).strip().title()
    return JobPosting(
        domain=domain, ats_provider=None, source="careers_page",
        external_id=f"link:{slug}", title=title or "Open position",
        url=url, description="",
    )


async def _scrape_careers_page_jobs(
    fetcher: Fetcher, domain: str, careers_url: Optional[str]
) -> tuple[list[JobPosting], Optional[str]]:
    """Look for job postings directly on the company's careers page(s) when
    no ATS board was found there. Returns (postings, error) -- error is set
    only on an actual fetch/parse problem, never for "page had no jobs"
    (that is just an empty list, a valid outcome, not a failure).
    """
    urls = [careers_url] if careers_url else [f"https://{domain}{p}" for p in ats.CAREERS_PATHS[:4]]
    postings: list[JobPosting] = []
    seen_ids: set[str] = set()
    fetched_any = False

    for url in urls:
        try:
            status, body = await fetcher.get(url)
        except Exception as exc:
            return postings, f"careers page fetch error ({classify_error(exc)}): {exc}"
        if status != 200 or not body:
            continue
        fetched_any = True

        for item in _extract_jsonld_jobs(body):
            p = _jsonld_to_posting(domain, item, url)
            if p and p.external_id not in seen_ids:
                seen_ids.add(p.external_id)
                postings.append(p)

        if not postings:
            for link in _extract_job_links(url, body):
                p = _link_to_posting(domain, link)
                if p.external_id not in seen_ids:
                    seen_ids.add(p.external_id)
                    postings.append(p)

        if postings:
            break

    if not fetched_any:
        return postings, "no careers page reachable (all candidate paths 404/unreachable)"
    return postings, None


# --------------------------------------------------------------------------
# stage 3: external web search (Tavily)
# --------------------------------------------------------------------------

def _tavily_post_with_connection_retry(u, api_key: str, query: str) -> httpx.Response:
    """POST to Tavily, retrying only transport-layer failures (DNS lookup,
    TCP connect, TLS handshake, read/pool timeout -- classify_error's
    "timeout"/"connection_error" buckets). Kept as its own bounded retry
    loop, separate from the 429 loop in _tavily_search_sync, so a run of
    connection errors can't eat into (or be starved by) the rate-limit
    retry budget -- each failure mode gets its own small allowance.
    """
    for attempt in range(_TAVILY_CONNECTION_ERROR_RETRIES + 1):
        try:
            with httpx.Client() as client:
                return client.post(_TAVILY_URL, json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": _SEARCH_MAX_RESULTS,
                    "search_depth": "basic",
                }, timeout=20.0)
        except httpx.HTTPError as exc:
            # e.g. Windows' `[Errno 11002] getaddrinfo failed`, a reset
            # connection, or a connect/read timeout -- transient transport
            # noise, not "Tavily said no". A real, sustained outage still
            # fails fast: after the last attempt this re-raises.
            kind = classify_error(exc)
            if kind not in ("timeout", "connection_error") or attempt >= _TAVILY_CONNECTION_ERROR_RETRIES:
                raise
            u.retry_count += 1
            wait = _TAVILY_CONNECTION_ERROR_BASE_WAIT * (2 ** attempt)
            print(f"  [jobs_discovery] Tavily {kind}, retrying in {wait:.0f}s "
                  f"({attempt + 1}/{_TAVILY_CONNECTION_ERROR_RETRIES})")
            time.sleep(wait)
    raise AssertionError("unreachable")  # loop always returns or raises


def _tavily_search_sync(query: str, api_key: str) -> list[dict]:
    with track("tavily", "job_search") as u:
        for attempt in range(_TAVILY_RATE_LIMIT_RETRIES + 1):
            r = _tavily_post_with_connection_retry(u, api_key, query)
            if r.status_code == 429 and attempt < _TAVILY_RATE_LIMIT_RETRIES:
                u.retry_count += 1
                wait = GroqTavilyProvider._parse_retry_after(r) or _TAVILY_RATE_LIMIT_DEFAULT_WAIT
                print(f"  [jobs_discovery] Tavily rate limited, waiting {wait:.0f}s "
                      f"({attempt + 1}/{_TAVILY_RATE_LIMIT_RETRIES})")
                time.sleep(wait)
                continue
            if r.status_code == 429:
                u.mark_failure(status_code=429, error_kind="rate_limited")
            r.raise_for_status()
            u.status_code = r.status_code
            return r.json().get("results", [])
    return []


async def _search_jobs_external(domain: str, name: Optional[str]) -> tuple[list[JobPosting], Optional[str]]:
    """Tavily search for recent job postings when the company's own site
    exposed nothing usable. Weakest-evidence source in the chain -- a search
    snippet, not a first-party page -- recorded via JobSource='web_search'
    so scoring/UI can tell it apart from a directly-confirmed posting.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return [], "TAVILY_API_KEY not set -- external search fallback unavailable"

    label = name or domain.split(".")[0]
    query = f'"{label}" jobs OR careers OR hiring site:{domain} OR "{label}" job openings'

    try:
        results = await asyncio.to_thread(_tavily_search_sync, query, api_key)
    except Exception as exc:
        return [], f"external job search failed ({classify_error(exc)}): {exc}"

    postings: list[JobPosting] = []
    for i, r in enumerate(results):
        title = (r.get("title") or "").strip()
        url = r.get("url") or ""
        if not title:
            continue
        # A plain "Careers at Acme" / "Acme - LinkedIn" search-result title
        # is not evidence of a specific open role -- skip results that don't
        # look like an actual job posting page, so this fallback records
        # weak-but-real signal, not noise.
        if not re.search(r"\b(hiring|job|career|opening|position|apply)\b", title, re.I):
            continue
        postings.append(JobPosting(
            domain=domain, ats_provider=None, source="web_search",
            external_id=f"search:{abs(hash(url or title))}",
            title=title[:200], url=url or None,
            description=strip_html(r.get("content"), limit=1000),
        ))
    return postings, None


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

async def discover_jobs(
    fetcher: Fetcher, company: Company, ats_result: Optional[tuple],
) -> JobDiscoveryResult:
    """Run the full ATS -> careers_page -> web_search -> UNKNOWN chain.

    `ats_result` is whatever `ats.detect()` already returned for this company
    (or the cached provider/token from a prior run) -- this function does not
    re-run detection, it decides what to do once detection has already
    succeeded or come up empty, and fetches ATS postings itself when a
    provider was resolved.
    """
    domain = company.domain
    notes: list[str] = []

    # --- 1. ATS ---
    if ats_result:
        provider, token, detected_by, careers_url = ats_result
        try:
            postings = await ats.fetch_postings(fetcher, domain, provider, token)
        except Exception as exc:
            notes.append(f"ATS fetch failed ({classify_error(exc)}): {exc}; continuing to careers-page fallback")
        else:
            for p in postings:
                p.source = "ats"
            print(f"[jobs_discovery] {domain}: ATS detected ({provider}) -> {len(postings)} jobs found")
            return JobDiscoveryResult(
                postings=postings, method="ats", source=provider, hiring_signal="confirmed",
                ats_provider=provider, ats_token=token, ats_detected_by=detected_by, careers_url=careers_url,
                notes=None,
            )
        careers_url_for_scrape = careers_url
    else:
        careers_url_for_scrape = None
        notes.append("ATS not detected")

    # --- 2. careers-page scrape ---
    try:
        postings, err = await _scrape_careers_page_jobs(fetcher, domain, careers_url_for_scrape)
    except Exception as exc:
        postings, err = [], f"careers page scrape crashed ({classify_error(exc)}): {exc}"

    if postings:
        print(f"[jobs_discovery] {domain}: No ATS detected -> Careers page -> {len(postings)} jobs found")
        return JobDiscoveryResult(
            postings=postings, method="careers_page", source="careers page scrape",
            hiring_signal="confirmed",
            ats_provider=(ats_result[0] if ats_result else None),
            ats_token=(ats_result[1] if ats_result else None),
            ats_detected_by=(ats_result[2] if ats_result else None),
            careers_url=careers_url_for_scrape,
            notes="; ".join(notes) or None,
        )
    if err:
        notes.append(err)
    else:
        # Page(s) were reachable but had nothing recognizable as a job --
        # real evidence of an empty/unreadable careers page, not a failure.
        notes.append("careers page reachable but no jobs found")

    # --- 3. external search ---
    try:
        postings, err = await _search_jobs_external(domain, company.name)
    except Exception as exc:
        postings, err = [], f"external search crashed ({classify_error(exc)}): {exc}"

    if postings:
        print(f"[jobs_discovery] {domain}: No ATS -> Careers page empty -> Web search -> {len(postings)} jobs found")
        return JobDiscoveryResult(
            postings=postings, method="web_search", source="tavily search",
            hiring_signal="confirmed",
            ats_provider=(ats_result[0] if ats_result else None),
            ats_token=(ats_result[1] if ats_result else None),
            ats_detected_by=(ats_result[2] if ats_result else None),
            careers_url=careers_url_for_scrape,
            notes="; ".join(notes) or None,
        )
    if err:
        notes.append(err)
    else:
        notes.append("external search found no job-like results")

    # --- 4. every method exhausted -- UNKNOWN, not zero ---
    print(f"[jobs_discovery] {domain}: All methods exhausted -> Job data unknown ({'; '.join(notes)})")
    return JobDiscoveryResult(
        postings=[], method="unknown", source=None, hiring_signal="unknown",
        ats_provider=(ats_result[0] if ats_result else None),
        ats_token=(ats_result[1] if ats_result else None),
        ats_detected_by=(ats_result[2] if ats_result else None),
        careers_url=careers_url_for_scrape,
        notes="; ".join(notes),
    )
