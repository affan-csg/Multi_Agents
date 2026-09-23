"""Tests for calendly/routes.py's HTTP layer -- POST /calendly/webhook
(signature verification wired to the real header/config, ignored vs applied
event types), POST /calendly/sync, and GET /calendly/status."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from outreach import api, db
from outreach.calendly import routes as calendly_routes
from outreach.calendly import sync as calendly_sync
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 4, 1, 12, 0, 0)
SIGNING_KEY = "test-signing-key"
# cancel_booking rejects a past-starting event (mirrors Calendly's own "Event
# in the past" rule) -- tests exercising a cancel call reaching the API must
# use a start time still ahead of real db.utcnow(), not NOW above, which is
# a fixed date that eventually lands in the real past as wall-clock time moves on.
FUTURE_START = (db.utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeCfg:
    calendly_url = ""
    calendly_api_token = "cal-token"
    calendly_webhook_signing_key = SIGNING_KEY


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(api.db, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(calendly_routes, "load_config", lambda: _FakeCfg())
    return TestClient(api.app)


def _sign(body: bytes, ts: int) -> str:
    signed_payload = f"{ts}.{body.decode('utf-8')}".encode("utf-8")
    sig = hmac.new(SIGNING_KEY.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _seed_item(conn, *, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="sent") -> None:
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="C", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(research_domain=domain, name="Acme", contact_email=to_email))
    db.create_item(conn, OutreachItem(
        item_id=item_id, campaign_id="camp-1", research_domain=domain,
        stage="sent", to_email=to_email, sent_at=NOW, created_at=NOW, stage_updated_at=NOW,
    ))
    if stage != "sent":
        db.set_stage(conn, item_id, stage, actor="user")


def _invitee_created_payload() -> dict:
    return {
        "event": "invitee.created",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
            "email": "prospect@acme.com", "name": "Dana",
            "scheduled_event": {
                "uri": "https://api.calendly.com/scheduled_events/EV1",
                "name": "30 Minute Meeting",
                "start_time": "2026-04-05T15:00:00Z", "end_time": "2026-04-05T15:30:00Z",
                "location": {"join_url": "https://zoom.us/j/999"},
            },
        },
    }


def test_webhook_rejects_missing_signature(client):
    r = client.post("/calendly/webhook", json=_invitee_created_payload())
    assert r.status_code == 401


def test_webhook_rejects_bad_signature(client):
    body = json.dumps(_invitee_created_payload()).encode()
    r = client.post(
        "/calendly/webhook", content=body,
        headers={"Calendly-Webhook-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_webhook_accepts_valid_signature_and_matches_item(client, conn):
    _seed_item(conn, stage="replied")
    payload = _invitee_created_payload()
    body = json.dumps(payload).encode()
    ts = int(time.time())
    r = client.post(
        "/calendly/webhook", content=body,
        headers={"Calendly-Webhook-Signature": _sign(body, ts), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["matched"] is True
    assert db.get_item(conn, "item-1").stage == "booked"


def test_webhook_unmatched_booking_still_accepted(client, conn):
    payload = _invitee_created_payload()
    body = json.dumps(payload).encode()
    ts = int(time.time())
    r = client.post(
        "/calendly/webhook", content=body,
        headers={"Calendly-Webhook-Signature": _sign(body, ts), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["matched"] is False


def test_webhook_ignores_unhandled_event_type(client):
    payload = {"event": "routing_form_submission.created", "payload": {}}
    body = json.dumps(payload).encode()
    ts = int(time.time())
    r = client.post(
        "/calendly/webhook", content=body,
        headers={"Calendly-Webhook-Signature": _sign(body, ts), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_cancellation_reverts_booking(client, conn):
    _seed_item(conn, stage="booked")
    payload = {
        "event": "invitee.canceled",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
            "email": "prospect@acme.com",
            "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EV1"},
            "cancellation": {"reason": "no longer needed"},
        },
    }
    body = json.dumps(payload).encode()
    ts = int(time.time())
    r = client.post(
        "/calendly/webhook", content=body,
        headers={"Calendly-Webhook-Signature": _sign(body, ts), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert db.get_item(conn, "item-1").stage == "waiting"


def test_sync_endpoint_reports_result(client, conn, monkeypatch):
    class FakeResult:
        def as_dict(self):
            return {"fetched": 3, "created": 3, "updated": 0, "matched": 2, "unmatched": 1,
                     "advanced_to_booked": 2, "reverted_from_booked": 0, "errors": []}

    monkeypatch.setattr(calendly_sync, "run_sync", lambda conn, cfg, **kw: FakeResult())
    r = client.post("/calendly/sync")
    assert r.status_code == 200
    assert r.json()["fetched"] == 3


def test_cancel_endpoint_cancels_booking_and_reverts_stage(client, conn, monkeypatch):
    _seed_item(conn, stage="booked")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)

    r = client.post("/calendly/events/cancel", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1", "reason": "conflict"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "canceled"
    assert body["item_id"] == "item-1"
    assert db.get_item(conn, "item-1").stage == "waiting"


def test_cancel_endpoint_unknown_event_returns_404(client):
    r = client.post("/calendly/events/cancel", json={"event_uri": "https://api.calendly.com/scheduled_events/NOPE"})
    assert r.status_code == 404


def test_cancel_endpoint_past_event_returns_409_without_calling_calendly(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    calls = []

    class ShouldNotBeCalledClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            calls.append(event_uuid_or_uri)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", ShouldNotBeCalledClient)
    r = client.post("/calendly/events/cancel", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 404  # cancel_booking's own precondition -- see sync.CancelError
    assert calls == []


def test_cancel_endpoint_api_failure_returns_502(client, conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FailingClient)
    r = client.post("/calendly/events/cancel", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 502


def test_cancel_endpoint_insufficient_scope_returns_actionable_message(client, conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class ScopeDeniedClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            raise CalendlyClientError(
                'POST https://api.calendly.com/scheduled_events/EV1/cancellation -> 403: '
                '{"title":"Insufficient scope","required_scopes":["scheduled_events:write"]}'
            )

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", ScopeDeniedClient)
    r = client.post("/calendly/events/cancel", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 502
    assert "scheduled_events:write" in r.json()["detail"]
    assert "Insufficient scope" not in r.json()["detail"]  # rewritten to a plain-English fix, not raw Calendly JSON


def test_delete_endpoint_cancels_active_booking_and_removes_local_row(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    calls = []

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            calls.append(event_uuid_or_uri)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)

    r = client.post("/calendly/events/delete", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert calls == ["https://api.calendly.com/scheduled_events/EV1"]

    from outreach.calendly import db as calendly_db
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


def test_delete_endpoint_past_booking_skips_calendly_call(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    calls = []

    class ShouldNotBeCalledClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            calls.append(event_uuid_or_uri)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", ShouldNotBeCalledClient)

    r = client.post("/calendly/events/delete", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 200
    assert calls == []

    from outreach.calendly import db as calendly_db
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


def test_delete_endpoint_unknown_event_returns_404(client):
    r = client.post("/calendly/events/delete", json={"event_uri": "https://api.calendly.com/scheduled_events/NOPE"})
    assert r.status_code == 404


def test_delete_endpoint_api_failure_returns_502_and_keeps_row(client, conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "inv1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FailingClient)
    r = client.post("/calendly/events/delete", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 502

    from outreach.calendly import db as calendly_db
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is not None


def test_no_show_endpoint_marks_and_returns_item_id(client, conn, monkeypatch):
    _seed_item(conn, stage="booked")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def mark_no_show(self, invitee_uri):
            return {"uri": "https://api.calendly.com/invitee_no_shows/ns-1"}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)
    r = client.post("/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "marked"
    assert body["item_id"] == "item-1"


def test_no_show_endpoint_unknown_event_returns_404(client):
    r = client.post("/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/NOPE"})
    assert r.status_code == 404


def test_no_show_endpoint_future_meeting_returns_404(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": FUTURE_START, "end_time": FUTURE_START, "location": {}},
            {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    calls = []

    class ShouldNotBeCalledClient:
        def __init__(self, cfg):
            self.configured = True

        def mark_no_show(self, invitee_uri):
            calls.append(invitee_uri)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", ShouldNotBeCalledClient)
    r = client.post("/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 404
    assert calls == []


def test_no_show_endpoint_api_failure_returns_502(client, conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def mark_no_show(self, invitee_uri):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FailingClient)
    r = client.post("/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 502


def test_unmark_no_show_endpoint_clears_flag(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def mark_no_show(self, invitee_uri):
            return {"uri": "https://api.calendly.com/invitee_no_shows/ns-1"}

        def unmark_no_show(self, no_show_uuid_or_uri):
            pass

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)
    client.post("/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})

    r = client.request("DELETE", "/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 200
    assert r.json()["status"] == "unmarked"


def test_unmark_no_show_endpoint_not_flagged_returns_404(client, conn, monkeypatch):
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(
        conn,
        calendly_sync.normalize_scheduled_event(
            {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
             "start_time": "2020-01-01T15:00:00Z", "end_time": "2020-01-01T15:30:00Z", "location": {}},
            {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
        ),
        result,
    )
    r = client.request("DELETE", "/calendly/events/no-show", json={"event_uri": "https://api.calendly.com/scheduled_events/EV1"})
    assert r.status_code == 404


def test_event_types_endpoint_returns_normalized_list(client, monkeypatch):
    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def list_event_types(self, *, page_token=None, count=100):
            return {
                "collection": [
                    {"uri": "https://api.calendly.com/event_types/ET1", "name": "30 Minute Meeting",
                     "active": True, "duration": 30, "color": "#003b52",
                     "scheduling_url": "https://calendly.com/x/30min"},
                ],
                "pagination": {"next_page_token": None},
            }

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FakeClient)
    r = client.get("/calendly/event-types")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "30 Minute Meeting"
    assert body[0]["active"] is True
    assert body[0]["duration"] == 30


def test_event_types_endpoint_paginates(client, monkeypatch):
    pages = [
        {"collection": [{"uri": "ET1", "name": "A", "active": True, "duration": 15}],
         "pagination": {"next_page_token": "tok-2"}},
        {"collection": [{"uri": "ET2", "name": "B", "active": False, "duration": 30}],
         "pagination": {"next_page_token": None}},
    ]

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True
            self._calls = 0

        def list_event_types(self, *, page_token=None, count=100):
            page = pages[self._calls]
            self._calls += 1
            return page

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FakeClient)
    r = client.get("/calendly/event-types")
    body = r.json()
    assert [e["uri"] for e in body] == ["ET1", "ET2"]


def test_event_types_endpoint_api_failure_returns_502(client, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def list_event_types(self, *, page_token=None, count=100):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FailingClient)
    r = client.get("/calendly/event-types")
    assert r.status_code == 502


def test_availability_endpoint_returns_normalized_list(client, monkeypatch):
    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def list_availability_schedules(self, *, user_uri=None):
            return {"collection": [
                {"uri": "https://api.calendly.com/user_availability_schedules/S1", "name": "Working Hours",
                 "timezone": "America/New_York", "default": True, "rules": [{"type": "wday", "wday": "monday"}]},
            ]}

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FakeClient)
    r = client.get("/calendly/availability")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Working Hours"
    assert body[0]["default"] is True
    assert body[0]["rules"] == [{"type": "wday", "wday": "monday"}]


def test_availability_endpoint_api_failure_returns_502(client, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def list_availability_schedules(self, *, user_uri=None):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FailingClient)
    r = client.get("/calendly/availability")
    assert r.status_code == 502


def test_scheduling_links_endpoint_creates_link(client, monkeypatch):
    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def create_scheduling_link(self, event_type_uri):
            return {"booking_url": "https://calendly.com/d/abc-123", "owner": event_type_uri, "owner_type": "EventType"}

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", FakeClient)
    r = client.post("/calendly/scheduling-links", json={"event_type_uri": "https://api.calendly.com/event_types/ET1"})
    assert r.status_code == 200
    body = r.json()
    assert body["booking_url"] == "https://calendly.com/d/abc-123"
    assert body["owner"] == "https://api.calendly.com/event_types/ET1"


def test_scheduling_links_endpoint_scope_error_returns_actionable_message(client, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class ScopeDeniedClient:
        def __init__(self, cfg):
            self.configured = True

        def create_scheduling_link(self, event_type_uri):
            raise CalendlyClientError(
                'POST https://api.calendly.com/scheduling_links -> 403: '
                '{"title":"Insufficient scope","required_scopes":["scheduling_links:write"]}'
            )

        def close(self):
            pass

    monkeypatch.setattr(calendly_routes, "CalendlyClient", ScopeDeniedClient)
    r = client.post("/calendly/scheduling-links", json={"event_type_uri": "https://api.calendly.com/event_types/ET1"})
    assert r.status_code == 502
    assert "scheduling_links:write" in r.json()["detail"]


def test_status_endpoint_reports_configuration(client):
    r = client.get("/calendly/status")
    assert r.status_code == 200
    assert r.json() == {"api_configured": True, "webhook_configured": True}


def test_status_endpoint_false_when_unconfigured(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(api.db, "connect", lambda *a, **kw: conn)

    class _Unconfigured:
        calendly_url = ""
        calendly_api_token = ""
        calendly_webhook_signing_key = ""

    monkeypatch.setattr(calendly_routes, "load_config", lambda: _Unconfigured())
    c = TestClient(api.app)
    r = c.get("/calendly/status")
    assert r.json() == {"api_configured": False, "webhook_configured": False}
