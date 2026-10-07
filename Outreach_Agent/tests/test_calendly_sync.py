"""Tests for calendly/sync.py -- normalization, prospect matching by email,
idempotent upsert (no duplicates on re-sync/re-delivery), cancellation
handling, and the resulting outreach_items stage sync."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.calendly import db as calendly_db
from outreach.calendly import sync as calendly_sync
from outreach.calendly.models import CalendlyEvent
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 4, 1, 12, 0, 0)
# A fixed NOW like the one above eventually lands in the real past as wall-clock
# time moves on -- cancel_booking legitimately rejects a past-starting event
# (mirroring Calendly's own "Event in the past" rule), so any test exercising
# a *successful* cancel must use a start time that stays ahead of real
# db.utcnow(), not the fixture's own NOW.
FUTURE = db.utcnow() + timedelta(days=30)
PAST_DT = db.utcnow() - timedelta(days=30)


def _seed_item(conn, *, item_id: str, domain: str, to_email: str, stage: str = "sent",
                campaign_id: str = "camp-1", sent_at=NOW) -> None:
    if not db.get_campaign(conn, campaign_id):
        db.create_campaign(conn, Campaign(campaign_id=campaign_id, name="C", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(research_domain=domain, name=domain, contact_email=to_email))
    db.create_item(conn, OutreachItem(
        item_id=item_id, campaign_id=campaign_id, research_domain=domain,
        stage="sent", to_email=to_email, sent_at=sent_at, created_at=NOW, stage_updated_at=NOW,
    ))
    if stage != "sent":
        db.set_stage(conn, item_id, stage, actor="user")


def _event(event_uri="https://api.calendly.com/scheduled_events/EV1", **overrides) -> CalendlyEvent:
    fields = dict(
        event_uri=event_uri, event_uuid=event_uri.rsplit("/", 1)[-1],
        invitee_email="prospect@acme.com", invitee_name="Dana",
        status="active", starts_at=NOW,
    )
    fields.update(overrides)
    return CalendlyEvent(**fields)


# --- matching ------------------------------------------------------------------

def test_matches_by_invitee_email_against_to_email(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com")
    item = calendly_sync.match_item(conn, "prospect@acme.com")
    assert item is not None
    assert item.item_id == "item-1"


def test_match_is_case_insensitive(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="Prospect@Acme.com")
    item = calendly_sync.match_item(conn, "prospect@acme.com")
    assert item is not None


def test_no_match_returns_none(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com")
    assert calendly_sync.match_item(conn, "nobody@nowhere.com") is None


def test_no_email_returns_none(conn):
    assert calendly_sync.match_item(conn, None) is None


def test_multiple_campaigns_same_person_picks_most_recent_send(conn):
    from datetime import timedelta

    _seed_item(conn, item_id="item-old", domain="a.com", to_email="dana@x.com",
               campaign_id="camp-1", sent_at=NOW - timedelta(days=5))
    _seed_item(conn, item_id="item-new", domain="b.com", to_email="dana@x.com",
               campaign_id="camp-2", sent_at=NOW)
    item = calendly_sync.match_item(conn, "dana@x.com")
    assert item.item_id == "item-new"


# --- apply_event: create/update idempotency -------------------------------------

def test_apply_event_matched_advances_item_to_booked(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="replied")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), result)

    item = db.get_item(conn, "item-1")
    assert item.stage == "booked"
    assert result.matched == 1
    assert result.advanced_to_booked == 1

    events = db.list_events(conn, "item-1")
    booked_event = next(e for e in events if e.to_stage == "booked")
    assert booked_event.actor == "calendly"


def test_apply_event_unmatched_still_recorded(conn):
    result = calendly_sync.SyncResult()
    stored = calendly_sync.apply_event(conn, _event(invitee_email="stranger@nowhere.com"), result)
    assert stored.item_id is None
    assert result.unmatched == 1
    assert result.matched == 0
    # still queryable -- unmatched bookings must still appear
    assert calendly_db.get_event(conn, stored.event_uri) is not None


def test_apply_event_is_idempotent_no_duplicate_rows(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="replied")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), result)
    calendly_sync.apply_event(conn, _event(), result)  # same event_uri again

    all_rows = conn.tables.get("calendly_events", [])
    assert len(all_rows) == 1
    assert result.created == 1
    assert result.updated == 1


def test_apply_event_does_not_re_advance_already_booked_item(conn):
    """A second sync pass over an item already in `booked` must not write a
    redundant stage_change event -- set_stage would still succeed, but
    _sync_item_stage's own `item.stage != "booked"` guard should short
    circuit before calling it a second time."""
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="booked")
    events_before = len(db.list_events(conn, "item-1"))

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), result)

    assert result.advanced_to_booked == 0
    assert len(db.list_events(conn, "item-1")) == events_before


# --- cancellations ---------------------------------------------------------------

def test_cancellation_reverts_booked_item_to_waiting(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="booked")

    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(status="canceled", cancel_reason="conflict"), result)

    item = db.get_item(conn, "item-1")
    assert item.stage == "waiting"
    assert result.reverted_from_booked == 1

    events = db.list_events(conn, "item-1")
    revert_event = next(e for e in events if e.to_stage == "waiting")
    assert revert_event.actor == "calendly"
    assert revert_event.detail["cancel_reason"] == "conflict"


def test_cancellation_of_non_booked_item_is_a_noop(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="replied")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(status="canceled"), result)

    item = db.get_item(conn, "item-1")
    assert item.stage == "replied"  # untouched
    assert result.reverted_from_booked == 0


# --- run_sync: pagination -----------------------------------------------------

def test_run_sync_walks_all_pages(conn, monkeypatch):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="a@acme.com")
    _seed_item(conn, item_id="item-2", domain="beta.com", to_email="b@beta.com", campaign_id="camp-2")

    # Active-status pages (paginated); the canceled-status pass (below) returns
    # nothing for this test -- run_sync queries both statuses (see its
    # docstring on why status can't be omitted), so a FakeClient must answer
    # per-status, not just per-call-count.
    active_pages = [
        {
            "collection": [{"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call", "start_time": "2026-04-01T15:00:00Z", "end_time": "2026-04-01T15:30:00Z", "location": {}}],
            "pagination": {"next_page_token": "page2"},
        },
        {
            "collection": [{"uri": "https://api.calendly.com/scheduled_events/EV2", "name": "Call", "start_time": "2026-04-02T15:00:00Z", "end_time": "2026-04-02T15:30:00Z", "location": {}}],
            "pagination": {"next_page_token": None},
        },
    ]
    invitees = {
        "https://api.calendly.com/scheduled_events/EV1": {"collection": [{"uri": "inv1", "email": "a@acme.com", "name": "A", "status": "active"}]},
        "https://api.calendly.com/scheduled_events/EV2": {"collection": [{"uri": "inv2", "email": "b@beta.com", "name": "B", "status": "active"}]},
    }

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True
            self._active_calls = 0

        def list_scheduled_events(self, *, status=None, **kwargs):
            if status == "canceled":
                return {"collection": [], "pagination": {"next_page_token": None}}
            page = active_pages[self._active_calls]
            self._active_calls += 1
            return page

        def list_event_invitees(self, event_uri, **kwargs):
            return invitees[event_uri]

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)

    from outreach.config import Config
    cfg = Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {}, "personalization": {}, "stages": {}}, path=None)
    result = calendly_sync.run_sync(conn, cfg)

    assert result.fetched == 2
    assert result.matched == 2
    assert db.get_item(conn, "item-1").stage == "booked"
    assert db.get_item(conn, "item-2").stage == "booked"


def test_run_sync_also_pulls_canceled_events(conn, monkeypatch):
    """Calendly's /scheduled_events defaults to active-only when `status` is
    omitted -- a booking canceled directly in Calendly (not through this
    app's own cancel button) would otherwise never be seen again by sync and
    would stay stuck locally at status="active" forever, still showing on the
    Bookings page. run_sync must query status="canceled" too and apply it."""
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="a@acme.com", stage="booked")
    calendly_db.upsert_event(conn, _event(status="active", item_id="item-1"))

    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def list_scheduled_events(self, *, status=None, **kwargs):
            if status == "canceled":
                return {
                    "collection": [{"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call", "start_time": "2026-04-01T15:00:00Z", "end_time": "2026-04-01T15:30:00Z", "location": {}}],
                    "pagination": {"next_page_token": None},
                }
            return {"collection": [], "pagination": {"next_page_token": None}}

        def list_event_invitees(self, event_uri, **kwargs):
            return {"collection": [{"uri": "inv1", "email": "a@acme.com", "name": "A", "status": "canceled", "cancellation": {"reason": "conflict"}}]}

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)

    from outreach.config import Config
    cfg = Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {}, "personalization": {}, "stages": {}}, path=None)
    result = calendly_sync.run_sync(conn, cfg)

    assert result.reverted_from_booked == 1
    stored = calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1")
    assert stored.status == "canceled"
    assert db.get_item(conn, "item-1").stage == "waiting"


def test_run_sync_without_token_reports_error_not_exception(conn, monkeypatch):
    class FakeClient:
        def __init__(self, cfg):
            self.configured = False

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", FakeClient)
    from outreach.config import Config
    cfg = Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {}, "personalization": {}, "stages": {}}, path=None)
    result = calendly_sync.run_sync(conn, cfg)
    assert result.fetched == 0
    assert any("CALENDLY_API_TOKEN" in e for e in result.errors)


# --- webhook ingestion ---------------------------------------------------------

def test_ingest_webhook_event_applies_normally(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="replied")
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": "inv1", "email": "prospect@acme.com", "name": "Dana",
            "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EV1", "start_time": "2026-04-01T15:00:00Z", "end_time": "2026-04-01T15:30:00Z"},
        },
    }
    event = calendly_sync.ingest_webhook_event(conn, payload)
    assert event is not None
    assert event.item_id == "item-1"
    assert db.get_item(conn, "item-1").stage == "booked"


def test_ingest_webhook_event_unrecognized_type_returns_none(conn):
    payload = {"event": "routing_form_submission.created", "payload": {}}
    assert calendly_sync.ingest_webhook_event(conn, payload) is None


# --- cancel_booking (Bookings page delete action) ------------------------------

def _cfg():
    from outreach.config import Config
    return Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {}, "personalization": {}, "stages": {}}, path=None)


class _FakeCancelClient:
    calls: list[tuple[str, str | None]] = []

    def __init__(self, cfg):
        self.configured = True

    def cancel_event(self, event_uuid_or_uri, *, reason=None):
        _FakeCancelClient.calls.append((event_uuid_or_uri, reason))
        return {}

    def close(self):
        pass


def test_cancel_booking_marks_local_row_canceled_and_reverts_stage(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="booked")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(item_id="item-1", starts_at=FUTURE), result)

    event = calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1", reason="no longer needed")

    assert event.status == "canceled"
    assert event.cancel_reason == "no longer needed"
    assert db.get_item(conn, "item-1").stage == "waiting"
    assert _FakeCancelClient.calls == [("https://api.calendly.com/scheduled_events/EV1", "no longer needed")]


def test_cancel_booking_calls_calendly_before_local_cancel(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=FUTURE), result)

    calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert len(_FakeCancelClient.calls) == 1


def test_cancel_booking_unknown_event_raises(conn):
    with pytest.raises(calendly_sync.CancelError):
        calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/NOPE")


def test_cancel_booking_past_event_raises_without_calling_calendly(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    past = db.utcnow() - timedelta(days=1)
    calendly_sync.apply_event(conn, _event(starts_at=past), result)

    with pytest.raises(calendly_sync.CancelError, match="already happened"):
        calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert _FakeCancelClient.calls == []  # rejected before ever calling Calendly


def test_cancel_booking_unknown_start_time_is_not_blocked_as_past(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=None), result)

    calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert len(_FakeCancelClient.calls) == 1


def test_cancel_booking_already_canceled_raises(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(status="canceled"), result)

    with pytest.raises(calendly_sync.CancelError):
        calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_cancel_booking_handles_timezone_aware_starts_at_from_real_row(conn, monkeypatch):
    """Regression test, same root cause as
    test_delete_booking_handles_timezone_aware_starts_at_from_real_row: a
    real calendly_events row's starts_at round-trips as a timezone-aware ISO
    string, and cancel_booking's past-event guard compares it against
    db.utcnow() (always naive)."""
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    future_aware = FUTURE.isoformat() + "+00:00"
    calendly_sync.apply_event(conn, _event(starts_at=future_aware), result)

    calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert len(_FakeCancelClient.calls) == 1


def test_cancel_booking_without_token_raises(conn, monkeypatch):
    class _Unconfigured:
        def __init__(self, cfg):
            self.configured = False

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _Unconfigured)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), result)

    with pytest.raises(calendly_sync.CancelError):
        calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_cancel_booking_api_failure_leaves_local_row_untouched(conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class _FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FailingClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=FUTURE), result)

    with pytest.raises(CalendlyClientError):
        calendly_sync.cancel_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


# --- delete_booking (Bookings page delete action) ------------------------------

def test_delete_booking_cancels_active_event_then_removes_local_row(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=FUTURE), result)

    calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert _FakeCancelClient.calls == [("https://api.calendly.com/scheduled_events/EV1", None)]
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


def test_delete_booking_past_event_skips_calendly_cancel(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    past = db.utcnow() - timedelta(days=1)
    calendly_sync.apply_event(conn, _event(starts_at=past), result)

    calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert _FakeCancelClient.calls == []  # already happened -- nothing to cancel
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


def test_delete_booking_already_canceled_event_skips_calendly_cancel(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(status="canceled", starts_at=FUTURE), result)

    calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert _FakeCancelClient.calls == []
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


def test_delete_booking_unknown_event_raises(conn):
    with pytest.raises(calendly_sync.DeleteBookingError):
        calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/NOPE")


def test_delete_booking_without_token_raises_and_leaves_row(conn, monkeypatch):
    class _Unconfigured:
        def __init__(self, cfg):
            self.configured = False

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _Unconfigured)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=FUTURE), result)

    with pytest.raises(calendly_sync.DeleteBookingError):
        calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is not None


def test_delete_booking_api_failure_leaves_local_row_untouched(conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class _FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def cancel_event(self, event_uuid_or_uri, *, reason=None):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FailingClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(starts_at=FUTURE), result)

    with pytest.raises(CalendlyClientError):
        calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is not None


def test_delete_booking_does_not_delete_matched_outreach_item(conn, monkeypatch):
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="booked")
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(item_id="item-1", starts_at=FUTURE), result)

    calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None
    assert db.get_item(conn, "item-1") is not None  # unlinked, not deleted


def test_delete_booking_handles_timezone_aware_starts_at_from_real_row(conn, monkeypatch):
    """Regression test: a real calendly_events row round-trips starts_at as a
    timezone-aware ISO string (e.g. "...+00:00", both from Calendly's own API
    and from Supabase), not the naive datetimes every other fixture in this
    file constructs directly. delete_booking's past/future check compares
    starts_at against db.utcnow() (always naive) -- before CalendlyEvent
    normalized incoming datetimes to naive UTC, that comparison raised
    TypeError: can't compare offset-naive and offset-aware datetimes,
    surfacing as an unhandled 500 from the delete endpoint."""
    _FakeCancelClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    result = calendly_sync.SyncResult()
    future_aware = FUTURE.isoformat() + "+00:00"
    calendly_sync.apply_event(conn, _event(starts_at=future_aware), result)

    calendly_sync.delete_booking(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert _FakeCancelClient.calls == [("https://api.calendly.com/scheduled_events/EV1", None)]
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1") is None


# --- mark_no_show / unmark_no_show (Bookings page no-show action) --------------

class _FakeNoShowClient:
    calls: list[str] = []
    unmark_calls: list[str] = []

    def __init__(self, cfg):
        self.configured = True

    def mark_no_show(self, invitee_uri):
        _FakeNoShowClient.calls.append(invitee_uri)
        return {"uri": "https://api.calendly.com/invitee_no_shows/ns-1"}

    def unmark_no_show(self, no_show_uuid_or_uri):
        _FakeNoShowClient.unmark_calls.append(no_show_uuid_or_uri)

    def close(self):
        pass


def test_mark_no_show_flags_event_and_persists_no_show_uri(conn, monkeypatch):
    _FakeNoShowClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/scheduled_events/EV1/invitees/INV1"), result)

    event = calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert event.no_show is True
    assert event.no_show_uri == "https://api.calendly.com/invitee_no_shows/ns-1"
    assert _FakeNoShowClient.calls == ["https://api.calendly.com/scheduled_events/EV1/invitees/INV1"]


def test_mark_no_show_future_event_raises_without_calling_calendly(conn, monkeypatch):
    _FakeNoShowClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1", starts_at=FUTURE), result)

    with pytest.raises(calendly_sync.NoShowError, match="hasn't happened"):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert _FakeNoShowClient.calls == []


def test_mark_no_show_handles_timezone_aware_starts_at_from_real_row(conn, monkeypatch):
    """Regression test, same root cause as
    test_cancel_booking_handles_timezone_aware_starts_at_from_real_row:
    mark_no_show's future-event guard also compares starts_at against
    db.utcnow() (always naive)."""
    _FakeNoShowClient.calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    future_aware = FUTURE.isoformat() + "+00:00"
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1", starts_at=future_aware), result)

    with pytest.raises(calendly_sync.NoShowError, match="hasn't happened"):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    assert _FakeNoShowClient.calls == []


def test_mark_no_show_already_flagged_raises(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1"), result)
    calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    with pytest.raises(calendly_sync.NoShowError, match="already marked"):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_mark_no_show_canceled_event_raises(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1", status="canceled"), result)

    with pytest.raises(calendly_sync.NoShowError):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_mark_no_show_unknown_event_raises(conn):
    with pytest.raises(calendly_sync.NoShowError):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/NOPE")


def test_mark_no_show_without_invitee_uri_raises(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(), result)  # no invitee_uri override

    with pytest.raises(calendly_sync.NoShowError, match="no invitee"):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_mark_no_show_api_failure_leaves_local_row_untouched(conn, monkeypatch):
    from outreach.calendly.client import CalendlyClientError

    class _FailingClient:
        def __init__(self, cfg):
            self.configured = True

        def mark_no_show(self, invitee_uri):
            raise CalendlyClientError("boom")

        def close(self):
            pass

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FailingClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1"), result)

    with pytest.raises(CalendlyClientError):
        calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")
    from outreach.calendly import db as calendly_db
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1").no_show is False


def test_unmark_no_show_clears_flag_and_calls_delete(conn, monkeypatch):
    _FakeNoShowClient.unmark_calls = []
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1"), result)
    calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    event = calendly_sync.unmark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    assert event.no_show is False
    assert event.no_show_uri is None
    assert _FakeNoShowClient.unmark_calls == ["https://api.calendly.com/invitee_no_shows/ns-1"]


def test_unmark_no_show_not_flagged_raises(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1"), result)

    with pytest.raises(calendly_sync.NoShowError, match="not marked"):
        calendly_sync.unmark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")


def test_unmark_no_show_unknown_event_raises(conn):
    with pytest.raises(calendly_sync.NoShowError):
        calendly_sync.unmark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/NOPE")


def test_resync_preserves_no_show_flag(conn, monkeypatch):
    """A re-sync/re-delivery normalizes a fresh CalendlyEvent from Calendly's
    own payload, which has no idea this integration separately flagged a
    no-show -- apply_event must carry the existing flag forward rather than
    silently clearing it on the next sync."""
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeNoShowClient)
    result = calendly_sync.SyncResult()
    calendly_sync.apply_event(conn, _event(invitee_uri="https://api.calendly.com/x/invitees/INV1"), result)
    calendly_sync.mark_no_show(conn, _cfg(), "https://api.calendly.com/scheduled_events/EV1")

    # Simulate a re-sync of the same event -- normalize_scheduled_event never
    # sets no_show, so apply_event must preserve it from the existing row.
    re_synced = calendly_sync.normalize_scheduled_event(
        {"uri": "https://api.calendly.com/scheduled_events/EV1", "name": "Call",
         "start_time": "2026-04-01T12:00:00Z", "end_time": "2026-04-01T12:30:00Z", "location": {}},
        {"uri": "https://api.calendly.com/x/invitees/INV1", "email": "prospect@acme.com", "name": "Dana", "status": "active"},
    )
    calendly_sync.apply_event(conn, re_synced, calendly_sync.SyncResult())

    from outreach.calendly import db as calendly_db
    assert calendly_db.get_event(conn, "https://api.calendly.com/scheduled_events/EV1").no_show is True


def test_ingest_webhook_event_duplicate_delivery_is_idempotent(conn):
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="replied")
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": "inv1", "email": "prospect@acme.com", "name": "Dana",
            "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EV1"},
        },
    }
    calendly_sync.ingest_webhook_event(conn, payload)
    calendly_sync.ingest_webhook_event(conn, payload)  # Calendly re-delivers on a retry

    assert len(conn.tables.get("calendly_events", [])) == 1


# --- persistent deletion (tombstones) -------------------------------------------

EV1 = "https://api.calendly.com/scheduled_events/EV1"


def _sync_client(status_for_ev1):
    class FakeClient:
        def __init__(self, cfg):
            self.configured = True

        def list_scheduled_events(self, *, status=None, **kwargs):
            if status == status_for_ev1:
                return {
                    "collection": [{"uri": EV1, "name": "Call", "start_time": "2026-04-01T15:00:00Z", "end_time": "2026-04-01T15:30:00Z", "location": {}}],
                    "pagination": {"next_page_token": None},
                }
            return {"collection": [], "pagination": {"next_page_token": None}}

        def list_event_invitees(self, event_uri, **kwargs):
            return {"collection": [{"uri": "inv1", "email": "a@acme.com", "name": "A", "status": status_for_ev1}]}

        def close(self):
            pass
    return FakeClient


def _bare_cfg():
    from outreach.config import Config
    return Config(raw={"research": {"api_base": "http://x"}, "selection": {}, "sending": {}, "personalization": {}, "stages": {}}, path=None)


@pytest.mark.parametrize("status", ["active", "canceled"])
def test_deleted_booking_is_not_recreated_by_sync(conn, monkeypatch, status):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    calendly_sync.apply_event(conn, _event(status="canceled", starts_at=PAST_DT), calendly_sync.SyncResult())
    calendly_sync.delete_booking(conn, _cfg(), EV1)
    assert calendly_db.get_event(conn, EV1) is None

    monkeypatch.setattr(calendly_sync, "CalendlyClient", _sync_client(status))
    result = calendly_sync.run_sync(conn, _bare_cfg())
    assert result.ignored == 1 and result.created == 0
    assert calendly_db.get_event(conn, EV1) is None
    # and again -- still gone
    calendly_sync.run_sync(conn, _bare_cfg(), skip_unchanged=True)
    assert calendly_db.get_event(conn, EV1) is None


def test_deleted_booking_is_not_recreated_by_webhook(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    calendly_sync.apply_event(conn, _event(status="canceled", starts_at=PAST_DT), calendly_sync.SyncResult())
    calendly_sync.delete_booking(conn, _cfg(), EV1)
    payload = {
        "event": "invitee.canceled",
        "payload": {"uri": "inv1", "email": "a@acme.com", "name": "A",
                    "scheduled_event": {"uri": EV1, "start_time": "2026-04-01T15:00:00Z", "end_time": "2026-04-01T15:30:00Z"}},
    }
    assert calendly_sync.ingest_webhook_event(conn, payload) is None
    assert calendly_db.get_event(conn, EV1) is None


def test_deleting_one_booking_does_not_block_others(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    calendly_sync.apply_event(conn, _event(status="canceled", starts_at=PAST_DT), calendly_sync.SyncResult())
    calendly_sync.delete_booking(conn, _cfg(), EV1)
    other = "https://api.calendly.com/scheduled_events/EV2"
    calendly_sync.apply_event(conn, _event(event_uri=other), calendly_sync.SyncResult())
    assert calendly_db.get_event(conn, other) is not None


def test_delete_booking_moves_booked_item_off_booked(conn, monkeypatch):
    monkeypatch.setattr(calendly_sync, "CalendlyClient", _FakeCancelClient)
    _seed_item(conn, item_id="item-1", domain="acme.com", to_email="prospect@acme.com", stage="waiting")
    calendly_sync.apply_event(conn, _event(starts_at=PAST_DT), calendly_sync.SyncResult())
    assert db.get_item(conn, "item-1").stage == "booked"
    calendly_sync.delete_booking(conn, _cfg(), EV1)
    assert db.get_item(conn, "item-1").stage == "waiting"
