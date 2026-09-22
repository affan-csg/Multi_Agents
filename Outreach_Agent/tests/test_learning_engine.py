"""Tests for learning/engine.py -- confidence scoring and insight
recomputation against seeded draft_versions + outcome data."""

from __future__ import annotations

from outreach.learning import db as learning_db
from outreach.learning.engine import (
    HIGH_CONFIDENCE_MIN,
    MEDIUM_CONFIDENCE_MIN,
    MIN_EVIDENCE_FOR_ANY_INSIGHT,
    compute_confidence,
    recompute_insights,
)
from outreach.learning.models import DraftVersion


# --- compute_confidence -------------------------------------------------------

def test_confidence_none_below_min_evidence():
    assert compute_confidence(5, 3, 2, 10.0) is None


def test_confidence_none_when_group_too_small():
    # total clears the threshold but one group is lopsided/tiny
    assert compute_confidence(30, 27, 3, 10.0) is None


def test_confidence_none_when_effect_too_small():
    assert compute_confidence(100, 50, 50, 0.5) is None


def test_confidence_low_at_min_threshold():
    assert compute_confidence(MIN_EVIDENCE_FOR_ANY_INSIGHT, 10, 10, 5.0) == "low"


def test_confidence_medium_at_threshold():
    assert compute_confidence(MEDIUM_CONFIDENCE_MIN, 25, 25, 5.0) == "medium"


def test_confidence_high_at_threshold():
    assert compute_confidence(HIGH_CONFIDENCE_MIN, 75, 75, 5.0) == "high"


def test_confidence_negative_effect_also_counts():
    assert compute_confidence(MIN_EVIDENCE_FOR_ANY_INSIGHT, 10, 10, -5.0) == "low"


# --- recompute_insights --------------------------------------------------------

def _seed_campaign_and_items(conn, n: int, *, stage="replied", campaign_id="camp1"):
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": "C"})
    for i in range(n):
        conn.tables.setdefault("outreach_items", []).append({
            "item_id": f"item-{campaign_id}-{i}", "campaign_id": campaign_id, "research_domain": f"c{i}.com",
            "stage": stage, "channel": "email", "subject": "s", "body": "b",
            "personalized_from": "pain_summary", "sent_at": "2026-01-01T00:00:00",
            "created_at": "2026-01-01T00:00:00", "stage_updated_at": "2026-01-01T00:00:00",
            "followup_count": 0,
        })


def _mark_replied(conn, item_id, *, positive=True):
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": f"m-{item_id}", "item_id": item_id, "direction": "inbound",
        "intent": "interested" if positive else "not_interested",
        "created_at": "2026-01-02T00:00:00",
    })


def _seed_draft_version(conn, item_id, campaign_id, **overrides):
    fields = dict(
        version_id=f"v-{item_id}", item_id=item_id, campaign_id=campaign_id, source="revise",
        before_subject="a", before_body="b", after_subject="c", after_body="d",
    )
    fields.update(overrides)
    learning_db.insert_draft_version(conn, DraftVersion(**fields))


def test_detects_clear_pattern_with_sufficient_evidence(conn):
    """60 short-CTA emails with a high reply rate vs 60 non-shortened with a
    low reply rate -- a real, large gap at n=120 should produce an active,
    medium-or-higher confidence insight."""
    _seed_campaign_and_items(conn, 120, stage="sent")
    items = conn.tables["outreach_items"]
    for i, item in enumerate(items):
        shortened = i < 60
        _seed_draft_version(conn, item["item_id"], "camp1", cta_changed=True, cta_shortened=shortened)
    # Deterministic, exact rates: every 2nd short-CTA item replies (30/60=50%),
    # every 20th non-shortened item replies (3/60=5%).
    for i, item in enumerate(items):
        shortened = i < 60
        if shortened and i % 2 == 0:
            _mark_replied(conn, item["item_id"])
        elif not shortened and i % 20 == 0:
            _mark_replied(conn, item["item_id"])

    insights = recompute_insights(conn)
    shorter_cta = next((i for i in insights if i.pattern_key == "shorter_cta"), None)
    assert shorter_cta is not None
    assert shorter_cta.status == "active"
    assert shorter_cta.confidence in ("medium", "high")
    assert shorter_cta.evidence_count == 120


