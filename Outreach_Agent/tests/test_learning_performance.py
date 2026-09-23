"""Tests for learning/performance.py -- windowed before/after comparisons,
campaign comparisons, and followup-bucket performance."""

from __future__ import annotations

import pytest

from outreach.learning import performance


def _seed_campaign(conn, campaign_id="camp1", name="Campaign 1"):
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": name})


def _seed_item(conn, item_id, campaign_id, sent_at, *, followup_count=0):
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": item_id, "campaign_id": campaign_id, "research_domain": f"{item_id}.com",
        "stage": "sent", "channel": "email", "subject": "s", "body": "b",
        "personalized_from": "pain_summary", "sent_at": sent_at,
        "created_at": sent_at, "stage_updated_at": sent_at, "followup_count": followup_count,
    })


def _mark_replied(conn, item_id, *, positive=True, booked=False):
    if booked:
        for row in conn.tables["outreach_items"]:
            if row["item_id"] == item_id:
                row["stage"] = "booked"
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": f"m-{item_id}", "item_id": item_id, "direction": "inbound",
        "intent": "interested" if positive else "not_interested",
        "created_at": "2026-01-03T00:00:00",
    })


# --- before_after_comparison ---------------------------------------------------

def test_before_after_insufficient_data_when_too_few_items(conn):
    _seed_campaign(conn)
    for i in range(10):
        _seed_item(conn, f"item-{i}", "camp1", f"2026-01-{i+1:02d}T00:00:00")

    result = performance.before_after_comparison(conn, metric="reply_rate", window=30)
    assert result["insufficient_data"] is True
    assert result["before"] is None
    assert result["after"] is None


def test_before_after_computes_rates_with_enough_data(conn):
    _seed_campaign(conn)
    # 60 items, sequential sent_at. First 30 = "before", last 30 = "after".
    for i in range(60):
        _seed_item(conn, f"item-{i}", "camp1", f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00")
    # Before group (first 30): 3 replies (10%). After group (last 30): 9 replies (30%).
    for i in range(3):
        _mark_replied(conn, f"item-{i}")
    for i in range(30, 39):
        _mark_replied(conn, f"item-{i}")

    result = performance.before_after_comparison(conn, metric="reply_rate", window=30)
    assert result["insufficient_data"] is False
    assert result["before"]["n"] == 30
    assert result["after"]["n"] == 30
    assert result["before"]["value"] == 10.0
    assert result["after"]["value"] == 30.0
    assert result["change_pct"] == 200.0


def test_before_after_unknown_metric_raises(conn):
    with pytest.raises(ValueError):
        performance.before_after_comparison(conn, metric="not_a_real_metric", window=5)


def test_before_after_no_reply_rate_metric(conn):
    _seed_campaign(conn)
    for i in range(60):
        _seed_item(conn, f"item-{i}", "camp1", f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00")
    # No replies at all -- no_reply_rate should be 100% both windows.
    result = performance.before_after_comparison(conn, metric="no_reply_rate", window=30)
    assert result["before"]["value"] == 100.0
    assert result["after"]["value"] == 100.0


def _mark_not_interested(conn, item_id):
    """A clear rejection: moves the item to the terminal not_interested
    stage AND records the rejection intent -- unlike _mark_replied's
    positive=False case, which only sets intent and leaves stage untouched
    (not a real rejection outcome as far as outcome_summary's `failed` flag
    is concerned)."""
    for row in conn.tables["outreach_items"]:
        if row["item_id"] == item_id:
            row["stage"] = "not_interested"
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": f"m-{item_id}", "item_id": item_id, "direction": "inbound",
        "intent": "not_interested", "created_at": "2026-01-03T00:00:00",
    })


def test_before_after_failure_rate_metric(conn):
    _seed_campaign(conn)
    for i in range(60):
        _seed_item(conn, f"item-{i}", "camp1", f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}T00:00:00")
    # Before group (first 30): 3 rejections (10%). After group (last 30): none.
    for i in range(3):
        _mark_not_interested(conn, f"item-{i}")

    result = performance.before_after_comparison(conn, metric="failure_rate", window=30)
    assert result["insufficient_data"] is False
    assert result["before"]["value"] == 10.0
    assert result["after"]["value"] == 0.0


# --- campaign_comparison --------------------------------------------------------

def test_campaign_comparison_computes_per_campaign_rates(conn):
    _seed_campaign(conn, "camp1", "Campaign One")
    _seed_campaign(conn, "camp2", "Campaign Two")
    for i in range(10):
        _seed_item(conn, f"c1-{i}", "camp1", "2026-01-01T00:00:00")
    for i in range(10):
        _seed_item(conn, f"c2-{i}", "camp2", "2026-01-01T00:00:00")
    for i in range(5):
        _mark_replied(conn, f"c1-{i}")
    for i in range(2):
        _mark_replied(conn, f"c2-{i}")

    results = performance.campaign_comparison(conn)
    by_id = {r["campaign_id"]: r for r in results}
    assert by_id["camp1"]["reply_rate"] == 50.0
    assert by_id["camp2"]["reply_rate"] == 20.0


def test_campaign_comparison_filters_by_campaign_ids(conn):
    _seed_campaign(conn, "camp1")
    _seed_campaign(conn, "camp2")
    _seed_item(conn, "i1", "camp1", "2026-01-01T00:00:00")
    _seed_item(conn, "i2", "camp2", "2026-01-01T00:00:00")

    results = performance.campaign_comparison(conn, campaign_ids=["camp1"])
    assert {r["campaign_id"] for r in results} == {"camp1"}


def test_campaign_comparison_skips_campaigns_with_no_sent_items(conn):
    _seed_campaign(conn, "camp1")
    results = performance.campaign_comparison(conn)
    assert results == []


def test_campaign_comparison_includes_failure_rate(conn):
    _seed_campaign(conn, "camp1", "Campaign One")
    for i in range(10):
        _seed_item(conn, f"c1-{i}", "camp1", "2026-01-01T00:00:00")
    for i in range(4):
        _mark_not_interested(conn, f"c1-{i}")

    results = performance.campaign_comparison(conn)
    by_id = {r["campaign_id"]: r for r in results}
    assert by_id["camp1"]["failure_rate"] == 40.0
    # A rejection still counts as a reply -- reply_rate and failure_rate
    # are not mutually exclusive views of the same items.
    assert by_id["camp1"]["reply_rate"] == 40.0


# --- followup_performance -------------------------------------------------------

def test_followup_performance_buckets(conn):
    _seed_campaign(conn)
    _seed_item(conn, "i0", "camp1", "2026-01-01T00:00:00", followup_count=0)
    _seed_item(conn, "i1", "camp1", "2026-01-01T00:00:00", followup_count=1)
    _seed_item(conn, "i2", "camp1", "2026-01-01T00:00:00", followup_count=2)
    _mark_replied(conn, "i1")
    _mark_replied(conn, "i2")

    result = performance.followup_performance(conn)
    buckets = {b["followup_count"]: b for b in result["buckets"]}
    assert buckets["0"]["reply_rate"] == 0.0
    assert buckets["1"]["reply_rate"] == 100.0
    assert buckets["2+"]["reply_rate"] == 100.0


def test_followup_performance_insufficient_data_when_no_sent_items(conn):
    result = performance.followup_performance(conn)
    assert result["insufficient_data"] is True
    assert result["buckets"] == []


# --- learning_era_comparison ----------------------------------------------------

def test_learning_era_comparison_insufficient_data_without_draft_versions(conn):
    result = performance.learning_era_comparison(conn, metric="reply_rate")
    assert result["insufficient_data"] is True
