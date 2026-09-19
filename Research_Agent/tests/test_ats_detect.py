"""Tests for ATS detection, in particular the subpage-following fix: a
top-level careers page that is only a landing page (no board link on it,
just a link to a second page where the board actually lives) must still
resolve, e.g. datarobot.com/careers -> /careers/open-positions/ -> Workday.
"""

from __future__ import annotations

import asyncio

from csg.ats import PROBEABLE_PROVIDERS, _candidate_subpages, detect


class FakeFetcher:
    """Minimal stand-in for csg.http.Fetcher: only the methods ats/__init__.py
    and the provider adapters' verify() actually call."""

    def __init__(self, gets: dict[str, tuple[int, str]], posts: dict[str, tuple[int, dict]] | None = None):
        self.gets = gets
        self.posts = posts or {}
        self.get_calls: list[str] = []

    async def get(self, url: str) -> tuple[int, str]:
        self.get_calls.append(url)
        return self.gets.get(url, (404, ""))

    async def get_json(self, url: str) -> tuple[int, object]:
        return self.posts.get(url, (404, {}))

    async def post_json(self, url: str, json_body: dict) -> tuple[int, object]:
        return self.posts.get(url, (404, {}))


# --- _candidate_subpages (pure function) ------------------------------------

def test_candidate_subpages_finds_open_positions_link():
    body = '<a href="/careers/open-positions/">Explore open positions</a>'
    subs = _candidate_subpages("https://datarobot.com/careers", body)
    assert subs == ["https://datarobot.com/careers/open-positions/"]


def test_candidate_subpages_ignores_unrelated_links():
    body = '<a href="/about">About us</a><a href="/blog/post-1">Blog</a>'
    assert _candidate_subpages("https://example.com/careers", body) == []


def test_candidate_subpages_ignores_off_domain_links():
    # A link straight to the ATS is caught by BOARD_LINK_PATTERNS already;
    # subpage-following must not re-fetch an external host.
    body = '<a href="https://jobs.lever.co/acme/open-positions">Openings</a>'
    assert _candidate_subpages("https://acme.com/careers", body) == []


def test_candidate_subpages_caps_at_max_subpages():
    body = "".join(f'<a href="/jobs/open-positions-{i}/">Openings {i}</a>' for i in range(10))
    subs = _candidate_subpages("https://example.com/careers", body)
    assert len(subs) == 3  # MAX_SUBPAGES


# --- detect() end-to-end with a fake fetcher --------------------------------

def test_detect_follows_landing_page_to_workday_board():
    """Mirrors the real datarobot.com case: /careers has no board link, only
    a button to /careers/open-positions/, which links straight to a Workday
    job posting URL -- that's where workday.LINK_RE finds tenant/shard/site."""
    landing = '<a href="/careers/open-positions/">Explore open positions</a>'
    listing = (
        '<a href="https://acme.wd1.myworkdayjobs.com/Acme_External_Careers/'
        'job/Boston/Staff-Engineer_R-1">Staff Engineer</a>'
    )
    fetcher = FakeFetcher(
        gets={
            "https://acme.com": (200, landing),
        },
        posts={
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Acme_External_Careers/jobs":
                (200, {"jobPostings": [{"externalPath": "/job/1"}]}),
        },
    )
    # Only the root path resolves in this fixture -- CAREERS_PATHS tries several,
    # the rest 404 via the FakeFetcher default, then the open-positions subpage:
    fetcher.gets["https://acme.com/careers/open-positions/"] = (200, listing)

    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result is not None
    provider, token, detected_by, careers_url = result
    assert provider == "workday"
    assert token == "acme/wd1/Acme_External_Careers"
    assert detected_by == "careers_page"
    assert careers_url == "https://acme.com/careers/open-positions/"


def test_detect_returns_none_when_no_board_anywhere():
    fetcher = FakeFetcher(gets={"https://acme.com": (200, "<p>We are hiring! Email us.</p>")})
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result is None


