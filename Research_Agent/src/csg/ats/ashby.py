"""Ashby public job board API. No auth.

GET https://api.ashbyhq.com/posting-api/job-board/{token}
"""

from __future__ import annotations

from typing import Any

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt, strip_html

BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _url(token: str) -> str:
    return f"{BASE}/{token}"


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_url(token))
    return status == 200 and isinstance(data, dict) and isinstance(data.get("jobs"), list)


def _location(job: dict[str, Any]) -> str | None:
    loc = job.get("location")
    if isinstance(loc, str) and loc:
        return loc
    addr = job.get("address")
    if isinstance(addr, dict):
        pa = addr.get("postalAddress")
        if isinstance(pa, dict):
            parts = [pa.get("addressLocality"), pa.get("addressRegion"), pa.get("addressCountry")]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined
    return None


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_url(token))
    if status != 200 or not isinstance(data, dict):
        return []

    out: list[JobPosting] = []
    for job in data.get("jobs", []) or []:
        if not isinstance(job, dict):
            continue
        if job.get("isListed") is False:
            continue

        ext_id = str(first_of(job, "id", "jobId") or "")
        if not ext_id:
            continue

        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml"))

        out.append(
            JobPosting(
                domain=domain,
                ats_provider="ashby",
                external_id=ext_id,
                title=str(job.get("title") or ""),
                location=_location(job),
                department=first_of(job, "department", "team"),
                employment_type=job.get("employmentType"),
                url=first_of(job, "jobUrl", "applyUrl"),
                posted_at=parse_dt(first_of(job, "publishedAt", "updatedAt")),
                description=str(description or "")[:6000],
                raw={k: v for k, v in job.items()
                     if k not in ("descriptionHtml", "descriptionPlain", "compensation")},
            )
        )
    return out
