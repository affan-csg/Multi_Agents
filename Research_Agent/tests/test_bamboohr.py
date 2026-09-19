"""Tests for the BambooHR adapter."""

from __future__ import annotations

import asyncio

from csg.ats import bamboohr


class FakeFetcher:
    def __init__(self, gets: dict[str, tuple[int, object]]):
        self.gets = gets

    async def get_json(self, url: str):
        return self.gets.get(url, (404, None))


def test_verify_true_for_real_tenant():
    fetcher = FakeFetcher({
        bamboohr._url("basdensteel"): (200, {"meta": {"totalCount": 22}, "result": []}),
    })
    assert asyncio.run(bamboohr.verify(fetcher, "basdensteel")) is True


def test_verify_false_for_unknown_tenant():
    # A nonexistent tenant 302s to bamboohr.com's marketing site rather than
    # returning this JSON shape -- Fetcher.get_json would surface that as a
    # non-200/non-JSON result, not this dict shape. Model it as 404/None
    # (what get_json returns for anything that isn't a clean 200+JSON).
    fetcher = FakeFetcher({
        bamboohr._url("fake-co-xyz"): (404, None),
    })
    assert asyncio.run(bamboohr.verify(fetcher, "fake-co-xyz")) is False


def test_verify_true_even_with_zero_current_openings():
    fetcher = FakeFetcher({
        bamboohr._url("quiet-co"): (200, {"meta": {"totalCount": 0}, "result": []}),
    })
    assert asyncio.run(bamboohr.verify(fetcher, "quiet-co")) is True


def test_fetch_builds_postings():
    job = {
        "id": "21",
        "jobOpeningName": "Basden Steel - General Job Application",
        "departmentId": None,
        "departmentLabel": None,
        "employmentStatusLabel": "Full-Time",
        "employmentType": None,
        "location": {"city": "Burleson", "state": "Texas"},
        "isRemote": None,
    }
    fetcher = FakeFetcher({
        bamboohr._url("basdensteel"): (200, {"meta": {"totalCount": 1}, "result": [job]}),
    })
    postings = asyncio.run(bamboohr.fetch(fetcher, "basdensteel.com", "basdensteel"))
    assert len(postings) == 1
    p = postings[0]
    assert p.external_id == "21"
    assert p.title == "Basden Steel - General Job Application"
    assert p.location == "Burleson, Texas"
    assert p.employment_type == "Full-Time"
    assert p.url == "https://basdensteel.bamboohr.com/careers/21"
    assert p.ats_provider == "bamboohr"


def test_fetch_uses_department_label_when_present():
    job = {"id": "24", "jobOpeningName": "Ironworkers", "departmentLabel": "DSS FIELD", "location": None}
    fetcher = FakeFetcher({
        bamboohr._url("basdensteel"): (200, {"result": [job]}),
    })
    postings = asyncio.run(bamboohr.fetch(fetcher, "basdensteel.com", "basdensteel"))
    assert postings[0].department == "DSS FIELD"
    assert postings[0].location is None


def test_fetch_skips_postings_with_no_id():
    fetcher = FakeFetcher({
        bamboohr._url("basdensteel"): (200, {"result": [{"jobOpeningName": "No ID here"}]}),
    })
    postings = asyncio.run(bamboohr.fetch(fetcher, "basdensteel.com", "basdensteel"))
    assert postings == []
