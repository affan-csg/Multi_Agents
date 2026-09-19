"""Greenhouse public job board API. No auth.

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt, strip_html

BASE = "https://boards-api.greenhouse.io/v1/boards"


def _url(token: str, content: bool = False) -> str:
    return f"{BASE}/{token}/jobs" + ("?content=true" if content else "")


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_url(token))
    return status == 200 and isinstance(data, dict) and isinstance(data.get("jobs"), list)


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_url(token, content=True))
    if status != 200 or not isinstance(data, dict):
        return []

    out: list[JobPosting] = []
    for job in data.get("jobs", []) or []:
        if not isinstance(job, dict):
            continue
        loc = job.get("location")
        location = loc.get("name") if isinstance(loc, dict) else (loc if isinstance(loc, str) else None)

        departments = job.get("departments") or []
        dept = None
        if departments and isinstance(departments[0], dict):
            dept = departments[0].get("name")

        posted = parse_dt(first_of(job, "first_published", "updated_at"))
        ext_id = str(first_of(job, "id", "internal_job_id") or "")
        if not ext_id:
            continue

        out.append(
            JobPosting(
                domain=domain,
                ats_provider="greenhouse",
                external_id=ext_id,
                title=str(job.get("title") or ""),
                location=location,
                department=dept,
                employment_type=None,
                url=job.get("absolute_url"),
                posted_at=posted,
                description=strip_html(job.get("content")),
                raw=_slim(job),
            )
        )
    return out


def _slim(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k != "content"}
