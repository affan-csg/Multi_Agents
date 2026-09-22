"""Tests for the /learning/* dashboard API surface (learning/routes.py)
against seeded FakeConn data -- response shape assertions, matching how
other *_api.py-style route tests in this suite call route functions
directly rather than spinning up a live server."""

from __future__ import annotations

from outreach import db
from outreach.learning import db as learning_db
from outreach.learning import routes as learning_routes
from outreach.learning.models import DraftVersion


def _seed_campaign_and_item(conn, campaign_id="camp1", item_id="item1", *, stage="sent"):
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": "C"})
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": item_id, "campaign_id": campaign_id, "research_domain": "acme.com",
        "stage": stage, "channel": "email", "subject": "s", "body": "b",
        "personalized_from": "pain_summary", "sent_at": "2026-01-01T00:00:00",
        "created_at": "2026-01-01T00:00:00", "stage_updated_at": "2026-01-01T00:00:00",
        "followup_count": 0,
    })


def _seed_draft_version(conn, version_id="v1", item_id="item1", campaign_id="camp1", **overrides):
    fields = dict(
        version_id=version_id, item_id=item_id, campaign_id=campaign_id, source="revise",
        before_subject="a", before_body="b", after_subject="c", after_body="d",
    )
    fields.update(overrides)
    learning_db.insert_draft_version(conn, DraftVersion(**fields))


def test_overview_empty_state(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    result = learning_routes.get_overview()
    assert result["total_draft_versions"] == 0
    assert result["active_insight_count"] == 0
    assert result["confidence_counts"] == {"low": 0, "medium": 0, "high": 0}


def test_overview_counts_drafts_and_edits(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    _seed_campaign_and_item(conn)
    _seed_draft_version(conn, "v1", source="revise")
    _seed_draft_version(conn, "v2", source="redraft")
    result = learning_routes.get_overview()
    assert result["total_draft_versions"] == 2
    assert result["drafts_analyzed"] == 2
    assert result["human_edits_analyzed"] == 2


def test_list_insights_route_empty(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    result = learning_routes.list_insights_route()
    assert result == []


def test_get_insight_detail_404(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    from fastapi import HTTPException
    try:
        learning_routes.get_insight_detail("does-not-exist")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_get_history_empty(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    assert learning_routes.get_history() == []


def test_list_human_edits_returns_versions(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    _seed_campaign_and_item(conn)
    _seed_draft_version(conn, "v1")
    result = learning_routes.list_human_edits()
    assert len(result) == 1
    assert result[0]["version_id"] == "v1"


def test_list_human_edits_filters_by_campaign(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    _seed_campaign_and_item(conn, "camp1", "item1")
    _seed_campaign_and_item(conn, "camp2", "item2")
    _seed_draft_version(conn, "v1", "item1", "camp1")
    _seed_draft_version(conn, "v2", "item2", "camp2")
    result = learning_routes.list_human_edits(campaign_id="camp1")
    assert len(result) == 1
    assert result[0]["campaign_id"] == "camp1"


def test_get_human_edit_detail_404(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    from fastapi import HTTPException
    try:
        learning_routes.get_human_edit_detail("nope")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_get_human_edit_detail_found(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    _seed_campaign_and_item(conn)
    _seed_draft_version(conn, "v1")
    result = learning_routes.get_human_edit_detail("v1")
    assert result["version_id"] == "v1"


def test_before_after_route(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    result = learning_routes.get_before_after(metric="reply_rate", window=30)
    assert result["insufficient_data"] is True


def test_before_after_route_bad_metric_returns_400(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    from fastapi import HTTPException
    try:
        learning_routes.get_before_after(metric="bogus", window=30)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400


def test_campaign_comparison_route(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    assert learning_routes.get_campaign_comparison() == []


def test_followup_performance_route(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    result = learning_routes.get_followup_performance()
    assert result["insufficient_data"] is True


def test_recommendations_route_empty(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    assert learning_routes.get_recommendations() == []
