"""Tests for calendly/client.py -- the httpx wrapper over api.calendly.com:
request shaping, pagination passthrough, and error handling (unreachable API,
4xx/5xx status, missing token)."""

from __future__ import annotations

import httpx
import pytest

from outreach.calendly.client import CalendlyClient, CalendlyClientError
from outreach.config import Config


def _cfg(token: str = "cal-test-token") -> Config:
    return Config(raw={
        "research": {"api_base": "http://x"}, "selection": {}, "sending": {},
        "personalization": {}, "stages": {},
    }, path=None)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("CALENDLY_API_TOKEN", "cal-test-token")


def _mock_response(json_body, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=json_body, request=httpx.Request("GET", "https://api.calendly.com/x"))


def test_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("CALENDLY_API_TOKEN", raising=False)
    client = CalendlyClient(_cfg())
    assert client.configured is False


def test_configured_with_token():
    client = CalendlyClient(_cfg())
    assert client.configured is True


def test_calling_without_token_raises(monkeypatch):
    monkeypatch.delenv("CALENDLY_API_TOKEN", raising=False)
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError, match="CALENDLY_API_TOKEN"):
        client.current_user()


def test_current_user_uri_is_cached(monkeypatch):
    calls = []

    def fake_get(self, url, **kwargs):
        calls.append(url)
        return _mock_response({"resource": {"uri": "https://api.calendly.com/users/abc", "current_organization": "org-1"}})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    assert client.current_user_uri() == "https://api.calendly.com/users/abc"
    assert client.current_user_uri() == "https://api.calendly.com/users/abc"
    assert calls == ["/users/me"]  # only fetched once, then cached


def test_list_scheduled_events_passes_filters(monkeypatch):
    captured = {}

    def fake_get(self, url, **kwargs):
        if url == "/users/me":
            return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _mock_response({"collection": [], "pagination": {"next_page_token": None}})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    result = client.list_scheduled_events(min_start_time="2026-01-01T00:00:00Z", page_token="tok-1")

    assert captured["url"] == "/scheduled_events"
    assert captured["params"]["user"] == "u1"
    assert captured["params"]["min_start_time"] == "2026-01-01T00:00:00Z"
    assert captured["params"]["page_token"] == "tok-1"
    assert result == {"collection": [], "pagination": {"next_page_token": None}}


def test_request_error_raises_calendly_client_error(monkeypatch):
    def raise_error(self, url, **kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", raise_error)
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError, match="unreachable"):
        client.current_user()


def test_4xx_status_raises_calendly_client_error(monkeypatch):
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, **kwargs: _mock_response({"message": "denied"}, 403))
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError, match="403"):
        client.current_user()


