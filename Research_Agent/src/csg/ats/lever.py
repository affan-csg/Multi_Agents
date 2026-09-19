"""Lever public postings API. No auth.

GET https://api.lever.co/v0/postings/{token}?mode=json
"""

from __future__ import annotations

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt, strip_html

BASE = "https://api.lever.co/v0/postings"


def _url(token: str) -> str:
    return f"{BASE}/{token}?mode=json"


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, data = await fetcher.get_json(_url(token))
    return status == 200 and isinstance(data, list)


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, data = await fetcher.get_json(_url(token))
    if status != 200 or not isinstance(data, list):
        return []

    out: list[JobPosting] = []
    for job in data:
        if not isinstance(job, dict):
            continue
        cats = job.get("categories") or {}
        if not isinstance(cats, dict):
            cats = {}

        ext_id = str(job.get("id") or "")
        if not ext_id:
            continue

        description = job.get("descriptionPlain") or strip_html(job.get("description"))

        out.append(
            JobPosting(
                domain=domain,
                ats_provider="lever",
                external_id=ext_id,
                title=str(first_of(job, "text", "title") or ""),
                location=cats.get("location"),
                department=first_of(cats, "department", "team"),
                employment_type=cats.get("commitment"),
                url=first_of(job, "hostedUrl", "applyUrl"),
                posted_at=parse_dt(first_of(job, "createdAt", "publishedAt")),
                description=str(description or "")[:6000],
                raw={k: v for k, v in job.items() if k not in ("description", "descriptionPlain", "lists")},
            )
        )
    return out
