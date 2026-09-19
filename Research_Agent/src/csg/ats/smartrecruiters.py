"""SmartRecruiters public postings API. No auth.

GET https://api.smartrecruiters.com/v1/companies/{token}/postings -- listing
GET https://api.smartrecruiters.com/v1/companies/{token}/postings/{id} -- detail

The listing endpoint has no description text (see fetch()'s per-posting
detail fetch, same tradeoff Workday's adapter makes).

IMPORTANT: unlike every other adapter, this API returns 200 with
totalFound=0/content=[] for ANY token, real company or not -- there is no
"not found" signal to probe against (confirmed live: a random nonsense
string returns the same shape as a real, empty-board company). Because of
that:
  - verify() requires totalFound > 0, not just a 200 -- a real company whose
    board is genuinely empty right now will be missed, which is an
    acceptable false-negative (we'll catch them once they post a req); the
    alternative (treating any 200 as a match) would false-positive on nearly
    every guessed token.
  - SmartRecruiters is deliberately excluded from ats/__init__.py's
    PROBEABLE_PROVIDERS -- even with the totalFound>0 guard, blind
    token-probing this API has no ground truth to rule out an accidental
    collision with an unrelated real tenant. It is only ever detected via a
    board link actually found on the company's own site (careers_page).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt, strip_html

BASE = "https://api.smartrecruiters.com/v1/companies"

# A company with 1000+ open reqs would mean 1000+ sequential detail calls --
# same mitigation Workday's adapter uses: cap how many get a detail fetch,
# run those concurrently (the shared Fetcher's per-host semaphore/delay still
# applies).
MAX_DETAIL_FETCHES = 60
DETAIL_CONCURRENCY = 8


def _list_url(token: str) -> str:
    return f"{BASE}/{token}/postings?limit=100"


def _detail_url(token: str, posting_id: str) -> str:
    return f"{BASE}/{token}/postings/{posting_id}"


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_list_url(token))
    return (
        status == 200
        and isinstance(data, dict)
        and isinstance(data.get("content"), list)
        and bool(data.get("content"))  # totalFound>0 -- see module docstring
    )


def _location(job: dict[str, Any]) -> Optional[str]:
    loc = job.get("location")
    if not isinstance(loc, dict):
        return None
    full = loc.get("fullLocation")
    if full:
        return str(full)
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    joined = ", ".join(str(p) for p in parts if p)
    return joined or None


def _description(detail: Optional[dict[str, Any]]) -> str:
    if not detail:
        return ""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        text = (sections.get(key) or {}).get("text")
        if text:
            parts.append(strip_html(text, limit=6000))
    return " ".join(parts)[:6000]


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_list_url(token))
    if status != 200 or not isinstance(data, dict):
        return []
    summaries = [j for j in (data.get("content") or []) if isinstance(j, dict) and j.get("id")]

    to_detail, rest = summaries[:MAX_DETAIL_FETCHES], summaries[MAX_DETAIL_FETCHES:]
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def get_detail(job: dict[str, Any]) -> Optional[dict[str, Any]]:
        async with sem:
            status, detail = await fetcher.get_json(_detail_url(token, str(job["id"])))
            return detail if status == 200 and isinstance(detail, dict) else None

    details = await asyncio.gather(*(get_detail(j) for j in to_detail))

    out: list[JobPosting] = []
    for job, detail in zip(to_detail, details):
        out.append(_to_job_posting(domain, job, detail))
    for job in rest:
        out.append(_to_job_posting(domain, job, None))
    return out


def _to_job_posting(domain: str, job: dict[str, Any], detail: Optional[dict[str, Any]]) -> JobPosting:
    detail = detail or job
    employment = (job.get("typeOfEmployment") or {}).get("label")
    return JobPosting(
        domain=domain,
        ats_provider="smartrecruiters",
        external_id=str(job["id"]),
        title=str(job.get("name") or ""),
        location=_location(job),
        department=(job.get("department") or {}).get("label"),
        employment_type=employment,
        url=detail.get("postingUrl") or first_of(job, "postingUrl"),
        posted_at=parse_dt(job.get("releasedDate")),
        description=_description(detail),
        raw={k: v for k, v in job.items() if k != "customField"},
    )