def test_detect_finds_smartrecruiters_board_link_on_careers_page():
    body = '<a href="https://careers.smartrecruiters.com/Acme">See open roles</a>'
    fetcher = FakeFetcher(
        gets={"https://acme.com": (200, body)},
        posts={
            "https://api.smartrecruiters.com/v1/companies/Acme/postings?limit=100":
                (200, {"content": [{"id": "1", "name": "Engineer"}]}),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("smartrecruiters", "Acme", "careers_page", "https://acme.com")


def test_detect_finds_workable_board_link_on_careers_page():
    body = '<a href="https://apply.workable.com/acme/">See open roles</a>'
    fetcher = FakeFetcher(
        gets={"https://acme.com": (200, body)},
        posts={
            "https://apply.workable.com/api/v1/widget/accounts/acme":
                (200, {"name": "Acme", "jobs": []}),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("workable", "acme", "careers_page", "https://acme.com")


def test_detect_finds_bamboohr_board_link_on_careers_page():
    body = '<a href="https://acme.bamboohr.com/careers">See open roles</a>'
    fetcher = FakeFetcher(
        gets={"https://acme.com": (200, body)},
        posts={
            "https://acme.bamboohr.com/careers/list":
                (200, {"meta": {"totalCount": 1}, "result": [{"id": "1"}]}),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("bamboohr", "acme", "careers_page", "https://acme.com")


def test_detect_finds_bamboohr_via_token_probe():
    # No board link anywhere on the site, but the guessed token ("acme",
    # derived from the domain) resolves against BambooHR directly.
    fetcher = FakeFetcher(
        gets={"https://acme.com": (200, "<p>We're hiring, ask around!</p>")},
        posts={
            "https://acme.bamboohr.com/careers/list":
                (200, {"meta": {"totalCount": 3}, "result": [{"id": "1"}]}),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("bamboohr", "acme", "token_probe", None)


def test_detect_finds_rippling_board_link_on_careers_page():
    import json

    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {"queryKey": ["board", "acme", "job-posts", False, {}], "state": {"data": {"items": [{"id": "1", "name": "Engineer"}]}}},
                    ]
                }
            }
        }
    }
    board_page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
    body = '<a href="https://ats.rippling.com/acme/jobs">See open roles</a>'
    fetcher = FakeFetcher(
        gets={
            "https://acme.com": (200, body),
            "https://ats.rippling.com/acme/jobs": (200, board_page),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("rippling", "acme", "careers_page", "https://acme.com")


def test_detect_finds_rippling_via_token_probe():
    import json

    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {"queryKey": ["board", "acme", "job-posts", False, {}], "state": {"data": {"items": []}}},
                    ]
                }
            }
        }
    }
    board_page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
    fetcher = FakeFetcher(
        gets={
            "https://acme.com": (200, "<p>We're hiring, ask around!</p>"),
            "https://ats.rippling.com/acme/jobs": (200, board_page),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("rippling", "acme", "token_probe", None)


def test_smartrecruiters_is_never_token_probed():
    # A blind token guess must not be trusted for SmartRecruiters -- its API
    # returns 200 for any token (see smartrecruiters.py's module docstring),
    # so if it were in PROBEABLE_PROVIDERS, detect() would "find" a
    # SmartRecruiters board for nearly every company that has none.
    assert "smartrecruiters" not in PROBEABLE_PROVIDERS


def test_detect_falls_back_to_careers_subdomain():
    # DataRobot-style landing pages aside, some companies host the whole
    # careers site on a subdomain (careers.acme.com) with nothing under a
    # path on the apex domain at all.
    board = '<a href="https://jobs.lever.co/acme">Open roles</a>'
    fetcher = FakeFetcher(
        gets={
            "https://careers.acme.com": (200, board),
        },
        posts={
            "https://api.lever.co/v0/postings/acme?mode=json": (200, []),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("lever", "acme", "careers_page", "https://careers.acme.com")


def test_detect_subdomain_fallback_only_tried_after_apex_exhausted():
    # If the apex domain resolves a board, the subdomain probe must never
    # fire -- confirms ordering, not just that both paths individually work.
    apex_board = '<a href="https://jobs.lever.co/acme">Open roles</a>'
    fetcher = FakeFetcher(
        gets={
            "https://acme.com": (200, apex_board),
            "https://careers.acme.com": (200, '<a href="https://jobs.ashbyhq.com/wrong-answer">x</a>'),
        },
        posts={
            "https://api.lever.co/v0/postings/acme?mode=json": (200, []),
        },
    )
    result = asyncio.run(detect(fetcher, "acme.com", name="Acme"))
    assert result == ("lever", "acme", "careers_page", "https://acme.com")
    assert "https://careers.acme.com" not in fetcher.get_calls
