"""send_campaign must only ever move items pending->sent or pending->failed,
never touch waiting/replied/booked, and must be idempotent (re-running only
touches items still pending)."""

from __future__ import annotations

from outreach import db, pipeline
from outreach.config import load_config
from outreach.models import Campaign, ImportedProspect, OutreachItem


def _setup_campaign(conn, *, dry_run: bool = True):
    cfg = load_config()
    cfg.raw["sending"]["dry_run"] = dry_run
    db.create_campaign(conn, Campaign(campaign_id="c1", channel="email"))
    return cfg


def test_send_campaign_marks_items_sent_in_dry_run(conn):
    cfg = _setup_campaign(conn, dry_run=True)
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme", contact_email="jane@acme.com", contact_name="Jane",
    ))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))

    stats = pipeline.send_campaign(conn, cfg, "c1")

    assert stats.sent == 1
    assert stats.failed == 0
    item = db.get_item(conn, "i1")
    assert item.stage == "sent"
    assert item.provider_message_id is not None
    assert item.subject and item.body


def test_send_campaign_skips_prospect_without_cached_contact(conn):
    cfg = _setup_campaign(conn, dry_run=True)
    # no matching prospect cached at all
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="ghost.com"))

    stats = pipeline.send_campaign(conn, cfg, "c1")

    assert stats.skipped == 1
    assert stats.sent == 0
    item = db.get_item(conn, "i1")
    assert item.stage == "failed"


def test_send_campaign_only_touches_pending_items(conn):
    cfg = _setup_campaign(conn, dry_run=True)
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", contact_email="jane@acme.com"))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))
    db.set_stage(conn, "i1", "waiting", actor="user")  # already past sent

    stats = pipeline.send_campaign(conn, cfg, "c1")

    assert stats.total == 0  # nothing pending
    item = db.get_item(conn, "i1")
    assert item.stage == "waiting"  # untouched


def test_send_campaign_never_sets_waiting_replied_or_booked(conn):
    cfg = _setup_campaign(conn, dry_run=True)
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", contact_email="jane@acme.com"))
    db.create_item(conn, OutreachItem(item_id="i1", campaign_id="c1", research_domain="acme.com"))

    pipeline.send_campaign(conn, cfg, "c1")

    item = db.get_item(conn, "i1")
    assert item.stage in ("sent", "failed")
