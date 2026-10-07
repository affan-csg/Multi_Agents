"""Calendly client retries and the incremental-sync behavior. No network:
the httpx client is replaced by a scripted fake and sleeping is stubbed."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from outreach import db
from outreach.calendly import client as client_mod
from outreach.calendly import db as calendly_db
from outreach.calendly import sync as calendly_sync
from outreach.calendly.client import CalendlyClient, CalendlyClientError
from outreach.calendly.models import CalendlyEvent
from outreach.config import Config
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 4, 1, 12, 0, 0)


def _cfg():
    return Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {},
                       "personalization": {}, "stages": {}}, path=None)


def _resp(status, body=None, headers=None):
    return httpx.Response(status, json=body or {}, headers=headers or {},
                          request=httpx.Request("GET", "https://api.calendly.com/x"))


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(client_mod, "_sleep", slept.append)
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")
    return slept


def _client(script):
    c = CalendlyClient(_cfg())
    queue = list(script)

    def get(path, **kw):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c._client.get = get
    c.calls_left = queue
    return c


def test_429_honors_retry_after_then_succeeds(sleeps):
    c = _client([_resp(429, headers={"Retry-After": "7"}), _resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}
    assert sleeps == [7.0]


def test_5xx_is_retried_with_backoff_then_succeeds(sleeps):
    c = _client([_resp(503), _resp(502), _resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}
    assert sleeps == [1.0, 2.0]


def test_timeouts_are_retried(sleeps):
    c = _client([httpx.ReadTimeout("slow"), _resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}


def test_persistent_5xx_exhausts_retries_and_raises(sleeps):
    c = _client([_resp(500)] * client_mod.MAX_ATTEMPTS)
    with pytest.raises(CalendlyClientError, match="500"):
        c._get("/x")
    assert len(sleeps) == client_mod.MAX_ATTEMPTS - 1


def test_persistent_timeout_raises_unreachable(sleeps):
    c = _client([httpx.ConnectTimeout("t")] * client_mod.MAX_ATTEMPTS)
    with pytest.raises(CalendlyClientError, match="unreachable"):
        c._get("/x")


def test_4xx_other_than_429_is_not_retried(sleeps):
    c = _client([_resp(401), _resp(200)])
    with pytest.raises(CalendlyClientError, match="401"):
        c._get("/x")
    assert sleeps == [] and len(c.calls_left) == 1


def test_retry_after_is_capped(sleeps):
    c = _client([_resp(429, headers={"Retry-After": "9999"}), _resp(200, {})])
    c._get("/x")
    assert sleeps == [client_mod._BACKOFF_CAP]


# --- incremental sync ------------------------------------------------------------------------------

def _seed(conn, stage="sent"):
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="C", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", name="A", contact_email="a@acme.com"))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com", stage="sent",
        to_email="a@acme.com", sent_at=NOW, created_at=NOW, stage_updated_at=NOW,
    ))
    if stage != "sent":
        db.set_stage(conn, "item-1", stage, actor="user")


def _event(**kw):
    f = dict(event_uri="https://api.calendly.com/scheduled_events/EV1", event_uuid="EV1",
             invitee_email="a@acme.com", status="active",
             starts_at=NOW + timedelta(days=3), ends_at=NOW + timedelta(days=3, minutes=30))
    f.update(kw)
    return CalendlyEvent(**f)


def test_unchanged_event_is_skipped(conn):
    _seed(conn)
    r1 = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), r1, skip_unchanged=True)
    assert r1.created == 1 and r1.advanced_to_booked == 1

    r2 = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), r2, skip_unchanged=True)
    assert r2.unchanged == 1 and r2.updated == 0 and r2.created == 0


def test_changed_event_is_applied(conn):
    _seed(conn)
    calendly_sync.apply_event(conn, _event(), calendly_sync.SyncResult(), skip_unchanged=True)
    r = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=NOW + timedelta(days=5)), r, skip_unchanged=True)
    assert r.updated == 1 and r.unchanged == 0


def test_move_stages_false_records_booking_without_moving_the_card(conn):
    _seed(conn)
    r = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), r, move_stages=lambda item: False)
    assert calendly_db.get_event(conn, _event().event_uri).item_id == "item-1"
    assert db.get_item(conn, "item-1").stage == "sent"
    assert r.advanced_to_booked == 0


def test_recorded_booking_moves_the_card_once_the_agent_is_on(conn):
    """Unchanged data is not skipped if the card does not yet reflect it."""
    _seed(conn)
    calendly_sync.apply_event(conn, _event(), calendly_sync.SyncResult(), move_stages=lambda i: False,
                              skip_unchanged=True)
    r = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), r, move_stages=lambda i: True, skip_unchanged=True)
    assert db.get_item(conn, "item-1").stage == "booked"
    assert r.unchanged == 0


def test_default_apply_event_behavior_is_unchanged(conn):
    """The manual button and webhook pass neither new argument."""
    _seed(conn)
    r = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), r)
    calendly_sync.apply_event(conn, _event(), r)
    assert db.get_item(conn, "item-1").stage == "booked"
    assert r.unchanged == 0 and r.updated == 1
