"""Contact discovery via Hunter.io's Domain Search API.

Free tier is a hard 25 searches/MONTH (confirmed against a live account) --
one call per domain, not per contact, so this is the tightest quota anywhere
in the pipeline. A domain is looked up at most once ever (see db.has_contacts)
unless --force is passed. There is no retry-on-quota-exhaustion here the way
qualify.py retries transient errors: a monthly quota is not worth backing off
for within a single run, so a 429 fails immediately with Hunter's own message.

Queries scope to department=hr (recruiting/people-ops/talent-acquisition
titles) plus seniority=senior,executive -- the roles a staffing firm's
outreach would actually target, not e.g. random engineers.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .usage_tracker import track

if TYPE_CHECKING:
    from .db import SupabaseConn

BASE_URL = "https://api.hunter.io/v2/domain-search"
DEPARTMENT = "hr"
SENIORITY = "senior,executive"
DEFAULT_LIMIT = 5


class ContactDiscoveryError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        raise ContactDiscoveryError(
            "HUNTER_API_KEY is not set. Add it to .env (see .env.example) before running "
            "`csg contacts`."
        )
    return key


def _parse_emails(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in data.get("data", {}).get("emails", []) or []:
        if not isinstance(e, dict) or not e.get("value"):
            continue
        out.append({
            "email": e["value"],
            "first_name": e.get("first_name"),
            "last_name": e.get("last_name"),
            "position": e.get("position") or e.get("position_raw"),
            "seniority": e.get("seniority"),
            "department": e.get("department"),
            "linkedin_url": e.get("linkedin"),
            "confidence": e.get("confidence"),
            "verification": (e.get("verification") or {}).get("status"),
        })
    return out


async def _search(domain: str, api_key: str, limit: int, department: Optional[str], seniority: Optional[str]) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"domain": domain, "api_key": api_key, "limit": limit}
    if department:
        params["department"] = department
    if seniority:
        params["seniority"] = seniority
    with track("hunter", "domain_search", domain=domain) as u:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(BASE_URL, params=params)
        if resp.status_code == 429:
            # Hunter's free tier is a hard 25/month, not a per-minute window --
            # a 429 here means "exhausted for the month," not "back off and
            # retry," so this is tagged quota_exhausted rather than
            # rate_limited (contrast with Tavily/Groq's per-minute 429s).
            u.mark_failure(status_code=429, error_kind="quota_exhausted")
            raise ContactDiscoveryError(
                f"Hunter.io rate/quota limit hit for {domain}: {resp.json().get('errors', resp.text)}"
            )
        resp.raise_for_status()
        u.status_code = resp.status_code
        return _parse_emails(resp.json())


async def find_contacts(domain: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """One or two Hunter.io Domain Search calls for `domain`. Raises
    ContactDiscoveryError on missing key; raises httpx errors as-is on
    network/HTTP failure so the caller sees exactly what Hunter reported
    (rate limit, invalid key, etc).

    Hunter's department=hr filter only matches companies with a dedicated
    HR/recruiting title -- smaller prospects route hiring through whoever's
    running the place (CEO, Head of Sales, ...) and Hunter never tags those
    as "hr", so the filtered search comes back empty even when the domain
    has plenty of Hunter data (seen live for peregrine.io: 10 executives on
    file, 0 tagged department=hr). When the HR-scoped search is empty, fall
    back to an executive/senior-only search with no department filter so a
    small prospect still yields a legitimate outreach target instead of
    silently producing zero contacts.
    """
    api_key = _api_key()
    hits = await _search(domain, api_key, limit, DEPARTMENT, SENIORITY)
    if hits:
        return hits
    return await _search(domain, api_key, limit, None, SENIORITY)


async def discover_run(
    conn: "SupabaseConn",
    domains: list[str],
    limit_per_domain: int = DEFAULT_LIMIT,
    force: bool = False,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Look up contacts for each domain not already in the `contacts` table
    (unless `force`). Serial by design -- Hunter's monthly quota is too tight
    to parallelize, and a burst adds no value when the cap is 25/month.

    Returns {"found": n, "skipped": n, "errors": n, "error_details": {domain: str},
    "contacts_saved": n}. Stops immediately (does not attempt remaining
    domains) on the first quota/rate-limit error, since that error will repeat
    for every subsequent domain in the same run.
    """
    from . import db

    pending = domains if force else [d for d in domains if not db.has_contacts(conn, d)]
    stats: dict[str, Any] = {
        "found": 0, "skipped": len(domains) - len(pending), "errors": 0,
        "error_details": {}, "contacts_saved": 0,
    }

    for i, domain in enumerate(pending):
        try:
            contacts = await find_contacts(domain, limit=limit_per_domain)
            n = db.save_contacts(conn, domain, contacts, source="hunter")
            conn.commit()
            stats["found"] += 1
            stats["contacts_saved"] += n
        except ContactDiscoveryError as exc:
            stats["errors"] += 1
            stats["error_details"][domain] = str(exc)
            if on_progress:
                on_progress(i + 1, len(pending), domain)
            break  # quota errors won't clear mid-run; stop burning attempts
        except httpx.HTTPStatusError as exc:
            stats["errors"] += 1
            stats["error_details"][domain] = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        if on_progress:
            on_progress(i + 1, len(pending), domain)

    return stats
