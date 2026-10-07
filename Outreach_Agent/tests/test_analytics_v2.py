"""Analytics dashboard endpoints (/analytics/v2/*) and sender attribution."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from outreach import api, db
from outreach.analytics import routes as analytics_routes
from outreach.channels import sender_identity
from outreach.config import Config

A = "a@csg.com"
B = "b@csg.com"
C = "c@csg.com"


def _item(i, campaign, sender, *, stage="sent", sent="2026-03-02T10:00:00+00:00", **extra):
    row = {
        "item_id": i, "campaign_id": campaign, "research_domain": f"{i}.com", "stage": stage,
        "channel": "email", "to_email": f"{i}@x.com", "sent_at": sent, "stage_updated_at": sent,
        "sender_email": sender, "followup_count": 0,
    }
    row.update(extra)
    return row


def _msg(i, item, intent, at="2026-03-04T09:00:00+00:00", direction="inbound"):
    return {"message_id": i, "item_id": item, "direction": direction, "intent": intent,
            "received_at": at, "created_at": at}


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(analytics_routes.db, "connect", lambda: conn)
    monkeypatch.setattr(analytics_routes, "_configured_senders", lambda: [A, B, C])
    conn.tables["campaigns"] = [
        {"campaign_id": "c1", "name": "Camp 1", "created_at": "2026-03-01T00:00:00+00:00"},
        {"campaign_id": "c2", "name": "Camp 2", "created_at": "2026-03-01T00:00:00+00:00"},
    ]
    conn.tables["outreach_items"] = [
        _item("i1", "c1", A, stage="replied"),
        _item("i2", "c1", A, stage="booked"),
        _item("i3", "c1", A),
        _item("i4", "c2", B, stage="not_interested"),
        _item("i5", "c2", B),
        _item("i6", "c2", None),  # legacy, sent before sender tracking
        _item("i7", "c2", B, stage="failed", sent=None, stage_updated_at="2026-03-03T10:00:00+00:00",
              error="smtp down"),
        _item("i8", "c1", A, sent="2026-02-01T10:00:00+00:00"),  # outside March window
    ]
    conn.tables["conversation_messages"] = [
        _msg("m1", "i1", "interested"),
        _msg("m2", "i2", "booked"),
        _msg("m3", "i4", "not_interested"),
        _msg("m4", "i5", "bounce"),
        _msg("m5", "i3", "out_of_office"),  # never counts as a reply
        _msg("m6", "i1", None, direction="outbound"),
    ]
    conn.tables["followup_attempts"] = [
        {"attempt_id": "f1", "item_id": "i1", "followup_number": 1, "status": "sent",
         "updated_at": "2026-03-03T09:00:00+00:00"},
        {"attempt_id": "f2", "item_id": "i3", "followup_number": 1, "status": "sent",
         "updated_at": "2026-03-03T09:00:00+00:00"},
        {"attempt_id": "f3", "item_id": "i3", "followup_number": 2, "status": "failed",
         "updated_at": "2026-03-04T09:00:00+00:00"},
    ]
    conn.tables["calendly_events"] = [
        {"event_uri": "e1", "item_id": "i2", "status": "active", "invitee_email": "i2@x.com",
         "starts_at": "2026-03-10T10:00:00+00:00", "created_at": "2026-03-05T09:00:00+00:00",
         "source": "sync", "no_show": False},
        {"event_uri": "e2", "item_id": None, "status": "active", "invitee_email": "stranger@x.com",
         "starts_at": "2026-03-11T10:00:00+00:00", "created_at": "2026-03-05T09:00:00+00:00",
         "source": "webhook", "no_show": False},
    ]
    return TestClient(api.app)


MARCH = {"date_from": "2026-03-01", "date_to": "2026-03-31"}


def _acct(data, sender):
    return next(a for a in data["accounts"] if a["sender"] == sender)


def test_overall_totals_and_rates(client):
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    o = data["overall"]
    # i8 is outside the window; i7 never sent.
    assert o["sent"] == 6
    assert o["replies"] == 3          # i1, i2, i4 (i3's out-of-office and i5's bounce are not replies)
    assert o["positive_replies"] == 2  # i1 interested, i2 booked
    assert o["bookings"] == 1
    assert o["follow_ups_sent"] == 2   # the failed attempt is not counted
    assert o["failed"] == 1
    assert o["bounced"] == 1
    assert o["reply_rate"]["pct"] == 50.0
    assert o["booking_rate"] == {"n": 1, "d": 6, "pct": 16.7, "low_sample": True}
    assert o["failure_rate"]["d"] == 7  # failed / (sent + failed)
    assert data["errors"] == {}


def test_per_account_attribution_and_future_accounts(client):
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    a, b, c = _acct(data, A), _acct(data, B), _acct(data, C)
    assert (a["sent"], a["replies"], a["bookings"], a["follow_ups_sent"]) == (3, 2, 1, 2)
    assert (b["sent"], b["replies"], b["failed"], b["bounced"]) == (2, 1, 1, 1)
    # A configured-but-idle account still appears, zeroed, with a null rate.
    assert c["sent"] == 0 and c["configured"] and c["reply_rate"]["pct"] is None
    # Pre-tracking sends are reported, not guessed.
    legacy = _acct(data, "unknown")
    assert legacy["label"] == "Unknown / legacy" and legacy["sent"] == 1
    assert sum(x["sent"] for x in data["accounts"]) == data["overall"]["sent"]


def test_new_sender_in_data_appears_automatically(client, conn):
    conn.tables["outreach_items"].append(_item("i9", "c1", "new@csg.com"))
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    assert _acct(data, "new@csg.com")["sent"] == 1


def test_filters_combine(client):
    data = client.get("/analytics/v2/summary", params={**MARCH, "sender": A, "campaign_id": "c1"}).json()
    assert data["overall"]["sent"] == 3
    assert [c["campaign_id"] for c in data["campaigns"]] == ["c1"]
    assert data["campaigns"][0]["senders"] == [A]
    assert data["bookings"]["total"] == 1 and data["bookings"]["unmatched"] == 0

    legacy = client.get("/analytics/v2/summary", params={**MARCH, "sender": "unknown"}).json()
    assert legacy["overall"]["sent"] == 1


def test_date_window_excludes_old_sends(client):
    data = client.get("/analytics/v2/summary", params={"date_from": "2026-01-15", "date_to": "2026-02-28"}).json()
    assert data["overall"]["sent"] == 1  # only i8


def test_campaign_table(client):
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    rows = {c["campaign_id"]: c for c in data["campaigns"]}
    assert rows["c1"]["prospects"] == 4 and rows["c1"]["sent"] == 3
    assert rows["c2"]["sent"] == 3 and rows["c2"]["failed"] == 1
    assert set(rows["c2"]["senders"]) == {B, "unknown"}


def test_followup_performance(client):
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    rows = {r["followup_number"]: r for r in data["followups"]["rows"]}
    # i1 replied 03-04, after its 03-03 follow-up; i3 (follow-up #1 too) never replied.
    assert rows[1]["sent"] == 2 and rows[1]["replied_after"] == 1


def test_bookings_include_unmatched_only_when_unfiltered(client):
    all_rows = client.get("/analytics/v2/summary", params=MARCH).json()["bookings"]
    assert all_rows["total"] == 2 and all_rows["unmatched"] == 1
    matched = next(r for r in all_rows["rows"] if r["matched"])
    assert matched["sender"] == A and matched["campaign_name"] == "Camp 1"
    assert all_rows["trend"] == [{"date": "2026-03-05", "active": 2, "canceled": 0}]

    scoped = client.get("/analytics/v2/summary", params={**MARCH, "sender": B}).json()["bookings"]
    assert scoped["total"] == 0


def test_canceled_booking_does_not_count_as_booked(client, conn):
    conn.tables["calendly_events"][0]["status"] = "canceled"
    conn.tables["outreach_items"][1]["stage"] = "waiting"  # cancel reverts the stage
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    assert data["overall"]["bookings"] == 0
    assert data["bookings"]["canceled"] == 1


def test_trend_by_event_date(client):
    trend = {d["date"]: d for d in client.get("/analytics/v2/summary", params=MARCH).json()["trend"]}
    assert trend["2026-03-02"]["sent"] == 6
    assert trend["2026-03-04"]["replies"] == 3
    assert trend["2026-03-05"]["bookings"] == 1


def test_prospect_drilldown(client):
    data = client.get("/analytics/v2/prospects", params={**MARCH, "sender": A, "campaign_id": "c1"}).json()
    assert data["total"] == 3
    rows = {r["item_id"]: r for r in data["rows"]}
    assert rows["i1"]["replied"] and rows["i1"]["positive"] and rows["i1"]["follow_ups"] == 1
    assert rows["i2"]["booked"] and rows["i3"]["bounced"] is False
    failed = client.get("/analytics/v2/prospects", params={**MARCH, "sender": B}).json()
    assert any(r["failed"] and r["error"] == "smtp down" for r in failed["rows"])


def test_one_broken_block_does_not_break_the_page(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendly table missing")
    monkeypatch.setattr(analytics_routes.metrics, "bookings_block", boom)
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    assert "bookings_block" in data["errors"]
    assert data["overall"]["sent"] == 6 and data["accounts"]


def test_secondary_table_failure_degrades(client, conn, monkeypatch):
    real = conn.select

    def select(table, **kw):
        if table == "calendly_events":
            raise RuntimeError("no such table")
        return real(table, **kw)
    monkeypatch.setattr(conn, "select", select)
    data = client.get("/analytics/v2/summary", params=MARCH).json()
    assert "bookings" in data["errors"] and data["overall"]["sent"] == 6


def test_paging_past_the_row_cap(conn, monkeypatch):
    monkeypatch.setattr(analytics_routes, "PAGE", 7)
    conn.tables["outreach_items"] = [_item(f"x{n:03d}", "c1", A) for n in range(30)]
    rows = analytics_routes.fetch_all(conn, "outreach_items", "item_id")
    assert len(rows) == 30 and len({r["item_id"] for r in rows}) == 30


def test_route_is_mounted():
    paths = TestClient(api.app).get("/openapi.json").json()["paths"]
    assert "/analytics/v2/summary" in paths and "/analytics/v2/prospects" in paths
    assert "/analytics" in paths  # the existing endpoint is untouched


# --- sender attribution at send time -----------------------------------------

def _cfg(tmp_path, monkeypatch, accounts=None):
    for k in ("OUTREACH_FROM_EMAIL", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "B_SMTP_USERNAME", "B_SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    sending = {"from_email": A, "from_name": "A Sender", "dry_run": False}
    if accounts:
        sending["accounts"] = accounts
    return Config(raw={"sending": sending, "personalization": {}}, path=tmp_path)


def test_single_account_config_is_unchanged(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert [a.identity for a in cfg.accounts] == [A]
    assert sender_identity(cfg) == A
    assert sender_identity(cfg, "ghost@x.com") == A  # unknown -> default account


def test_additional_accounts_resolve_and_fall_back(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, accounts=[
        {"from_email": B, "env_prefix": "B"},
        {"from_email": A, "env_prefix": "DUP"},  # duplicate of the default is ignored
        {"from_email": "", "env_prefix": "X"},   # incomplete entries are ignored
    ])
    monkeypatch.setenv("B_SMTP_USERNAME", "b-login")
    monkeypatch.setenv("B_SMTP_PASSWORD", "pw")
    assert [a.from_email for a in cfg.accounts] == [A, B]
    assert sender_identity(cfg, B.upper()) == B
    assert cfg.account_for(B).smtp_username == "b-login"
    assert cfg.account_for(None).is_default


def test_pipeline_stamps_the_actual_sender(conn, tmp_path, monkeypatch):
    from outreach import pipeline
    from outreach.models import Campaign, ImportedProspect, OutreachItem

    cfg = _cfg(tmp_path, monkeypatch, accounts=[{"from_email": B, "env_prefix": "B"}])
    cfg.raw["sending"]["rate_limit_per_minute"] = 0
    sent_with = []
    monkeypatch.setattr(pipeline, "send_via_channel", lambda cfg, ch, **kw: sent_with.append(kw["sender_email"]) or "mid-1")
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="p.com", name="P", contact_email="p@p.com", tier="A", score=1,
    ))
    db.create_campaign(conn, Campaign(campaign_id="c1", name="C", channel="email"))
    db.create_item(conn, OutreachItem(
        item_id="it1", campaign_id="c1", research_domain="p.com", stage="pending",
        subject="s", body="b", to_email="p@p.com",
    ))
    pipeline.send_campaign(conn, cfg, "c1", sender_email=B)
    assert sent_with == [B]
    assert db.get_item(conn, "it1").sender_email == B


def test_failed_send_is_still_attributed(conn, tmp_path, monkeypatch):
    from outreach import pipeline
    from outreach.channels import SendError
    from outreach.models import Campaign, ImportedProspect, OutreachItem

    cfg = _cfg(tmp_path, monkeypatch)
    cfg.raw["sending"]["rate_limit_per_minute"] = 0

    def fail(*a, **k):
        raise SendError("smtp down")
    monkeypatch.setattr(pipeline, "send_via_channel", fail)
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="p.com", name="P", contact_email="p@p.com", tier="A", score=1,
    ))
    db.create_campaign(conn, Campaign(campaign_id="c1", name="C", channel="email"))
    db.create_item(conn, OutreachItem(
        item_id="it1", campaign_id="c1", research_domain="p.com", stage="pending",
        subject="s", body="b", to_email="p@p.com",
    ))
    pipeline.send_campaign(conn, cfg, "c1")
    item = db.get_item(conn, "it1")
    assert item.stage == "failed" and item.sender_email == A


def test_create_item_omits_empty_sender_column(conn):
    from outreach.models import OutreachItem
    db.create_item(conn, OutreachItem(item_id="z", campaign_id="c", research_domain="z.com"))
    assert "sender_email" not in conn.tables["outreach_items"][0]
