"""Workable public widget API. No auth.

GET https://apply.workable.com/api/v1/widget/accounts/{token}

Confirmed live against a real tenant with open postings (e.g. "zoopla").
Unlike Greenhouse/Lever/Ashby/SmartRecruiters, the widget response has no
separate per-job detail endpoint -- description text is simply not available
through this API, only structured fields (title/location/department/
employment_type/url/posted_at). That is a real gap in what we can report for
a Workable-hosted company, not a bug: there is nowhere else public to get it
from without rendering the JS-driven apply page.
"""

from __future__ import annotations

from typing import Any, Optional

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt

BASE = "https://apply.workable.com/api/v1/widget/accounts"


def _url(token: str) -> str:
    return f"{BASE}/{token}"


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_url(token))
    return status == 200 and isinstance(data, dict) and isinstance(data.get("jobs"), list)


def _location(job: dict[str, Any]) -> Optional[str]:
    parts = [job.get("city"), job.get("state"), job.get("country")]
    joined = ", ".join(str(p) for p in parts if p)
    return joined or None


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_url(token))
    if status != 200 or not isinstance(data, dict):
        return []

    out: list[JobPosting] = []
    for job in data.get("jobs", []) or []:
        if not isinstance(job, dict):
            continue
        ext_id = str(first_of(job, "shortcode", "code") or "")
        if not ext_id:
            continue
        out.append(
            JobPosting(
                domain=domain,
                ats_provider="workable",
                external_id=ext_id,
                title=str(job.get("title") or ""),
                location=_location(job),
                department=job.get("department"),
                employment_type=job.get("employment_type"),
                url=first_of(job, "url", "shortlink"),
                posted_at=parse_dt(first_of(job, "published_on", "created_at")),
                description="",
                raw=job,
            )
        )
    return out
