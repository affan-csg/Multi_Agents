"""Workday CXS job board API. No auth, but POST-only (unlike GH/Lever/Ashby's
plain GETs) and the "token" is really three parts -- tenant, shard, and site
-- none of which is derivable from the other; a shard (wd1, wd3, wd5, ...) can
only be learned from an actual career-site link, never guessed. So this
adapter's "token" is a single encoded string "tenant/shard/site" stored in the
companies.ats_token column, unlike every other provider's plain token.

POST https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Body: {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}
-- limit is capped at 20 server-side; a higher value silently returns nothing.

NOTE ON TERMS OF SERVICE: Workday's Site Terms explicitly prohibit automated
data-gathering/scraping of their hosted sites. This adapter is technically
functional (confirmed against a live public tenant) but using it against a
given company's career site may violate that company's Workday-hosted site's
terms. This is a deliberate scope decision, not an oversight -- see the
project's ATS-adapter discussion for context.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from ..http import Fetcher
from ..models import JobPosting
from ._common import first_of, parse_dt, strip_html

PAGE_LIMIT = 20  # server-enforced ceiling; requesting more returns empty
MAX_PAGES = 25   # safety cap: 25 * 20 = 500 postings max per company

# Unlike GH/Lever/Ashby, Workday's list endpoint has no description/real date
# -- getting those requires one extra GET per posting (its own endpoint, not
# batchable). A company with 1000+ open Workday reqs (not unusual for a large
# enterprise) would mean 1000+ sequential detail calls, worse than every other
# adapter combined. Two mitigations: cap how many postings get a detail
# fetch, and run detail fetches concurrently (the shared Fetcher's per-host
# semaphore/delay still applies, so this does not defeat rate limiting).
MAX_DETAIL_FETCHES = 60
DETAIL_CONCURRENCY = 8

TOKEN_RE = re.compile(r"^([\w-]+)/(wd\d+)/([\w-]+)$")

# Matches a Workday career-site link embedded in a company's own page HTML,
# e.g. https://mastercard.wd1.myworkdayjobs.com/CorporateCareers, or with an
# optional locale prefix: https://acme.wd1.myworkdayjobs.com/en-US/CorporateCareers.
# The optional first group is deliberately restricted to a locale-shaped
# segment (e.g. "en-US", "en") rather than any [\w-]+ -- a real individual job
# link like .../CorporateCareers/job/Boston/Staff-Engineer_R-123 would
# otherwise have its "job" path segment misread as the locale-to-skip and its
# actual site ("CorporateCareers") lost, capturing "job" as the site instead.
LINK_RE = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}(?:-[A-Z]{2})?/)?([\w-]+)", re.I
)


def encode_token(tenant: str, shard: str, site: str) -> str:
    return f"{tenant}/{shard}/{site}"


def decode_token(token: str) -> Optional[tuple[str, str, str]]:
    m = TOKEN_RE.match(token)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def _jobs_url(tenant: str, shard: str, site: str) -> str:
    return f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def _detail_url(tenant: str, shard: str, site: str, external_path: str) -> str:
    return f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"


async def verify(fetcher: Fetcher, token: str) -> bool:
    parts = decode_token(token)
    if not parts:
        return False
    tenant, shard, site = parts
    status, data = await fetcher.post_json(
        _jobs_url(tenant, shard, site),
        {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
    )
    return status == 200 and isinstance(data, dict) and isinstance(data.get("jobPostings"), list)


async def fetch(fetcher: Fetcher, domain: str, token: str) -> list[JobPosting]:
    parts = decode_token(token)
    if not parts:
        return []
    tenant, shard, site = parts

    summaries: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        status, data = await fetcher.post_json(
            _jobs_url(tenant, shard, site),
            {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": page * PAGE_LIMIT, "searchText": ""},
        )
        if status != 200 or not isinstance(data, dict):
            break
        raw_postings = data.get("jobPostings") or []
        if not raw_postings:
            break
        # Whether to keep paginating is decided by the RAW page size, not the
        # post-filter count -- a page can legitimately contain one malformed
        # entry (seen live: 20 raw, 19 with externalPath) without being the
        # last page. Using the filtered count here previously truncated
        # pagination after the first such page, silently dropping every
        # subsequent page of real postings.
        summaries.extend(p for p in raw_postings if isinstance(p, dict) and p.get("externalPath"))
        if len(raw_postings) < PAGE_LIMIT:
            break

    # Detail fetches are capped and run concurrently -- see MAX_DETAIL_FETCHES.
    # Postings beyond the cap still get a JobPosting from list-only fields
    # (title/location, no description/real date/url).
    to_detail, rest = summaries[:MAX_DETAIL_FETCHES], summaries[MAX_DETAIL_FETCHES:]

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def with_detail(summary: dict[str, Any]) -> JobPosting:
        async with sem:
            detail = await _fetch_detail(fetcher, tenant, shard, site, summary["externalPath"])
        return _to_job_posting(domain, summary["externalPath"], summary, detail)

    out = list(await asyncio.gather(*(with_detail(s) for s in to_detail)))
    out.extend(_to_job_posting(domain, s["externalPath"], s, None) for s in rest)
    return out


async def _fetch_detail(
    fetcher: Fetcher, tenant: str, shard: str, site: str, external_path: str,
) -> Optional[dict[str, Any]]:
    status, data = await fetcher.get_json(_detail_url(tenant, shard, site, external_path))
    if status != 200 or not isinstance(data, dict):
        return None
    return data.get("jobPostingInfo")


def _to_job_posting(
    domain: str, external_path: str, summary: dict[str, Any], detail: Optional[dict[str, Any]],
) -> JobPosting:
    detail = detail or {}
    ext_id = str(first_of(detail, "id", "jobReqId") or external_path)

    # `country` on the detail response is a {descriptor, id} object, not a
    # string -- prefer the human-readable locationsText from the list summary
    # (e.g. "Pune, India"), which is more specific than the country alone.
    location = summary.get("locationsText") or detail.get("location")
    country = detail.get("country")
    if not location and isinstance(country, dict):
        location = country.get("descriptor")
    posted = parse_dt(detail.get("startDate"))

    return JobPosting(
        domain=domain,
        ats_provider="workday",
        external_id=ext_id,
        title=str(first_of(detail, "title") or summary.get("title") or ""),
        location=str(location) if location else None,
        department=None,
        employment_type=detail.get("timeType"),
        url=detail.get("externalUrl"),
        posted_at=posted,
        description=strip_html(detail.get("jobDescription")),
        raw={k: v for k, v in detail.items() if k != "jobDescription"} or dict(summary),
    )
