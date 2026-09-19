"""Hunter.io response parsing -- pure function, no network.

Locks in the hard requirement: a contact without an email must never reach
the database. LinkedIn is optional and carried through when present, but
never required.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from csg import contacts
from csg.contacts import _parse_emails


def test_contact_without_email_is_discarded():
    """The actual guarantee behind 'email is a must': Hunter sometimes
    returns entries with a name/position but no verified email address (e.g.
    a guessed pattern Hunter couldn't confirm) -- those must never become a
    saved contact."""
    data = {"data": {"emails": [
        {"first_name": "Jane", "last_name": "Doe", "position": "CHRO"},  # no "value"
        {"value": "", "first_name": "No", "last_name": "Email"},         # empty value
        {"value": "real@acme.com", "first_name": "Has", "last_name": "Email"},
    ]}}
    out = _parse_emails(data)
    assert len(out) == 1
    assert out[0]["email"] == "real@acme.com"


def test_email_is_always_present_on_every_parsed_contact():
    data = {"data": {"emails": [
        {"value": "a@acme.com"},
        {"value": "b@acme.com", "linkedin": "https://linkedin.com/in/b"},
    ]}}
    out = _parse_emails(data)
    assert len(out) == 2
    assert all(c["email"] for c in out)


def test_linkedin_is_optional_and_carried_through_when_present():
    data = {"data": {"emails": [
        {"value": "a@acme.com", "linkedin": "https://linkedin.com/in/a"},
        {"value": "b@acme.com"},  # no linkedin field at all
    ]}}
    out = _parse_emails(data)
    by_email = {c["email"]: c for c in out}
    assert by_email["a@acme.com"]["linkedin_url"] == "https://linkedin.com/in/a"
    assert by_email["b@acme.com"]["linkedin_url"] is None


def test_non_dict_entries_are_skipped_not_crashed_on():
    """Defensive against a malformed/unexpected Hunter response shape."""
    data = {"data": {"emails": [None, "not a dict", 42, {"value": "ok@acme.com"}]}}
    out = _parse_emails(data)
    assert len(out) == 1
    assert out[0]["email"] == "ok@acme.com"


def test_missing_emails_key_returns_empty_list():
    assert _parse_emails({"data": {}}) == []
    assert _parse_emails({}) == []


def test_find_contacts_falls_back_to_unfiltered_when_hr_search_is_empty(monkeypatch):
    """A small prospect with no dedicated HR title (e.g. hiring routed through
    the CEO or Head of Sales) comes back empty from department=hr even though
    Hunter has plenty of data for the domain -- seen live for peregrine.io.
    find_contacts must retry without the department filter instead of
    reporting zero contacts."""
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    calls: list[tuple[str | None, str | None]] = []

    async def fake_search(domain, api_key, limit, department, seniority):
        calls.append((department, seniority))
        if department == contacts.DEPARTMENT:
            return []
        return [{"email": "ceo@peregrine.io", "seniority": "executive"}]

    monkeypatch.setattr(contacts, "_search", fake_search)

    out = asyncio.run(contacts.find_contacts("peregrine.io"))

    assert out == [{"email": "ceo@peregrine.io", "seniority": "executive"}]
    assert calls == [
        (contacts.DEPARTMENT, contacts.SENIORITY),
        (None, contacts.SENIORITY),
    ]


def test_find_contacts_does_not_fall_back_when_hr_search_has_hits(monkeypatch):
    """The unfiltered fallback is only for the empty case -- a domain that
    genuinely has HR contacts must not waste a second Hunter.io call (quota
    is 25/month, tightest in the pipeline)."""
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    calls: list[tuple[str | None, str | None]] = []

    async def fake_search(domain, api_key, limit, department, seniority):
        calls.append((department, seniority))
        return [{"email": "hr@acme.com", "seniority": "senior"}]

    monkeypatch.setattr(contacts, "_search", fake_search)

    out = asyncio.run(contacts.find_contacts("acme.com"))

    assert out == [{"email": "hr@acme.com", "seniority": "senior"}]
    assert calls == [(contacts.DEPARTMENT, contacts.SENIORITY)]


def test_find_contacts_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    with pytest.raises(contacts.ContactDiscoveryError, match="HUNTER_API_KEY"):
        asyncio.run(contacts.find_contacts("acme.com"))


# --- usage tracking (Settings > Usage tab) --------------------------------
# _search is the one Hunter.io call site; these hit it directly (rather than
# monkeypatching it out, like the tests above) with a mocked transport so the
# track("hunter", ...) wrapper around it actually runs.

def _patch_async_client(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(contacts.httpx, "AsyncClient", fake_client)


@pytest.fixture
def captured_usage_rows(monkeypatch):
    rows: list[dict] = []

    def fake_insert(conn, new_rows):
        rows.extend(new_rows)

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr("csg.db.connect", lambda: FakeConn())
    monkeypatch.setattr("csg.db.insert_api_usage", fake_insert)
    return rows


def _wait_for_usage_row():
    time.sleep(0.3)


def test_search_records_success_usage(captured_usage_rows):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"emails": [{"value": "a@acme.com"}]}})

    with pytest.MonkeyPatch.context() as mp:
        _patch_async_client(mp, handler)
        out = asyncio.run(contacts._search("acme.com", "key", 5, "hr", "senior,executive"))

    assert out == [{
        "email": "a@acme.com", "first_name": None, "last_name": None, "position": None,
        "seniority": None, "department": None, "linkedin_url": None, "confidence": None,
        "verification": None,
    }]
    _wait_for_usage_row()

    assert len(captured_usage_rows) == 1
    row = captured_usage_rows[0]
    assert row["provider"] == "hunter"
    assert row["call_kind"] == "domain_search"
    assert row["domain"] == "acme.com"
    assert row["success"] is True
    assert row["status_code"] == 200


def test_search_429_is_tagged_quota_exhausted_not_rate_limited(captured_usage_rows):
    """Hunter's free tier is a hard 25/month, not a per-minute window -- a 429
    means the month's quota is gone, not "back off and retry a moment later,"
    so this must record quota_exhausted, matching discover_run's own
    "stop immediately, don't burn more attempts" behavior on this error."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"errors": [{"details": "quota exceeded"}]})

    with pytest.MonkeyPatch.context() as mp:
        _patch_async_client(mp, handler)
        with pytest.raises(contacts.ContactDiscoveryError):
            asyncio.run(contacts._search("acme.com", "key", 5, "hr", "senior,executive"))

    _wait_for_usage_row()

    assert len(captured_usage_rows) == 1
    row = captured_usage_rows[0]
    assert row["provider"] == "hunter"
    assert row["success"] is False
    assert row["status_code"] == 429
    assert row["error_kind"] == "quota_exhausted"


def test_search_other_http_error_is_recorded_as_failure(captured_usage_rows):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with pytest.MonkeyPatch.context() as mp:
        _patch_async_client(mp, handler)
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(contacts._search("acme.com", "key", 5, "hr", "senior,executive"))

    _wait_for_usage_row()

    assert len(captured_usage_rows) == 1
    row = captured_usage_rows[0]
    assert row["success"] is False
    assert row["status_code"] == 500
