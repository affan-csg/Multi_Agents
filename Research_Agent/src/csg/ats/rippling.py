"""Rippling ATS (Recruiting) public job board. No auth.

GET https://ats.rippling.com/{token}/jobs  (HTML, Next.js server-rendered)

Confirmed live against a real tenant with open postings (e.g. "copado").
There is no separate public JSON API -- the page embeds its React Query
cache as a `__NEXT_DATA__` script tag, and the job list lives at
`props.pageProps.dehydratedState.queries[N].state.data.items` for whichever
query's key starts with ["board", token, "job-posts", ...]. Parsing the
rendered page (rather than the Next.js `/_next/data/{buildId}/...json`
route) is deliberate: buildId changes on every Rippling deploy, so it is not
a stable URL to depend on, while the embedded data always matches whatever
build actually served the page.

Growing share of mid-size tech/SaaS careers pages (unlike Greenhouse/Lever/
Workable, no guessable token API to probe -- Rippling returns a real page
for any slug-shaped token, generic or not, so this is careers_page-only,
never token_probe; see BOARD_LINK_PATTERNS in __init__.py).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of

BASE = "https://ats.rippling.com"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def _url(token: str) -> str:
    return f"{BASE}/{token}/jobs"


def _extract_items(body: str, token: str) -> Optional[list[dict[str, Any]]]:
    m = _NEXT_DATA_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    for q in queries:
        key = q.get("queryKey")
        if (
            isinstance(key, list)
            and len(key) >= 3
            and key[0] == "board"
            and key[1] == token
            and key[2] == "job-posts"
        ):
            state_data = q.get("state", {}).get("data")
            if isinstance(state_data, dict) and isinstance(state_data.get("items"), list):
                return state_data["items"]
    return None


async def verify(fetcher: Fetcher, token: str) -> bool:
    status, body = await fetcher.get(_url(token))
    if status != 200 or not body:
        return False
    return _extract_items(body, token) is not None


def _location(job: dict[str, Any]) -> Optional[str]:
    locs = job.get("locations")
    if not isinstance(locs, list) or not locs:
        return None
    first = locs[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    if name:
        return str(name)
    parts = [first.get("city"), first.get("stateCode") or first.get("state"), first.get("country")]
    joined = ", ".join(str(p) for p in parts if p)
    return joined or None


def _employment_type(job: dict[str, Any]) -> Optional[str]:
    locs = job.get("locations")
    if isinstance(locs, list) and locs and isinstance(locs[0], dict):
        return locs[0].get("workplaceType")
    return None


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    status, body = await fetcher.get(_url(token))
    if status != 200 or not body:
        return []

    items = _extract_items(body, token)
    if not items:
        return []

    out: list[JobPosting] = []
    for job in items:
        if not isinstance(job, dict):
            continue
        ext_id = str(job.get("id") or "")
        if not ext_id:
            continue
        department = job.get("department")
        out.append(
            JobPosting(
                domain=domain,
                ats_provider="rippling",
                external_id=ext_id,
                title=str(job.get("name") or "").strip(),
                location=_location(job),
                department=department.get("name") if isinstance(department, dict) else None,
                employment_type=_employment_type(job),
                url=first_of(job, "url"),
                posted_at=None,
                description="",
                raw=job,
            )
        )
    return out