def test_list_event_invitees_derives_uuid_from_full_uri(monkeypatch):
    captured = {}

    def fake_get(self, url, **kwargs):
        captured["url"] = url
        return _mock_response({"collection": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    client.list_event_invitees("https://api.calendly.com/scheduled_events/abc-123")
    assert captured["url"] == "/scheduled_events/abc-123/invitees"


def test_create_webhook_subscription_posts_expected_body(monkeypatch):
    posted = {}

    def fake_get(self, url, **kwargs):
        return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})

    def fake_post(self, url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return _mock_response({"resource": {"uri": "wh-1", "signing_key": "secret-key"}})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = CalendlyClient(_cfg())
    result = client.create_webhook_subscription("https://host/calendly/webhook", events=["invitee.created"])

    assert posted["url"] == "/webhook_subscriptions"
    assert posted["json"]["url"] == "https://host/calendly/webhook"
    assert posted["json"]["events"] == ["invitee.created"]
    assert posted["json"]["organization"] == "org-1"
    assert posted["json"]["user"] == "u1"
    assert result["signing_key"] == "secret-key"


def test_cancel_event_posts_to_cancellation_endpoint(monkeypatch):
    posted = {}

    def fake_post(self, url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return _mock_response({"resource": {"uri": "https://api.calendly.com/scheduled_events/abc-123", "status": "canceled"}})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = CalendlyClient(_cfg())
    result = client.cancel_event("https://api.calendly.com/scheduled_events/abc-123", reason="conflict")

    assert posted["url"] == "/scheduled_events/abc-123/cancellation"
    assert posted["json"] == {"reason": "conflict"}
    assert result["status"] == "canceled"


def test_cancel_event_without_reason_omits_it(monkeypatch):
    posted = {}

    def fake_post(self, url, **kwargs):
        posted["json"] = kwargs.get("json")
        return _mock_response({"resource": {}})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = CalendlyClient(_cfg())
    client.cancel_event("abc-123")
    assert posted["json"] == {}


def test_mark_no_show_posts_expected_body(monkeypatch):
    posted = {}

    def fake_post(self, url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return _mock_response({"resource": {"uri": "https://api.calendly.com/invitee_no_shows/ns-1"}})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = CalendlyClient(_cfg())
    result = client.mark_no_show("https://api.calendly.com/scheduled_events/abc/invitees/inv-1")

    assert posted["url"] == "/invitee_no_shows"
    assert posted["json"] == {"invitee": "https://api.calendly.com/scheduled_events/abc/invitees/inv-1"}
    assert result["uri"] == "https://api.calendly.com/invitee_no_shows/ns-1"


def test_unmark_no_show_deletes_by_uuid_from_uri(monkeypatch):
    deleted = {}

    def fake_delete(self, url, **kwargs):
        deleted["url"] = url
        return _mock_response({}, 204)

    monkeypatch.setattr(httpx.Client, "delete", fake_delete)
    client = CalendlyClient(_cfg())
    client.unmark_no_show("https://api.calendly.com/invitee_no_shows/ns-1")
    assert deleted["url"] == "/invitee_no_shows/ns-1"


def test_mark_no_show_4xx_raises_calendly_client_error(monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: _mock_response({"message": "denied"}, 403))
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError):
        client.mark_no_show("https://api.calendly.com/scheduled_events/abc/invitees/inv-1")


def test_list_event_types_passes_filters(monkeypatch):
    captured = {}

    def fake_get(self, url, **kwargs):
        if url == "/users/me":
            return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _mock_response({"collection": [], "pagination": {"next_page_token": None}})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    result = client.list_event_types(page_token="tok-1")

    assert captured["url"] == "/event_types"
    assert captured["params"]["user"] == "u1"
    assert captured["params"]["page_token"] == "tok-1"
    assert result == {"collection": [], "pagination": {"next_page_token": None}}


def test_list_event_types_4xx_raises_calendly_client_error(monkeypatch):
    def fake_get(self, url, **kwargs):
        if url == "/users/me":
            return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})
        return _mock_response({"message": "denied"}, 403)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError):
        client.list_event_types()


def test_list_availability_schedules_passes_user(monkeypatch):
    captured = {}

    def fake_get(self, url, **kwargs):
        if url == "/users/me":
            return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _mock_response({"collection": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    result = client.list_availability_schedules()

    assert captured["url"] == "/user_availability_schedules"
    assert captured["params"]["user"] == "u1"
    assert result == {"collection": []}


def test_list_availability_schedules_4xx_raises_calendly_client_error(monkeypatch):
    def fake_get(self, url, **kwargs):
        if url == "/users/me":
            return _mock_response({"resource": {"uri": "u1", "current_organization": "org-1"}})
        return _mock_response({"message": "denied"}, 403)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError):
        client.list_availability_schedules()


def test_create_scheduling_link_posts_expected_body(monkeypatch):
    posted = {}

    def fake_post(self, url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return _mock_response({"resource": {"booking_url": "https://calendly.com/d/abc-123", "owner": "https://api.calendly.com/event_types/ET1", "owner_type": "EventType"}})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = CalendlyClient(_cfg())
    result = client.create_scheduling_link("https://api.calendly.com/event_types/ET1")

    assert posted["url"] == "/scheduling_links"
    assert posted["json"] == {"max_event_count": 1, "owner": "https://api.calendly.com/event_types/ET1", "owner_type": "EventType"}
    assert result["booking_url"] == "https://calendly.com/d/abc-123"


def test_create_scheduling_link_4xx_raises_calendly_client_error(monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: _mock_response({"message": "denied"}, 403))
    client = CalendlyClient(_cfg())
    with pytest.raises(CalendlyClientError):
        client.create_scheduling_link("https://api.calendly.com/event_types/ET1")


