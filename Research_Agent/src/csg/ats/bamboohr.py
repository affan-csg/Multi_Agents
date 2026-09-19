"""BambooHR public careers-list API. No auth.

GET https://{token}.bamboohr.com/careers/list  (Accept: application/json)

Confirmed live against a real tenant with open postings (e.g. "basdensteel").
Like Workable, the listing endpoint has no description text and no separate
detail API -- the per-job page (https://{token}.bamboohr.com/careers/{id}) is
server-rendered HTML, not JSON, so this adapter only reports the structured
listing fields (title/location/department/employment_type/url), same
tradeoff Workable's adapter makes. Unlike SmartRecruiters, a nonexistent
token reliably does NOT return this JSON shape (it 302s to bamboohr.com's
marketing site instead), so verify() can trust a clean signal and this is
safe to token-probe.
"""

from __future__ import annotations

from typing import Any, Optional

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of

BASE = "bamboohr.com"


def _url(token: str) -> str:
    return f"https://{token}.{BASE}/careers/list"


def _job_url(token: str, job_id: str) -> str:
    return f"https://{token}.{BASE}/careers/{job_id}"


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_url(token))
    return status == 200 and isinstance(data, dict) and isinstance(data.get("result"), list)


def _location(job: dict[str, Any]) -> Optional[str]:
    loc = job.get("location")
    if not isinstance(loc, dict):
        return None
    parts = [loc.get("city"), loc.get("state")]
    joined = ", ".join(str(p) for p in parts if p)
    return joined or None


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_url(token))
    if status != 200 or not isinstance(data, dict):
        return []

    out: list[JobPosting] = []
    for job in data.get("result", []) or []:
        if not isinstance(job, dict):
            continue
        ext_id = str(job.get("id") or "")
        if not ext_id:
            continue
        out.append(
            JobPosting(
                domain=domain,
                ats_provider="bamboohr",
                external_id=ext_id,
                title=str(job.get("jobOpeningName") or "").strip(),
                location=_location(job),
                department=first_of(job, "departmentLabel"),
                employment_type=job.get("employmentStatusLabel"),
                url=_job_url(token, ext_id),
                posted_at=None,
                description="",
                raw=job,
            )
        )
    return out
