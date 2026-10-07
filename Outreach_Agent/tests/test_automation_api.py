"""Automation HTTP surface: status, interval, run-now, and the follow-up
fields on the conversation settings / drawer endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from outreach import api, db
from outreach.automation import routes as automation_routes
from outreach.automation import runner
from outreach.conversation import inbox
from outreach.conversation import routes as conv_routes
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def client(conn, monkeypatch):
    for mod in (db, api.db, conv_routes.db, automation_routes.db):
        monkeypatch.setattr(mod, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(inbox, "imap_configured", lambda: True)
    monkeypatch.setattr(inbox, "fetch_replies", lambda **kw: [])
    return TestClient(api.app)


def test_routes_are_mounted():
    paths = TestClient(api.app).get("/openapi.json").json()["paths"]
    assert "/automation/status" in paths and "/automation/run" in paths
    assert "patch" in paths["/automation/settings"]


def test_status_reports_defaults(client, monkeypatch):
    monkeypatch.delenv("AUTO_SYNC_ENABLED", raising=False)
    body = client.get("/automation/status").json()
    assert body["interval_minutes"] == 30
    assert body["scheduler_enabled"] is False
    assert body["last_success_at"] is None and body["next_run_at"] is None
    assert "locked_by" not in body


def test_run_updates_status_with_last_and_next_sync(client, conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", name="c", created_at=NOW))
    client.post("/campaigns/c1/conversation/settings", json={"enabled": True})
    out = client.post("/automation/run").json()
    assert out["result"]["status"] == "ok"
    st = out["status"]
    assert st["last_success_at"] and st["next_run_at"] and st["last_inbox_success_at"]
    assert st["running"] is False


def test_interval_can_be_changed_and_is_validated(client):
    assert client.patch("/automation/settings", json={"interval_minutes": 15}).json()["interval_minutes"] == 15
    assert client.patch("/automation/settings", json={"interval_minutes": 1}).status_code == 400
    assert client.patch("/automation/settings", json={"interval_minutes": 100000}).status_code == 400
    assert client.get("/automation/status").json()["interval_minutes"] == 15


def test_changing_interval_reschedules_from_the_last_start(client, conn):
    runner.ensure_state(conn)
    started = datetime(2026, 3, 10, 12, 0, 0)
    db.update_automation_state(conn, "sync", last_started_at=started, next_run_at=started + timedelta(minutes=30))
    body = client.patch("/automation/settings", json={"interval_minutes": 10}).json()
    assert body["next_run_at"].startswith("2026-03-10T12:10:00")


def test_sync_toggle_persists_and_does_not_reset_state(client, conn):
    runner.ensure_state(conn)
    db.update_automation_state(conn, "sync", last_success_at=NOW, interval_minutes=15)
    assert client.get("/automation/status").json()["enabled"] is True

    off = client.patch("/automation/settings", json={"enabled": False}).json()
    assert off["enabled"] is False and off["interval_minutes"] == 15
    assert off["last_success_at"].startswith("2026-03-10")
    assert db.get_automation_state(conn).enabled is False

    on = client.patch("/automation/settings", json={"enabled": True}).json()
    assert on["enabled"] is True and on["due"] is True


def test_loop_skips_cycles_while_off_but_manual_run_still_works(client, conn, monkeypatch):
    from outreach.automation import loop
    db.create_campaign(conn, Campaign(campaign_id="c1", name="c", created_at=NOW))
    client.post("/campaigns/c1/conversation/settings", json={"enabled": True})
    client.patch("/automation/settings", json={"enabled": False})
    assert loop._run_if_due() is None
    assert client.post("/automation/run").json()["result"]["status"] == "ok"
    client.patch("/automation/settings", json={"enabled": True})
    assert loop._run_if_due() is not None


# --- conversation settings + drawer -----------------------------------------------------------

def test_followup_settings_roundtrip_and_validation(client, conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", name="c", created_at=NOW))
    s = client.get("/campaigns/c1/conversation/settings").json()
    assert "auto_followup" not in s and s["followup_days"] == [7, 14] and s["no_response_after_days"] == 21

    ok = client.post("/campaigns/c1/conversation/settings", json={
        "followup_days": [5, 10], "no_response_after_days": 15,
    })
    assert ok.status_code == 200 and ok.json()["followup_days"] == [5, 10]

    for bad in ({"followup_days": [10, 5]}, {"followup_days": [0, 3]}, {"no_response_after_days": 10}):
        assert client.post("/campaigns/c1/conversation/settings", json=bad).status_code == 400


def test_item_conversation_exposes_followup_progress_and_next_action(client, conn):
    db.create_campaign(conn, Campaign(campaign_id="c1", name="c", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(research_domain="a.com", name="A"))
    sent_at = db.utcnow() - timedelta(days=3)
    db.create_item(conn, OutreachItem(
        item_id="i1", campaign_id="c1", research_domain="a.com", stage="waiting",
        to_email="x@a.com", sent_at=sent_at, created_at=sent_at, stage_updated_at=sent_at,
    ))
    f = client.get("/items/i1/conversation").json()["followup"]
    assert f["followups_sent"] == 0 and f["followups_total"] == 2
    assert f["next_action"] == "Follow-up #1"
    expected = sent_at + timedelta(days=7)
    assert abs(datetime.fromisoformat(f["next_action_at"]) - expected) < timedelta(minutes=1)
    assert "auto_followup" not in f
