"""Tests for GET /settings/usage -- the Settings > Usage tab's data source.

Aggregates api_usage rows (written best-effort by usage_tracker.py) into a
per-provider summary plus a daily time series. Monkeypatches db.connect /
db.api_usage_since / db.api_usage_all directly, same style as
test_api_delete_run.py, so these lock in the aggregation logic without
touching a real Supabase project.

Three window modes, matching the Usage tab's filter (Latest Research Run /
Last 7 Days / Last 30 Days / All Time):
  - no params at all -> All Time -> db.api_usage_all (no lower bound).
  - `hours` only -> db.api_usage_since(now - hours).
  - `since` (an ISO timestamp, e.g. a run's started_at) -> used verbatim,
    which is how "Latest Research Run" is implemented.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from csg import api, db


@pytest.fixture
def client():
    return TestClient(api.app)


def _row(provider, success=True, error_kind=None, status_code=None, retry_count=0,
         prompt_tokens=None, completion_tokens=None, total_tokens=None,
         latency_ms=None, created_at="2026-09-18T12:00:00+00:00"):
    return {
        "provider": provider, "call_kind": "x", "run_id": None, "domain": None,
        "success": success, "status_code": status_code, "error_kind": error_kind,
        "retry_count": retry_count, "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "total_tokens": total_tokens, "created_at": created_at,
    }


def test_usage_empty_when_no_rows(client, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_all", lambda conn: [])

    r = client.get("/settings/usage")
    assert r.status_code == 200
    body = r.json()
    assert set(body["providers"].keys()) == {"gemini", "groq", "tavily", "hunter"}
    for provider in ("gemini", "groq", "tavily", "hunter"):
        assert body["providers"][provider]["requests"] == 0
        assert body["providers"][provider]["daily"] == []


class _FakeConn:
    def close(self):
        pass


def test_usage_aggregates_success_and_failure_counts(client, monkeypatch):
    rows = [
        _row("gemini", success=True, prompt_tokens=100, completion_tokens=20, total_tokens=120, latency_ms=800),
        _row("gemini", success=False, error_kind="quota_exhausted", status_code=429),
        _row("gemini", success=False, error_kind="rate_limited", status_code=429, retry_count=2),
        _row("groq", success=False, error_kind="connection_error"),
        _row("tavily", success=True, latency_ms=200),
        _row("hunter", success=True, latency_ms=150),
        _row("hunter", success=False, error_kind="quota_exhausted", status_code=429),
    ]
    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_all", lambda conn: rows)

    r = client.get("/settings/usage")
    assert r.status_code == 200
    body = r.json()

    gemini = body["providers"]["gemini"]
    assert gemini["requests"] == 3
    assert gemini["successes"] == 1
    assert gemini["failures"] == 2
    assert gemini["quota_exhausted"] == 1
    assert gemini["rate_limited_429"] == 1
    assert gemini["retries"] == 2
    assert gemini["total_tokens"] == 120
    assert gemini["avg_latency_ms"] == 800

    groq = body["providers"]["groq"]
    assert groq["requests"] == 1
    assert groq["connection_errors"] == 1

    tavily = body["providers"]["tavily"]
    assert tavily["requests"] == 1
    assert tavily["successes"] == 1

    hunter = body["providers"]["hunter"]
    assert hunter["requests"] == 2
    assert hunter["successes"] == 1
    assert hunter["quota_exhausted"] == 1
    assert hunter["total_tokens"] == 0  # Hunter is a search API, no token usage


def test_usage_buckets_by_day(client, monkeypatch):
    rows = [
        _row("gemini", created_at="2026-09-17T09:00:00+00:00", total_tokens=10),
        _row("gemini", created_at="2026-09-17T15:00:00+00:00", total_tokens=20),
        _row("gemini", created_at="2026-09-18T02:00:00+00:00", total_tokens=30, success=False),
    ]
    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_all", lambda conn: rows)

    r = client.get("/settings/usage")
    daily = r.json()["providers"]["gemini"]["daily"]

    assert daily == [
        {"date": "2026-09-17", "requests": 2, "failures": 0, "total_tokens": 30},
        {"date": "2026-09-18", "requests": 1, "failures": 1, "total_tokens": 30},
    ]


def test_usage_degrades_gracefully_when_table_missing(client, monkeypatch):
    def raise_missing_table(conn):
        raise db.SupabaseError("relation \"api_usage\" does not exist")

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_all", raise_missing_table)

    r = client.get("/settings/usage")
    assert r.status_code == 200
    assert r.json()["providers"]["gemini"]["requests"] == 0


def test_usage_defaults_to_all_time_with_no_params(client, monkeypatch):
    """No `hours` and no `since` -- the tab's default "All Time" filter --
    must hit api_usage_all (no lower time bound), not api_usage_since."""
    captured = {"called": False}

    def fake_all(conn):
        captured["called"] = True
        return []

    def fail_if_since_called(conn, since):
        raise AssertionError("api_usage_since should not be called for All Time")

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_all", fake_all)
    monkeypatch.setattr(db, "api_usage_since", fail_if_since_called)

    r = client.get("/settings/usage")
    assert r.status_code == 200
    assert r.json()["window_hours"] is None
    assert r.json()["since"] is None
    assert captured["called"]


def test_usage_accepts_hours_param(client, monkeypatch):
    captured = {}

    def fake_since(conn, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_since", fake_since)

    r = client.get("/settings/usage?hours=24")
    assert r.status_code == 200
    assert r.json()["window_hours"] == 24
    assert "since" in captured


def test_usage_accepts_explicit_since_param(client, monkeypatch):
    """`since` is how the Dashboard implements "Latest Research Run" -- it
    passes that run's own started_at verbatim, with no `hours` involved."""
    captured = {}

    def fake_since(conn, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_since", fake_since)

    r = client.get("/settings/usage?since=2026-09-15T00%3A00%3A00%2B00%3A00")
    assert r.status_code == 200
    assert captured["since"] == "2026-09-15T00:00:00+00:00"
    assert r.json()["since"] == "2026-09-15T00:00:00+00:00"
    assert r.json()["window_hours"] is None


def test_usage_since_wins_over_hours_when_both_passed(client, monkeypatch):
    captured = {}

    def fake_since(conn, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "api_usage_since", fake_since)

    r = client.get("/settings/usage?hours=24&since=2026-09-15T00%3A00%3A00%2B00%3A00")
    assert r.status_code == 200
    assert captured["since"] == "2026-09-15T00:00:00+00:00"
