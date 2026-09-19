"""Tests for the Rippling ATS adapter (ats.rippling.com/{token}/jobs).

Confirmed live against a real tenant (Copado) during the investigation into
why established companies with real, open job boards were scoring D-tier --
Rippling was entirely undetected because it wasn't in ADAPTERS at all, not
because detection failed. These tests pin the __NEXT_DATA__ parsing against
a synthetic fixture shaped like the real page, so a future Rippling frontend
change that breaks the query-key path is caught here rather than silently
producing zero postings again.
"""

from __future__ import annotations

import asyncio
import json

from csg.ats import rippling


def _next_data_html(token: str, items: list[dict]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["board", token, "job-posts", False, {}],
                            "state": {"data": {"items": items}},
                        },
                        {"queryKey": ["board", token, "locations"], "state": {"data": []}},
                    ]
                }
            }
        }
    }
    return f'<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></head></html>'


_SAMPLE_JOB = {
    "id": "2979b047-9678-43a4-bbb4-99db41f6825d",
    "name": "Sr. Full Stack Technical Architect",
    "url": "https://ats.rippling.com/copado/jobs/2979b047-9678-43a4-bbb4-99db41f6825d",
    "department": {"name": "Engineering"},
    "locations": [
        {
            "name": "Jaipur, India",
            "country": "India",
            "countryCode": "IN",
            "workplaceType": "ON_SITE",
        }
    ],
}


class FakeFetcher:
    def __init__(self, gets: dict[str, tuple[int, str]]):
        self.gets = gets

    async def get(self, url: str) -> tuple[int, str]:
        return self.gets.get(url, (404, ""))


def test_verify_true_for_real_board():
    fetcher = FakeFetcher({"https://ats.rippling.com/copado/jobs": (200, _next_data_html("copado", [_SAMPLE_JOB]))})
    assert asyncio.run(rippling.verify(fetcher, "copado")) is True


def test_verify_false_for_missing_token():
    fetcher = FakeFetcher({})  # any unlisted URL 404s via FakeFetcher default
    assert asyncio.run(rippling.verify(fetcher, "nonexistent-xyz")) is False


def test_verify_false_when_page_has_no_next_data():
    fetcher = FakeFetcher({"https://ats.rippling.com/acme/jobs": (200, "<html><body>not a rippling page</body></html>")})
    assert asyncio.run(rippling.verify(fetcher, "acme")) is False


def test_fetch_parses_title_location_department_url():
    fetcher = FakeFetcher({"https://ats.rippling.com/copado/jobs": (200, _next_data_html("copado", [_SAMPLE_JOB]))})
    jobs = asyncio.run(rippling.fetch(fetcher, "copado.com", "copado"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_provider == "rippling"
    assert job.external_id == "2979b047-9678-43a4-bbb4-99db41f6825d"
    assert job.title == "Sr. Full Stack Technical Architect"
    assert job.location == "Jaipur, India"
    assert job.department == "Engineering"
    assert job.employment_type == "ON_SITE"
    assert job.url == "https://ats.rippling.com/copado/jobs/2979b047-9678-43a4-bbb4-99db41f6825d"


def test_fetch_skips_jobs_without_id():
    bad_job = {**_SAMPLE_JOB, "id": ""}
    fetcher = FakeFetcher({"https://ats.rippling.com/copado/jobs": (200, _next_data_html("copado", [bad_job]))})
    assert asyncio.run(rippling.fetch(fetcher, "copado.com", "copado")) == []


def test_fetch_returns_empty_list_on_404():
    fetcher = FakeFetcher({"https://ats.rippling.com/copado/jobs": (404, "")})
    assert asyncio.run(rippling.fetch(fetcher, "copado.com", "copado")) == []


def test_fetch_ignores_other_tenants_query_key_in_same_page():
    """Guards the queryKey[1] == token check -- a page must never accidentally
    return another tenant's cached query that happens to sit in dehydratedState."""
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {"queryKey": ["board", "other-tenant", "job-posts", False, {}], "state": {"data": {"items": [_SAMPLE_JOB]}}},
                    ]
                }
            }
        }
    }
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'
    fetcher = FakeFetcher({"https://ats.rippling.com/copado/jobs": (200, html)})
    assert asyncio.run(rippling.fetch(fetcher, "copado.com", "copado")) == []
