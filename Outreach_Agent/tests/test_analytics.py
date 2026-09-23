"""GET /analytics -- _aggregate_analytics's stage_funnel, counts, and rate
math, with particular attention to the not_interested ("Failed") stage: it
must show up in the funnel, count correctly, and be reflected in reply_rate/
failure_rate without disturbing booking_rate or the existing stages' counts.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from outreach import api, db
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(api.db, "connect", lambda *a, **kw: conn)
    return TestClient(api.app)


def _item(item_id: str, domain: str, stage: str, *, sent: bool = True) -> OutreachItem:
    return OutreachItem(
        item_id=item_id, campaign_id="camp-1", research_domain=domain,
        stage=stage, to_email=f"cto@{domain}",
        sent_at=NOW if sent else None,
        created_at=NOW, stage_updated_at=NOW,
    )


def _seed(conn, stages: list[str]) -> None:
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    for i, stage in enumerate(stages):
        domain = f"acme{i}.com"
        db.upsert_prospect(conn, ImportedProspect(
            research_domain=domain, name=f"Acme {i}", tier="A", industry="SaaS",
            contact_email=f"cto@{domain}",
        ))
        db.create_item(conn, _item(f"item-{i}", domain, stage, sent=stage not in ("pending", "needs_review")))


def test_stage_funnel_includes_not_interested(client, conn):
    _seed(conn, ["sent", "waiting", "replied", "booked", "not_interested"])

    r = client.get("/analytics?campaign_id=camp-1")
    assert r.status_code == 200
    funnel = {row["stage"]: row["count"] for row in r.json()["stage_funnel"]}

    assert funnel["not_interested"] == 1
    assert funnel["booked"] == 1
    assert funnel["sent"] == 1
    assert funnel["waiting"] == 1
    assert funnel["replied"] == 1


def test_not_interested_count_reported_separately_from_send_failed_count(client, conn):
    """not_interested (rejection) and failed (send error) are different
    concepts and must not be conflated in the analytics payload."""
    _seed(conn, ["not_interested", "not_interested", "failed"])

    r = client.get("/analytics?campaign_id=camp-1")
    body = r.json()

    assert body["not_interested_count"] == 2
    assert body["failed_count"] == 1


def test_reply_rate_counts_not_interested_as_a_reply(client, conn):
    """A rejection is still a human reply -- reply_rate must count it, same
    convention the Learning/Optimization agents use (outcome_summary's
    `replied` flag)."""
    _seed(conn, ["sent", "sent", "not_interested", "booked"])
    # sent_total = 4 (sent, sent, not_interested, booked); replied = booked + not_interested = 2
    r = client.get("/analytics?campaign_id=camp-1")
    body = r.json()

    assert body["sent_total"] == 4
    assert body["reply_rate"] == 50.0  # 2 of 4


def test_booking_rate_unaffected_by_not_interested(client, conn):
    _seed(conn, ["booked", "not_interested", "not_interested", "sent"])
    r = client.get("/analytics?campaign_id=camp-1")
    body = r.json()

    assert body["sent_total"] == 4
    assert body["booking_rate"] == 25.0  # 1 of 4
    assert body["failure_rate"] == 50.0  # 2 of 4


def test_failure_rate_zero_with_no_rejections(client, conn):
    _seed(conn, ["sent", "replied", "booked"])
    r = client.get("/analytics?campaign_id=camp-1")
    assert r.json()["failure_rate"] == 0.0


def test_needs_review_and_pending_excluded_from_sent_total(client, conn):
    """Pre-send stages must not count toward sent_total/reply_rate/
    failure_rate math -- only items that actually went out."""
    _seed(conn, ["pending", "needs_review", "not_interested"])
    r = client.get("/analytics?campaign_id=camp-1")
    body = r.json()

    assert body["sent_total"] == 1
    assert body["failure_rate"] == 100.0
    assert body["total_items"] == 3