def test_small_sample_does_not_trigger_false_insight(conn):
    """Same shape but n=6 -- even with a large percentage swing, must stay
    insufficient_data and never appear as 'active'."""
    _seed_campaign_and_items(conn, 6, stage="sent")
    items = conn.tables["outreach_items"]
    for i, item in enumerate(items):
        shortened = i < 3
        _seed_draft_version(conn, item["item_id"], "camp1", cta_changed=True, cta_shortened=shortened)
        if shortened:
            _mark_replied(conn, item["item_id"])

    insights = recompute_insights(conn)
    active_keys = {i.pattern_key for i in insights if i.status == "active"}
    assert "shorter_cta" not in active_keys


def test_recompute_is_idempotent(conn):
    _seed_campaign_and_items(conn, 120, stage="sent")
    items = conn.tables["outreach_items"]
    for i, item in enumerate(items):
        shortened = i < 60
        _seed_draft_version(conn, item["item_id"], "camp1", cta_changed=True, cta_shortened=shortened)
        if shortened and i % 2 == 0:
            _mark_replied(conn, item["item_id"])
        elif not shortened and i % 20 == 0:
            _mark_replied(conn, item["item_id"])

    recompute_insights(conn)
    first = learning_db.get_insight(conn, "shorter_cta")
    recompute_insights(conn)
    second = learning_db.get_insight(conn, "shorter_cta")

    assert first.evidence_count == second.evidence_count
    assert first.confidence == second.confidence
    # no duplicate learning_insights rows
    assert len([r for r in conn.tables.get("learning_insights", []) if r["pattern_key"] == "shorter_cta"]) == 1


def test_confidence_increase_appends_exactly_one_event(conn):
    _seed_campaign_and_items(conn, 40, stage="sent", campaign_id="camp1")
    items = conn.tables["outreach_items"]
    for i, item in enumerate(items):
        shortened = i < 20
        _seed_draft_version(conn, item["item_id"], "camp1", cta_changed=True, cta_shortened=shortened)
        if shortened and i % 2 == 0:
            _mark_replied(conn, item["item_id"])
        elif not shortened and i % 20 == 0:
            _mark_replied(conn, item["item_id"])

    recompute_insights(conn)
    before_events = len(conn.tables.get("learning_events", []))

    # Add more evidence, same effect, pushing total evidence into the
    # 'medium' tier -- seed a second batch of items/versions.
    _seed_campaign_and_items(conn, 40, stage="sent", campaign_id="camp2")
    items2 = conn.tables["outreach_items"][-40:]
    for i, item in enumerate(items2):
        shortened = i < 20
        _seed_draft_version(conn, item["item_id"], "camp2", cta_changed=True, cta_shortened=shortened)
        if shortened and i % 2 == 0:
            _mark_replied(conn, item["item_id"])
        elif not shortened and i % 20 == 0:
            _mark_replied(conn, item["item_id"])

    recompute_insights(conn)
    after_events = len(conn.tables.get("learning_events", []))
    new_events = [e for e in conn.tables["learning_events"][before_events:] if e["pattern_key"] == "shorter_cta"]

    assert after_events > before_events
    assert len(new_events) == 1
    assert new_events[0]["event_type"] == "confidence_increased"


def test_human_edit_pattern_frequency(conn):
    """30 human edits, ~72% shorten the CTA -- should surface
    human_edit_shorten_cta with a matching percentage."""
    _seed_campaign_and_items(conn, 30, stage="sent")
    items = conn.tables["outreach_items"]
    for i, item in enumerate(items):
        # 22/30 = ~73% shortened
        shortened = i < 22
        _seed_draft_version(conn, item["item_id"], "camp1", source="revise", cta_shortened=shortened)

    insights = recompute_insights(conn)
    pattern = next((i for i in insights if i.pattern_key == "human_edit_shorten_cta"), None)
    assert pattern is not None
    assert pattern.status == "active"
    assert pattern.metric_comparison["pct"] > 70


def test_empty_draft_versions_returns_no_active_insights(conn):
    insights = recompute_insights(conn)
    assert all(i.status != "active" for i in insights) or insights == []
