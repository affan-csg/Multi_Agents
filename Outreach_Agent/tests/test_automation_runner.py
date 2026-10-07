"""The polling cycle: lease/overlap, persisted timers, failure handling and
backoff, shared inbox fetch, interval. Mail, Calendly and the clock are fake."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach import db
from outreach.automation import runner
from outreach.config import load_config
from outreach.conversation import engine, inbox
from outreach.conversation.settings import set_settings
from outreach.models import Campaign, ImportedProspect, OutreachItem

T0 = datetime(2026, 5, 1, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def imap(monkeypatch):
    """Configured IMAP whose fetch is recorded and scriptable."""
    calls: list[dict] = []
    monkeypatch.setattr(inbox, "imap_configured", lambda: True)

    def fetch(**kw):
        calls.append(kw)
        if imap.error:
            raise inbox.InboxError(imap.error)
        return []

    imap.error = None
    imap.calls = calls
    monkeypatch.setattr(inbox, "fetch_replies", fetch)
    return imap


def _campaign(conn, cid, enabled=True):
    db.create_campaign(conn, Campaign(campaign_id=cid, name=cid, created_at=T0))
    set_settings(conn, cid, enabled=enabled)


def test_first_cycle_creates_state_and_schedules_next_run(conn, cfg, imap):
    _campaign(conn, "c1")
    r = runner.run_cycle(conn, cfg, now=T0)
    s = db.get_automation_state(conn)
    assert r.status == "ok"
    assert s.interval_minutes == 30
    assert s.last_success_at is not None
    assert s.next_run_at == T0 + timedelta(minutes=30)
    assert s.last_inbox_success_at == T0
    assert s.locked_until is None and s.consecutive_failures == 0


def test_overlapping_cycle_is_skipped_while_lease_is_held(conn, cfg, imap):
    _campaign(conn, "c1")
    runner.ensure_state(conn)
    assert db.claim_automation_lease(conn, "sync", "other", now=T0, until=T0 + timedelta(minutes=20))

    r = runner.run_cycle(conn, cfg, now=T0 + timedelta(minutes=1))
    assert r.status == "skipped_locked"
    assert imap.calls == []
    # The other holder's lease is untouched.
    assert db.get_automation_state(conn).locked_by == "other"


def test_stale_lease_is_reclaimed(conn, cfg, imap):
    _campaign(conn, "c1")
    runner.ensure_state(conn)
    db.claim_automation_lease(conn, "sync", "crashed", now=T0, until=T0 + timedelta(minutes=20))
    r = runner.run_cycle(conn, cfg, now=T0 + timedelta(minutes=21))
    assert r.status == "ok"


def test_schedule_survives_a_restart(conn, cfg, imap):
    """is_due is derived purely from the persisted next_run_at: a new process
    neither fires early nor resets the countdown."""
    _campaign(conn, "c1")
    runner.run_cycle(conn, cfg, now=T0)

    state = runner.ensure_state(conn)  # a "restarted" process reading the DB
    assert not runner.is_due(state, T0 + timedelta(minutes=29))
    assert runner.is_due(state, T0 + timedelta(minutes=30))
    assert state.next_run_at == T0 + timedelta(minutes=30)


def test_inbox_is_fetched_once_for_all_campaigns_and_incrementally(conn, cfg, imap, monkeypatch):
    for cid in ("c1", "c2", "c3"):
        _campaign(conn, cid)
    _campaign(conn, "off", enabled=False)
    ticked = []
    real = engine.tick
    monkeypatch.setattr(engine, "tick", lambda conn_, cfg_, cid, **kw: ticked.append((cid, kw["emails"])) or real(conn_, cfg_, cid, **kw))

    runner.run_cycle(conn, cfg, now=T0)
    assert len(imap.calls) == 1 and imap.calls[0]["since"] is None
    assert sorted(c for c, _ in ticked) == ["c1", "c2", "c3"]  # the OFF campaign is never ticked

    runner.run_cycle(conn, cfg, now=T0 + timedelta(minutes=30))
    # Second fetch starts one day before the last successful read.
    assert imap.calls[1]["since"] == T0 - timedelta(days=1)


def test_no_enabled_campaign_means_no_inbox_access(conn, cfg, imap):
    _campaign(conn, "off", enabled=False)
    runner.run_cycle(conn, cfg, now=T0)
    assert imap.calls == []


def test_inbox_failure_is_recorded_backs_off_and_blocks_followups(conn, cfg, imap, monkeypatch):
    _campaign(conn, "c1")
    imap.error = "login failed"
    seen = {}
    real = engine.tick
    monkeypatch.setattr(engine, "tick", lambda conn_, cfg_, cid, **kw: seen.update(kw) or real(conn_, cfg_, cid, **kw))

    r = runner.run_cycle(conn, cfg, now=T0)
    s = db.get_automation_state(conn)
    assert r.status in ("partial", "failed")
    assert seen["inbox_ok"] is False
    assert "login failed" in s.last_error
    assert s.consecutive_failures == 1
    assert s.last_success_at is None and s.last_inbox_success_at is None
    assert s.next_run_at == T0 + timedelta(minutes=5)  # retry sooner than a full interval

    runner.run_cycle(conn, cfg, now=T0 + timedelta(minutes=5))
    s = db.get_automation_state(conn)
    assert s.consecutive_failures == 2
    assert s.next_run_at == T0 + timedelta(minutes=5) + timedelta(minutes=10)

    imap.error = None
    runner.run_cycle(conn, cfg, now=T0 + timedelta(minutes=15))
    s = db.get_automation_state(conn)
    assert s.consecutive_failures == 0 and s.last_error is None


def test_backoff_never_exceeds_the_interval(conn, cfg, imap):
    _campaign(conn, "c1")
    imap.error = "down"
    now = T0
    for _ in range(8):
        runner.run_cycle(conn, cfg, now=now)
        s = db.get_automation_state(conn)
        assert s.next_run_at - now <= timedelta(minutes=30)
        now = s.next_run_at


def test_interval_setting_is_honored(conn, cfg, imap):
    _campaign(conn, "c1")
    runner.ensure_state(conn)
    db.update_automation_state(conn, "sync", interval_minutes=10)
    runner.run_cycle(conn, cfg, now=T0)
    assert db.get_automation_state(conn).next_run_at == T0 + timedelta(minutes=10)


def test_env_seeds_the_interval_only_on_first_creation(conn, monkeypatch):
    monkeypatch.setenv("AUTO_SYNC_INTERVAL_MINUTES", "45")
    assert runner.ensure_state(conn).interval_minutes == 45
    db.update_automation_state(conn, "sync", interval_minutes=20)
    monkeypatch.setenv("AUTO_SYNC_INTERVAL_MINUTES", "99")
    assert runner.ensure_state(conn).interval_minutes == 20  # DB wins after that


@pytest.mark.parametrize("raw,expected", [("2", 5), ("30", 30), ("junk", 30), ("999999", 1440)])
def test_env_interval_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("AUTO_SYNC_INTERVAL_MINUTES", raw)
    assert runner.env_interval_minutes() == expected


def test_scheduler_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_SYNC_ENABLED", raising=False)
    assert runner.scheduler_enabled() is False
    monkeypatch.setenv("AUTO_SYNC_ENABLED", "true")
    assert runner.scheduler_enabled() is True


# --- Calendly inside the cycle ---------------------------------------------------------------

class _FakeSync:
    def __init__(self, errors=()):
        self.errors = list(errors)

    def as_dict(self):
        return {"errors": self.errors}


def test_calendly_failure_is_recorded_and_followups_are_gated(conn, cfg, imap, monkeypatch):
    from outreach.calendly import sync as calendly_sync

    _campaign(conn, "c1")
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")
    monkeypatch.setattr(calendly_sync, "run_sync", lambda *a, **kw: _FakeSync(["429 after retries"]))
    seen = {}
    real = engine.tick
    monkeypatch.setattr(engine, "tick", lambda conn_, cfg_, cid, **kw: seen.update(kw) or real(conn_, cfg_, cid, **kw))

    r = runner.run_cycle(conn, cfg, now=T0)
    s = db.get_automation_state(conn)
    assert seen["calendly_ok"] is False and seen["inbox_ok"] is True
    assert s.last_calendly_success_at is None
    assert s.last_inbox_success_at == T0  # the inbox half still succeeded
    assert "429" in s.last_error
    assert r.status == "partial"


def test_calendly_crash_does_not_stop_the_cycle(conn, cfg, imap, monkeypatch):
    from outreach.calendly import sync as calendly_sync

    _campaign(conn, "c1")
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")
    monkeypatch.setattr(calendly_sync, "run_sync", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    runner.run_cycle(conn, cfg, now=T0)
    assert len(imap.calls) == 1
    assert db.get_automation_state(conn).locked_until is None  # lease always released


def test_calendly_sync_gets_incremental_args_and_agent_gated_stage_moves(conn, cfg, imap, monkeypatch):
    from outreach.calendly import sync as calendly_sync

    _campaign(conn, "on", enabled=True)
    _campaign(conn, "off", enabled=False)
    monkeypatch.setenv("CALENDLY_API_TOKEN", "tok")
    captured = {}

    def fake_run(conn_, cfg_, **kw):
        captured.update(kw)
        return _FakeSync()

    monkeypatch.setattr(calendly_sync, "run_sync", fake_run)
    runner.run_cycle(conn, cfg, now=T0)

    assert captured["lookback_days"] == 30 and captured["skip_unchanged"] is True
    mk = lambda cid: OutreachItem(item_id="i", campaign_id=cid, research_domain="d", stage="sent", created_at=T0)
    assert captured["move_stages"](mk("on")) is True
    assert captured["move_stages"](mk("off")) is False
    assert db.get_automation_state(conn).last_calendly_success_at == T0
