"""Malformed/missing-data handling: None/empty draft text, orphaned
draft_versions rows (item deleted, no matching conversation_messages), and
an empty draft_versions table -- none of these should ever raise."""

from __future__ import annotations

from outreach.learning import db as learning_db
from outreach.learning import performance
from outreach.learning.diffing import compute_diff
from outreach.learning.engine import recompute_insights
from outreach.learning.models import DraftVersion


def test_compute_diff_all_none():
    diff = compute_diff(None, None, None, None)
    assert diff["subject_changed"] is False
    assert diff["sentences_removed_count"] == 0
    assert diff["personalization_delta"] == 0


def test_compute_diff_mixed_none_and_text():
    diff = compute_diff(None, None, "Subject", "Hi there, worth a chat?")
    assert diff["subject_changed"] is True
    assert diff["opening_changed"] is True


def test_compute_diff_prospect_missing_fields():
    diff = compute_diff("s", "Hi there", "s", "Hi Jordan", prospect={})
    assert diff["personalization_delta"] == 0


def test_recompute_insights_empty_table_returns_empty_list(conn):
    insights = recompute_insights(conn)
    assert insights == []


def test_recompute_insights_with_orphaned_draft_version(conn):
    """draft_versions row whose item_id has no matching outreach_items row
    (e.g. the item was deleted) -- outcome refresh must skip it, not raise,
    and it must simply not count toward any outcome-linked insight."""
    learning_db.insert_draft_version(conn, DraftVersion(
        version_id="v1", item_id="deleted-item", campaign_id="camp1", source="revise",
        before_subject="a", before_body="b", after_subject="c", after_body="d",
        cta_changed=True, cta_shortened=True,
    ))
    insights = recompute_insights(conn)
    # Should not raise; outcome_replied stays None for the orphaned row, so
    # it's excluded from outcome-linked detectors.
    shorter_cta = next((i for i in insights if i.pattern_key == "shorter_cta"), None)
    assert shorter_cta is None


def test_refresh_draft_version_outcomes_with_no_matching_items(conn):
    learning_db.insert_draft_version(conn, DraftVersion(
        version_id="v1", item_id="ghost", campaign_id="camp1", source="revise",
    ))
    updated = learning_db.refresh_draft_version_outcomes(conn)
    assert updated == 0


def test_performance_before_after_with_no_items_at_all(conn):
    result = performance.before_after_comparison(conn, metric="reply_rate", window=5)
    assert result["insufficient_data"] is True


def test_performance_campaign_comparison_with_no_campaigns(conn):
    assert performance.campaign_comparison(conn) == []


def test_performance_followup_with_no_items(conn):
    result = performance.followup_performance(conn)
    assert result["insufficient_data"] is True
    assert result["buckets"] == []


def test_outreach_outcome_summary_with_malformed_intent(conn):
    """A conversation_messages row with an unrecognized/garbage intent value
    must not crash the summary -- it should simply not count as a reply."""
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": "item1", "campaign_id": "camp1", "research_domain": "acme.com",
        "stage": "sent", "channel": "email", "created_at": "2026-01-01T00:00:00",
        "stage_updated_at": "2026-01-01T00:00:00", "followup_count": 0,
    })
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "m1", "item_id": "item1", "direction": "inbound",
        "intent": "some_unexpected_value", "created_at": "2026-01-02T00:00:00",
    })
    summary = learning_db.outreach_outcome_summary(conn, ["item1"])
    assert summary["item1"]["replied"] is False
    assert summary["item1"]["positive_reply"] is False


def test_record_draft_edit_with_none_prospect(conn):
    conn.tables.setdefault("campaigns", []).append({"campaign_id": "camp1", "name": "C"})
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": "item1", "campaign_id": "camp1", "research_domain": "acme.com",
        "stage": "sent", "subject": "s", "body": "b", "created_at": "2026-01-01T00:00:00",
        "stage_updated_at": "2026-01-01T00:00:00", "followup_count": 0,
    })
    learning_db.record_draft_edit(
        conn, item_id="item1", campaign_id="camp1", source="revise",
        before_subject="a", before_body="b", after_subject="c", after_body="d",
        prospect=None,
    )
    versions = learning_db.list_draft_versions(conn, item_id="item1")
    assert len(versions) == 1
    assert versions[0].personalization_delta == 0
