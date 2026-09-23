"""Tests for recommendation-adoption outcome tracking -- verifies the
before/after-style comparison reports observed rates without ever asserting
causation, and correctly reports insufficient_data when there isn't enough
evidence on either side of the comparison."""

from __future__ import annotations

import uuid

from outreach.optimization import db as opt_db
from outreach.optimization import tracking
from outreach.optimization.models import RecommendationAdoption
from test_optimization_fixtures import build_cta_style_dataset, seed_campaign


def test_insufficient_data_when_no_adoptions_recorded(conn):
    result = tracking.adoption_outcome(conn, "some_pattern")
    assert result["insufficient_data"] is True


def test_insufficient_data_when_adopting_campaign_too_small(conn):
    seed_campaign(conn, "small-adopter")
    opt_db.record_adoption(conn, RecommendationAdoption(
        adoption_id=str(uuid.uuid4()), pattern_key="cta_style:direct-vs-soft", campaign_id="small-adopter",
    ))
    # No sent items at all under "small-adopter" -- must not fabricate a comparison.
    build_cta_style_dataset(conn, campaign_id="other-camp", n_direct=40, n_soft=40)
    result = tracking.adoption_outcome(conn, "cta_style:direct-vs-soft")
    assert result["insufficient_data"] is True


def test_reports_observed_rates_with_causality_disclaimer_when_enough_data(conn):
    build_cta_style_dataset(conn, campaign_id="adopting-camp", n_direct=15, n_soft=15)
    build_cta_style_dataset(conn, campaign_id="baseline-camp", n_direct=15, n_soft=15)
    opt_db.record_adoption(conn, RecommendationAdoption(
        adoption_id=str(uuid.uuid4()), pattern_key="cta_style:direct-vs-soft", campaign_id="adopting-camp",
    ))

    result = tracking.adoption_outcome(conn, "cta_style:direct-vs-soft")

    assert result["insufficient_data"] is False
    assert result["adopting"]["n"] == 30
    assert result["comparison"]["n"] == 30
    assert "not proof" in result["causality_note"] or "not a controlled experiment" in result["causality_note"]
    assert "reply_rate" in result["adopting"]
    assert "booking_rate" in result["adopting"]
