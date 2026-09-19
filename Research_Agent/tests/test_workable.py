"""Tests for the Workable adapter."""

from __future__ import annotations

import asyncio

from csg.ats import workable


class FakeFetcher:
    def __init__(self, gets: dict[str, tuple[int, object]]):
        self.gets = gets

    async def get_json(self, url: str):
        return self.gets.get(url, (404, None))


def test_verify_true_for_real_tenant():
    fetcher = FakeFetcher({
        workable._url("zoopla"): (200, {"name": "Zoopla", "description": None, "jobs": []}),
    })
    assert asyncio.run(workable.verify(fetcher, "zoopla")) is True


def test_verify_false_for_unknown_tenant():
    # Unlike SmartRecruiters, Workable's widget API genuinely 404s for a
    # token with no matching account -- confirmed live.
    fetcher = FakeFetcher({
        workable._url("zoopla"): (404, None),
    })
    assert asyncio.run(workable.verify(fetcher, "zoopla")) is False


def test_verify_true_even_with_zero_current_jobs():
    # A real tenant with an empty board right now is still a valid match --
    # the account existing (200 + jobs: []) is the real signal, not job count.
    fetcher = FakeFetcher({
        workable._url("monzo"): (200, {"name": "Monzo", "description": None, "jobs": []}),
    })
    assert asyncio.run(workable.verify(fetcher, "monzo")) is True


def test_fetch_builds_postings():
    job = {
        "title": "Pricing Manager",
        "shortcode": "4212A20A46",
        "employment_type": "Full-time",
        "department": "Commercial",
        "url": "https://apply.workable.com/j/4212A20A46",
        "shortlink": "https://apply.workable.com/j/4212A20A46",
        "published_on": "2026-08-18",
        "created_at": "2026-08-18",
        "country": "United Kingdom",
        "city": "London",
        "state": "England",
    }
    fetcher = FakeFetcher({
        workable._url("zoopla"): (200, {"name": "Zoopla", "jobs": [job]}),
    })
    postings = asyncio.run(workable.fetch(fetcher, "zoopla.co.uk", "zoopla"))
    assert len(postings) == 1
    p = postings[0]
    assert p.external_id == "4212A20A46"
    assert p.title == "Pricing Manager"
    assert p.location == "London, England, United Kingdom"
    assert p.department == "Commercial"
    assert p.employment_type == "Full-time"
    assert p.url == "https://apply.workable.com/j/4212A20A46"
    assert p.ats_provider == "workable"


def test_fetch_skips_jobs_with_no_identifier():
    fetcher = FakeFetcher({
        workable._url("zoopla"): (200, {"name": "Zoopla", "jobs": [{"title": "No code here"}]}),
    })
    postings = asyncio.run(workable.fetch(fetcher, "zoopla.co.uk", "zoopla"))
    assert postings == []
