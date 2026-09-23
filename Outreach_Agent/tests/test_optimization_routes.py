"""API-route-level tests for /optimization/* -- overview, insights,
recommendations, and the adoption record/read/outcome endpoints."""

from __future__ import annotations

import pytest

from outreach.optimization import routes as opt_routes
from test_optimization_fixtures import build_cta_style_dataset, seed_campaign


@pytest.fixture(autouse=True)
def _patch_connect(conn, monkeypatch):
    monkeypatch.setattr(opt_routes.db, "connect", lambda: conn)
    return conn


def test_overview_reports_no_active_insights_when_no_data(conn):
    overview = opt_routes.get_overview()
    assert overview["active_insight_count"] == 0


def test_overview_counts_active_insights_after_sufficient_data(conn):
    build_cta_style_dataset(conn, n_direct=40, n_soft=40)
    overview = opt_routes.get_overview()
    assert overview["active_insight_count"] >= 1
    assert overview["confidence_counts"]["medium"] + overview["confidence_counts"]["high"] >= 1


def test_recommendations_empty_when_insufficient_data(conn):
    """Never a manufactured recommendation -- see __init__.py invariant 3."""
    build_cta_style_dataset(conn, campaign_id="tiny", n_direct=2, n_soft=2)
    recs = opt_routes.get_recommendations()
    assert recs == []


def test_recommendations_include_evidence_and_causality_note(conn):
    build_cta_style_dataset(conn, n_direct=40, n_soft=40)
    recs = opt_routes.get_recommendations()
    assert len(recs) >= 1
    rec = recs[0]
    assert "evidence_count" in rec
    assert "confidence" in rec
    assert "metric_comparison" in rec
    assert "evidence_campaign_ids" in rec
    assert "causality_note" in rec
    assert "not a proven cause" in rec["causality_note"]


def test_insight_detail_404_for_unknown_pattern(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        opt_routes.get_insight_detail("nonexistent_pattern")


def test_record_and_list_adoption(conn):
    seed_campaign(conn, "camp-adopt")
    result = opt_routes.create_adoption(opt_routes.AdoptionRequest(
        pattern_key="cta_style:direct-vs-soft", campaign_id="camp-adopt", note="trying direct CTAs",
    ))
    assert result["pattern_key"] == "cta_style:direct-vs-soft"

    adoptions = opt_routes.list_adoptions_route(campaign_id="camp-adopt")
    assert len(adoptions) == 1
    assert adoptions[0]["note"] == "trying direct CTAs"


def test_adoption_for_unknown_campaign_404s(conn):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        opt_routes.create_adoption(opt_routes.AdoptionRequest(
            pattern_key="cta_style:direct-vs-soft", campaign_id="does-not-exist",
        ))


def test_adoption_outcome_insufficient_data_when_no_adoptions(conn):
    outcome = opt_routes.get_adoption_outcome("never_adopted_pattern")
    assert outcome["insufficient_data"] is True
