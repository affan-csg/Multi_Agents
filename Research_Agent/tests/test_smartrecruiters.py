"""Tests for the SmartRecruiters adapter, in particular the false-positive
trap its verify() has to guard against: the public API returns 200 for ANY
token, real or fake, when the board has zero current postings."""

from __future__ import annotations

import asyncio

from csg.ats import smartrecruiters


class FakeFetcher:
    def __init__(self, gets: dict[str, tuple[int, object]]):
        self.gets = gets
        self.calls: list[str] = []

    async def get_json(self, url: str):
        self.calls.append(url)
        return self.gets.get(url, (404, None))


def test_verify_true_for_real_company_with_postings():
    fetcher = FakeFetcher({
        smartrecruiters._list_url("BoschGroup"): (200, {
            "offset": 0, "limit": 100, "totalFound": 2,
            "content": [{"id": "1", "name": "Engineer"}, {"id": "2", "name": "Manager"}],
        }),
    })
    assert asyncio.run(smartrecruiters.verify(fetcher, "BoschGroup")) is True


def test_verify_false_for_fake_token_even_though_api_returns_200():
    # Regression: SmartRecruiters' API has no real "not found" -- a made-up
    # token returns 200/totalFound=0/content=[] exactly like a real company
    # with a temporarily empty board. verify() must not treat this as a hit,
    # or every company in the world would "detect" as SmartRecruiters.
    fetcher = FakeFetcher({
        smartrecruiters._list_url("nonexistent-xyz"): (200, {
            "offset": 0, "limit": 100, "totalFound": 0, "content": [],
        }),
    })
    assert asyncio.run(smartrecruiters.verify(fetcher, "nonexistent-xyz")) is False


def test_verify_false_on_404():
    fetcher = FakeFetcher({})  # unset URL -> FakeFetcher default (404, None)
    assert asyncio.run(smartrecruiters.verify(fetcher, "whatever")) is False


def test_fetch_builds_postings_with_detail_merge():
    summary = {
        "id": "744000150217869",
        "name": "AI Engineering Intern",
        "location": {"city": "Plymouth", "region": "MI", "country": "us", "fullLocation": "Plymouth, MI, United States"},
        "department": {"label": "Engineering"},
        "typeOfEmployment": {"label": "Intern"},
        "releasedDate": "2026-09-17T21:19:20.172Z",
    }
    detail = {
        "postingUrl": "https://jobs.smartrecruiters.com/BoschGroup/744000150217869-ai-engineering-intern",
        "jobAd": {"sections": {
            "jobDescription": {"text": "<p>Build cool AI stuff.</p>"},
            "qualifications": {"text": "<p>Python required.</p>"},
        }},
    }
    fetcher = FakeFetcher({
        smartrecruiters._list_url("BoschGroup"): (200, {"content": [summary]}),
        smartrecruiters._detail_url("BoschGroup", "744000150217869"): (200, detail),
    })
    postings = asyncio.run(smartrecruiters.fetch(fetcher, "bosch.com", "BoschGroup"))
    assert len(postings) == 1
    p = postings[0]
    assert p.external_id == "744000150217869"
    assert p.title == "AI Engineering Intern"
    assert p.location == "Plymouth, MI, United States"
    assert p.department == "Engineering"
    assert p.employment_type == "Intern"
    assert p.url == detail["postingUrl"]
    assert "Build cool AI stuff" in p.description
    assert "Python required" in p.description
    assert p.ats_provider == "smartrecruiters"


def test_fetch_skips_postings_with_no_id():
    fetcher = FakeFetcher({
        smartrecruiters._list_url("BoschGroup"): (200, {"content": [{"name": "No ID here"}]}),
    })
    postings = asyncio.run(smartrecruiters.fetch(fetcher, "bosch.com", "BoschGroup"))
    assert postings == []
