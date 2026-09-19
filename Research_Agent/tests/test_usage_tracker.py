"""Tests for usage_tracker.track() -- the best-effort instrumentation wrapper
used by the Gemini/Groq/Tavily/Hunter call sites (discovery/research.py,
discovery/web_search.py, discovery/groq_tavily.py, qualify.py, contacts.py)
to feed the Settings > Usage tab.

Two properties matter most here, since this module is purely observational:
1. It must never change what the wrapped call site raises or returns.
2. A DB write failure inside the background thread must never surface to
   the caller -- these tests patch db.insert_api_usage to explode and assert
   the wrapped call still behaves normally.
"""

from __future__ import annotations

import time

import httpx

import pytest

from csg import usage_tracker
from csg.usage_tracker import track


def _join_background_writes(timeout: float = 0.5) -> None:
    """usage_tracker fires a daemon thread per call; give it a moment to run
    before asserting on what it wrote. The fake DB calls in these tests are
    in-memory list appends with no real I/O, so this margin is generous."""
    time.sleep(timeout)


@pytest.fixture
def captured_rows(monkeypatch):
    rows: list[dict] = []

    def fake_insert(conn, new_rows):
        rows.extend(new_rows)

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr("csg.db.connect", lambda: FakeConn())
    monkeypatch.setattr("csg.db.insert_api_usage", fake_insert)
    return rows


def test_track_records_success(captured_rows):
    with track("gemini", "enrich_firmographics", domain="acme.com") as u:
        u.set_tokens(prompt=100, completion=50, total=150)

    _join_background_writes()

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["provider"] == "gemini"
    assert row["call_kind"] == "enrich_firmographics"
    assert row["domain"] == "acme.com"
    assert row["success"] is True
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50
    assert row["total_tokens"] == 150


def test_track_reraises_and_records_failure(captured_rows):
    class QuotaError(Exception):
        code = 429
        def __str__(self):
            return "RESOURCE_EXHAUSTED: exceeded your current quota"

    with pytest.raises(QuotaError):
        with track("gemini", "qualify"):
            raise QuotaError()

    _join_background_writes()

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["success"] is False
    assert row["status_code"] == 429
    assert row["error_kind"] == "quota_exhausted"


def test_track_never_raises_when_db_write_fails(monkeypatch):
    """The whole point of this module: a broken DB connection must not
    surface as an exception into research/qualify code."""
    def boom():
        raise RuntimeError("Supabase is down")

    monkeypatch.setattr("csg.db.connect", boom)

    with track("tavily", "tavily_search") as u:
        u.set_tokens(total=10)
    # No exception should propagate from the `with` block or from the
    # background thread -- if it does, pytest's thread exception hook (or
    # this test hanging/erroring) would catch it.


def test_manual_mark_failure_is_not_overwritten_by_exception_handler(captured_rows):
    """A call site that detects a 429 itself (e.g. after a retry budget is
    exhausted) and calls u.mark_failure() before returning normally should
    have that classification preserved -- track() only auto-classifies when
    an exception actually propagates."""
    with track("groq", "discovery_parse") as u:
        u.mark_failure(status_code=429, error_kind="rate_limited")

    _join_background_writes()

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["success"] is False
    assert row["status_code"] == 429
    assert row["error_kind"] == "rate_limited"


def test_retry_count_is_recorded(captured_rows):
    with track("tavily", "tavily_search") as u:
        u.retry_count += 1
        u.retry_count += 1

    _join_background_writes()

    assert captured_rows[0]["retry_count"] == 2


@pytest.mark.parametrize(
    "message,expected",
    [
        ("RESOURCE_EXHAUSTED: exceeded your current quota", "quota_exhausted"),
        ("429 Too Many Requests: rate limit hit", "rate_limited"),
        ("[SSL: UNEXPECTED_EOF_WHILE_READING]", "connection_error"),
    ],
)
def test_error_kind_classification(message, expected):
    assert usage_tracker.classify_error(Exception(message)) == expected


def test_status_code_extracted_from_httpx_http_status_error(captured_rows):
    """Regression: httpx.HTTPStatusError (what resp.raise_for_status() raises
    -- the common failure path for Hunter/Tavily/Groq's plain httpx calls)
    carries its status on .response.status_code, not on .code/.status_code.
    Before this fix, a 500 from any of these providers recorded status_code
    as None instead of 500, since the old guess only checked for "429" in
    the exception's string form."""
    request = httpx.Request("GET", "https://api.hunter.io/v2/domain-search")
    response = httpx.Response(500, request=request, text="internal error")
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    assert usage_tracker._guess_status_code(exc) == 500

    with pytest.raises(httpx.HTTPStatusError):
        with track("hunter", "domain_search"):
            raise exc

    _join_background_writes()
    assert captured_rows[0]["status_code"] == 500
