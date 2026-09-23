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


# --- Calendly integration extension --------------------------------------------

def test_matched_calendly_event_enriches_booking_entry(client, conn):
    """A booked item with a matched calendly_events row surfaces the real
    per-booking event data (join link, real start/end time, source) instead
    of just the account-wide calendly_url."""
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/EV1", event_uuid="EV1",
        invitee_email="cto@acme.com", invitee_name="Dana", status="active",
        starts_at=NOW, ends_at=NOW, location="https://zoom.us/j/123",
        item_id="item-1", research_domain="acme.com",
    ))

    entry = client.get("/bookings").json()[0]
    assert entry["calendly_url"] == "https://zoom.us/j/123"
    assert entry["meeting_starts_at"] is not None
    assert entry["meeting_status"] == "active"
    assert entry["booking_source"] == "sync"
    assert entry["matched"] is True


def test_unmatched_calendly_booking_still_appears(client, conn):
    """A real Calendly booking that could not be matched to any pipeline
    item (booked through a channel outside this agent's outreach) must still
    show up on the Bookings page, per the integration's requirement."""
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/EV2", event_uuid="EV2",
        invitee_email="stranger@nowhere.com", invitee_name="Stranger", status="active",
        starts_at=NOW,
    ))

    entries = client.get("/bookings").json()
    assert len(entries) == 1
    assert entries[0]["item_id"] is None
    assert entries[0]["contact_email"] == "stranger@nowhere.com"
    assert entries[0]["booked_by"] == "calendly"
    assert entries[0]["matched"] is False


# --- Phase 1: search/filter/sort/pagination query params -----------------------

def test_bookings_default_matches_current_behavior_exactly(client, conn):
    """Regression pin, written before the query-param feature landed: calling
    /bookings with no params at all must keep returning exactly what it does
    today -- a flat array, active-only, newest-first. Every new query param
    below is required to be strictly additive/opt-in."""
    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")

    r = client.get("/bookings")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["item_id"] == "item-1"


def test_bookings_status_canceled_includes_reverted_matched_rows(client, conn):
    """A matched item that got reverted off `booked` on cancellation is
    dropped from the default (active) view -- but must reappear when the
    caller explicitly asks for status=canceled, since that's the whole point
    of being able to see canceled bookings at all."""
    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/EV1", event_uuid="EV1",
        invitee_email="cto@acme.com", invitee_name="Dana", status="canceled",
        starts_at=NOW, item_id="item-1", research_domain="acme.com",
    ))
    db.set_stage(conn, "item-1", "waiting", actor="calendly")

    default_entries = client.get("/bookings").json()
    assert default_entries == []

    canceled_entries = client.get("/bookings", params={"status": "canceled"}).json()
    assert len(canceled_entries) == 1
    assert canceled_entries[0]["meeting_status"] == "canceled"


def test_bookings_date_range_filters_by_meeting_starts_at(client, conn):
    from datetime import timedelta

    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, item_id="item-early", domain="early.com")
    db.set_stage(conn, "item-early", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/EARLY", event_uuid="EARLY",
        invitee_email="cto@early.com", status="active", starts_at=NOW - timedelta(days=10),
        item_id="item-early", research_domain="early.com",
    ))

    _seed(conn, item_id="item-late", domain="late.com")
    db.set_stage(conn, "item-late", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/LATE", event_uuid="LATE",
        invitee_email="cto@late.com", status="active", starts_at=NOW + timedelta(days=10),
        item_id="item-late", research_domain="late.com",
    ))

    entries = client.get("/bookings", params={"date_from": NOW.date().isoformat()}).json()
    assert {e["item_id"] for e in entries} == {"item-late"}


def test_bookings_search_matches_company_contact_campaign(client, conn):
    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")
    _seed(conn, item_id="item-2", domain="widgets.io")
    db.set_stage(conn, "item-2", "booked", actor="user")

    entries = client.get("/bookings", params={"search": "acme"}).json()
    assert {e["item_id"] for e in entries} == {"item-1"}


def test_bookings_sort_by_meeting_starts_at_asc(client, conn):
    from datetime import timedelta

    from outreach.calendly import db as calendly_db
    from outreach.calendly.models import CalendlyEvent

    _seed(conn, item_id="item-a", domain="a.com")
    db.set_stage(conn, "item-a", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/A", event_uuid="A",
        invitee_email="cto@a.com", status="active", starts_at=NOW + timedelta(days=5),
        item_id="item-a", research_domain="a.com",
    ))
    _seed(conn, item_id="item-b", domain="b.com")
    db.set_stage(conn, "item-b", "booked", actor="calendly")
    calendly_db.upsert_event(conn, CalendlyEvent(
        event_uri="https://api.calendly.com/scheduled_events/B", event_uuid="B",
        invitee_email="cto@b.com", status="active", starts_at=NOW + timedelta(days=1),
        item_id="item-b", research_domain="b.com",
    ))

    entries = client.get("/bookings", params={"sort": "meeting_starts_at", "order": "asc"}).json()
    assert [e["item_id"] for e in entries] == ["item-b", "item-a"]


def test_bookings_no_page_size_returns_flat_array(client, conn):
    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="user")

    r = client.get("/bookings", params={"page": 1})
    assert isinstance(r.json(), list)


def test_bookings_pagination_returns_envelope_when_page_size_present(client, conn):
    for i in range(3):
        _seed(conn, item_id=f"item-{i}", domain=f"d{i}.com")
        db.set_stage(conn, f"item-{i}", "booked", actor="user")

    r = client.get("/bookings", params={"page_size": 2})
    body = r.json()
    assert set(body.keys()) == {"items", "total", "page", "page_size"}
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2


def test_bookings_pagination_page_2_returns_next_slice(client, conn):
    for i in range(3):
        _seed(conn, item_id=f"item-{i}", domain=f"d{i}.com")
        db.set_stage(conn, f"item-{i}", "booked", actor="user")

    page1 = client.get("/bookings", params={"page_size": 2, "page": 1}).json()
    page2 = client.get("/bookings", params={"page_size": 2, "page": 2}).json()
    assert len(page2["items"]) == 1
    assert {e["item_id"] for e in page1["items"]} & {e["item_id"] for e in page2["items"]} == set()


def test_booking_without_matched_calendly_event_falls_back_to_account_url(client, conn, monkeypatch):
    """No calendly_events row at all (classifier-only path, or the
    integration not configured) -- pre-existing behavior must be unchanged:
    the shared account-wide calendly_url is used."""
    from outreach import api as api_module

    class _FakeCfg:
        calendly_url = "https://calendly.com/affan-careersourcegroup/30min"

    monkeypatch.setattr(api_module, "load_config", lambda: _FakeCfg())

    _seed(conn, item_id="item-1", domain="acme.com")
    db.set_stage(conn, "item-1", "booked", actor="conversation_agent", detail={"intent": "booked"})

    entry = client.get("/bookings").json()[0]
    assert entry["calendly_url"] == "https://calendly.com/affan-careersourcegroup/30min"
    assert entry["meeting_starts_at"] is None
    assert entry["booking_source"] is None
    assert entry["matched"] is True
