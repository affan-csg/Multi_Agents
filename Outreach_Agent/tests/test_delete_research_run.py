"""Tests for db.delete_by_research_run -- cascade-cleanup triggered when the
Research Agent deletes a run (see api.delete_research_run, called by the
Research Agent's own /runs/{run_id} DELETE handler)."""

from __future__ import annotations

from outreach import db
from outreach.models import Campaign, ImportedProspect, OutreachEvent, OutreachItem


def _seed_prospect(conn, domain: str, run_id: str) -> None:
    db.upsert_prospect(conn, ImportedProspect(research_domain=domain, research_run_id=run_id))


def _seed_campaign_with_item(conn, campaign_id: str, run_id: str, domain: str) -> str:
    db.create_campaign(conn, Campaign(campaign_id=campaign_id, name="Test", research_run_id=run_id))
    item = OutreachItem(item_id=f"item-{domain}", campaign_id=campaign_id, research_domain=domain)
    db.create_item(conn, item)
    conn.insert("outreach_events", [OutreachEvent(
        event_id=f"evt-{domain}", item_id=item.item_id, event_type="created",
    ).model_dump(mode="json")])
    return item.item_id


def test_delete_by_research_run_removes_campaigns_items_events_and_prospects(conn):
    _seed_prospect(conn, "acme.com", "run1")
    _seed_prospect(conn, "beta.com", "run1")
    item_id = _seed_campaign_with_item(conn, "camp1", "run1", "acme.com")

    result = db.delete_by_research_run(conn, "run1")

    assert result == {"campaigns_removed": 1, "items_removed": 1, "prospects_removed": 2}
    assert db.get_campaign(conn, "camp1") is None
    assert db.get_item(conn, item_id) is None
    assert conn.select("outreach_events", item_id=f"eq.{item_id}") == []
    assert db.get_prospect(conn, "acme.com") is None
    assert db.get_prospect(conn, "beta.com") is None


def test_delete_by_research_run_leaves_other_runs_untouched(conn):
    _seed_prospect(conn, "acme.com", "run1")
    _seed_prospect(conn, "other.com", "run2")
    _seed_campaign_with_item(conn, "camp1", "run1", "acme.com")
    other_item_id = _seed_campaign_with_item(conn, "camp2", "run2", "other.com")

    db.delete_by_research_run(conn, "run1")

    assert db.get_campaign(conn, "camp2") is not None
    assert db.get_item(conn, other_item_id) is not None
    assert db.get_prospect(conn, "other.com") is not None


def test_delete_by_research_run_is_idempotent_on_unknown_run(conn):
    """The Research Agent calls this unconditionally on every run delete, even
    when that run never had a successful Outreach hand-off -- must not error."""
    result = db.delete_by_research_run(conn, "never-imported")

    assert result == {"campaigns_removed": 0, "items_removed": 0, "prospects_removed": 0}
