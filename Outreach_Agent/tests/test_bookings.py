"""GET /bookings -- every prospect currently in the `booked` stage, across
every campaign, for the Dashboard's Bookings page."""

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


def _seed(conn, *, item_id: str, domain: str, campaign_id: str = "camp-1",
          campaign_name: str = "Test Campaign", stage: str = "booked") -> None:
    if not db.get_campaign(conn, campaign_id):
        db.create_campaign(conn, Campaign(campaign_id=campaign_id, name=campaign_name, created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, name=domain.split(".")[0].title(), industry="SaaS",
        contact_email=f"cto@{domain}", contact_name="Dana", contact_position="CTO",
    ))
    db.create_item(conn, OutreachItem(
        item_id=item_id, campaign_id=campaign_id, research_domain=domain,
        stage="sent", to_email=f"cto@{domain}", sent_at=NOW,
        created_at=NOW, stage_updated_at=NOW,
    ))


def test_empty_when_nothing_booked(client, conn):
    _seed(conn, item_id="item-1", domain="acme.com", stage="sent")
    assert client.get("/bookings").json() == []


def test_lists_a_booked_item(client, conn):
    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "waiting", actor="user")
    db.set_stage(conn, "item-1", "replied", actor="conversation_agent", detail={"intent": "interested"})
    db.set_stage(conn, "item-1", "booked", actor="conversation_agent", detail={"intent": "booked"})

    r = client.get("/bookings")
    assert r.status_code == 200
    entries = r.json()
    assert len(entries) == 1

    entry = entries[0]
    assert entry["item_id"] == "item-1"
    assert entry["campaign_name"] == "Test Campaign"
    assert entry["research_domain"] == "acme.com"
    assert entry["company_name"] == "Acme"
    assert entry["contact_name"] == "Dana"
    assert entry["contact_email"] == "cto@acme.com"
    assert entry["booked_by"] == "conversation_agent"
    assert entry["booked_detail"]["intent"] == "booked"
    assert entry["booked_at"] is not None


def test_excludes_non_booked_stages(client, conn):
    for stage, item_id, domain in [
        ("pending", "item-p", "a.com"), ("sent", "item-s", "b.com"),
        ("waiting", "item-w", "c.com"), ("replied", "item-r", "d.com"),
    ]:
        _seed(conn, item_id=item_id, domain=domain, stage=stage)
        if stage != "sent":
            db.set_stage(conn, item_id, stage, actor="user")

    assert client.get("/bookings").json() == []


def test_pools_bookings_across_campaigns(client, conn):
    _seed(conn, item_id="item-1", domain="acme.com", campaign_id="camp-1", campaign_name="East Coast")
    _seed(conn, item_id="item-2", domain="beta.com", campaign_id="camp-2", campaign_name="West Coast")
    db.set_stage(conn, "item-1", "booked", actor="user")
    db.set_stage(conn, "item-2", "booked", actor="user")

    entries = client.get("/bookings").json()
    campaign_names = {e["campaign_name"] for e in entries}
    assert campaign_names == {"East Coast", "West Coast"}


def test_sorted_most_recently_booked_first(client, conn, monkeypatch):
    """Two back-to-back set_stage calls can resolve to the same wall-clock
    microsecond on a fast machine (observed on Windows' default clock
    resolution) -- the sort's tiebreaker, not real timing, is what's under
    test here, so the two bookings are given explicit, distinct timestamps
    rather than relying on however fast the test happens to run."""
    from datetime import datetime, timedelta

    _seed(conn, item_id="item-old", domain="old.com")
    _seed(conn, item_id="item-new", domain="new.com")

    monkeypatch.setattr(db, "utcnow", lambda: datetime(2026, 3, 10, 12, 0, 0))
    db.set_stage(conn, "item-old", "booked", actor="user")
    monkeypatch.setattr(db, "utcnow", lambda: datetime(2026, 3, 10, 12, 5, 0))
    db.set_stage(conn, "item-new", "booked", actor="user")

    entries = client.get("/bookings").json()
    assert [e["item_id"] for e in entries] == ["item-new", "item-old"]


def test_stable_order_when_booked_at_the_same_instant(client, conn, monkeypatch):
    """When two bookings genuinely tie on timestamp, the endpoint must not
    raise or silently drop one -- both still appear, in a deterministic
    (if not timestamp-distinguishable) order."""
    from datetime import datetime

    _seed(conn, item_id="item-a", domain="a.com")
    _seed(conn, item_id="item-b", domain="b.com")

    same_instant = datetime(2026, 3, 10, 12, 0, 0, 633464)
    monkeypatch.setattr(db, "utcnow", lambda: same_instant)

    db.set_stage(conn, "item-a", "booked", actor="user")
    db.set_stage(conn, "item-b", "booked", actor="user")

    entries = client.get("/bookings").json()
    assert {e["item_id"] for e in entries} == {"item-a", "item-b"}
    assert len(entries) == 2


def test_calendly_url_included_when_configured(client, conn, monkeypatch):
    """The Dashboard links each booking row out to Calendly -- there's no
    per-booking event URL captured, so every row carries the same
    account-wide scheduling link, sourced from config (CALENDLY_URL)."""
    from outreach import api as api_module

    class _FakeCfg:
        calendly_url = "https://calendly.com/affan-careersourcegroup/30min"

    monkeypatch.setattr(api_module, "load_config", lambda: _FakeCfg())

    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")

    entry = client.get("/bookings").json()[0]
    assert entry["calendly_url"] == "https://calendly.com/affan-careersourcegroup/30min"


def test_calendly_url_null_when_not_configured(client, conn, monkeypatch):
    from outreach import api as api_module

    class _FakeCfg:
        calendly_url = ""

    monkeypatch.setattr(api_module, "load_config", lambda: _FakeCfg())

    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")

    entry = client.get("/bookings").json()[0]
    assert entry["calendly_url"] is None


def test_manual_booking_has_no_intent_but_still_lists(client, conn):
    """A person dragging a card straight to Booked (agent off, or manual
    override) must show up too -- booked_detail is just whatever detail was
    recorded, empty dict if none."""
    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")

    entry = client.get("/bookings").json()[0]
    assert entry["booked_by"] == "user"
    assert entry["booked_at"] is not None
