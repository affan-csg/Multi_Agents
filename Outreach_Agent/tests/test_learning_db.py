"""Round-trip tests for learning/db.py against the shared FakeConn fixture --
insert/list/get for all 3 new tables, plus the outcome-join helpers over the
existing outreach_items/conversation_messages tables."""

from __future__ import annotations

from datetime import datetime, timedelta

from outreach.learning import db as learning_db
from outreach.learning.models import DraftVersion, LearningEvent, LearningInsight


def _seed_item(conn, item_id="item-1", campaign_id="camp-1", stage="sent"):
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": "C"})
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": item_id, "campaign_id": campaign_id, "research_domain": "acme.com",
        "stage": stage, "channel": "email", "subject": "s", "body": "b",
        "personalized_from": "pain_summary", "to_email": "a@acme.com",
        "sent_at": "2026-01-01T00:00:00", "created_at": "2026-01-01T00:00:00",
        "stage_updated_at": "2026-01-01T00:00:00", "followup_count": 0,
    })


def _dv(version_id="v1", item_id="item-1", campaign_id="camp-1", **overrides):
    fields = dict(
        version_id=version_id, item_id=item_id, campaign_id=campaign_id, source="revise",
        before_subject="a", before_body="b", after_subject="c", after_body="d",
    )
    fields.update(overrides)
    return DraftVersion(**fields)


def test_insert_and_get_draft_version(conn):
    learning_db.insert_draft_version(conn, _dv())
    got = learning_db.get_draft_version(conn, "v1")
    assert got is not None
    assert got.item_id == "item-1"
    assert got.source == "revise"


def test_get_draft_version_missing_returns_none(conn):
    assert learning_db.get_draft_version(conn, "nope") is None


def test_list_draft_versions_filters(conn):
    learning_db.insert_draft_version(conn, _dv("v1", "item-1", "camp-1", source="revise"))
    learning_db.insert_draft_version(conn, _dv("v2", "item-2", "camp-1", source="redraft"))
    learning_db.insert_draft_version(conn, _dv("v3", "item-1", "camp-2", source="revise"))

    by_item = learning_db.list_draft_versions(conn, item_id="item-1")
    assert {v.version_id for v in by_item} == {"v1", "v3"}

    by_campaign = learning_db.list_draft_versions(conn, campaign_id="camp-1")
    assert {v.version_id for v in by_campaign} == {"v1", "v2"}

    by_source = learning_db.list_draft_versions(conn, source="redraft")
    assert {v.version_id for v in by_source} == {"v2"}


def test_list_draft_versions_limit_and_offset(conn):
    for i in range(5):
        learning_db.insert_draft_version(conn, _dv(f"v{i}"))
    page = learning_db.list_draft_versions(conn, limit=2, offset=1)
    assert len(page) == 2


def test_record_draft_edit_computes_diff_and_inserts(conn):
    _seed_item(conn)
    learning_db.record_draft_edit(
        conn, item_id="item-1", campaign_id="camp-1", source="revise",
        before_subject="Old", before_body="Hi there, worth a call?",
        after_subject="New", after_body="Hi, worth a quick chat?",
        instruction="shorten it",
    )
    versions = learning_db.list_draft_versions(conn, item_id="item-1")
    assert len(versions) == 1
    assert versions[0].subject_changed is True
    assert versions[0].instruction == "shorten it"


def test_outreach_outcome_summary_empty_ids(conn):
    assert learning_db.outreach_outcome_summary(conn, []) == {}


def test_outreach_outcome_summary_replied_and_positive(conn):
    _seed_item(conn, item_id="item-1", stage="replied")
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "m1", "item_id": "item-1", "direction": "inbound",
        "intent": "interested", "created_at": "2026-01-02T00:00:00",
    })
    summary = learning_db.outreach_outcome_summary(conn, ["item-1"])
    assert summary["item-1"]["replied"] is True
    assert summary["item-1"]["positive_reply"] is True
    assert summary["item-1"]["booked"] is False


def test_outreach_outcome_summary_booked(conn):
    _seed_item(conn, item_id="item-1", stage="booked")
    summary = learning_db.outreach_outcome_summary(conn, ["item-1"])
    assert summary["item-1"]["booked"] is True


def test_outreach_outcome_summary_no_reply(conn):
    _seed_item(conn, item_id="item-1", stage="waiting")
    summary = learning_db.outreach_outcome_summary(conn, ["item-1"])
    assert summary["item-1"]["replied"] is False
    assert summary["item-1"]["positive_reply"] is False


def test_outreach_outcome_summary_missing_item_omitted(conn):
    summary = learning_db.outreach_outcome_summary(conn, ["does-not-exist"])
    assert summary == {}


def test_outreach_outcome_summary_not_interested_is_failed_and_replied(conn):
    """A clear rejection (the not_interested/"Failed" stage) is flagged
    `failed` -- and still counts as `replied`, since a human did reply, same
    convention `booked` already gets."""
    _seed_item(conn, item_id="item-1", stage="not_interested")
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "m1", "item_id": "item-1", "direction": "inbound",
        "intent": "not_interested", "created_at": "2026-01-02T00:00:00",
    })
    summary = learning_db.outreach_outcome_summary(conn, ["item-1"])

    assert summary["item-1"]["failed"] is True
    assert summary["item-1"]["replied"] is True
    assert summary["item-1"]["positive_reply"] is False
    assert summary["item-1"]["booked"] is False


def test_outreach_outcome_summary_booked_is_not_failed(conn):
    _seed_item(conn, item_id="item-1", stage="booked")
    summary = learning_db.outreach_outcome_summary(conn, ["item-1"])
    assert summary["item-1"]["failed"] is False


def test_refresh_draft_version_outcomes_backfills_columns(conn):
    _seed_item(conn, item_id="item-1", stage="replied")
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": "m1", "item_id": "item-1", "direction": "inbound",
        "intent": "interested", "created_at": "2026-01-02T00:00:00",
    })
    learning_db.insert_draft_version(conn, _dv("v1", "item-1", "camp-1"))
    updated = learning_db.refresh_draft_version_outcomes(conn)
    assert updated == 1
    refreshed = learning_db.get_draft_version(conn, "v1")
    assert refreshed.outcome_stage == "replied"
    assert refreshed.outcome_replied is True
    assert refreshed.outcome_positive_reply is True


def test_refresh_draft_version_outcomes_empty_table_returns_zero(conn):
    assert learning_db.refresh_draft_version_outcomes(conn) == 0


def test_refresh_draft_version_outcomes_skips_orphaned_item(conn):
    # draft_versions row whose item was deleted -- must not raise.
    learning_db.insert_draft_version(conn, _dv("v1", "item-deleted", "camp-1"))
    updated = learning_db.refresh_draft_version_outcomes(conn)
    assert updated == 0


# --- learning_insights ---------------------------------------------------------

def _insight(pattern_key="shorter_cta", **overrides):
    fields = dict(
        pattern_key=pattern_key, pattern_label="Shorter CTAs", category="cta",
        description="desc", evidence_count=50, confidence="medium", status="active",
    )
    fields.update(overrides)
    return LearningInsight(**fields)


def test_upsert_and_get_insight(conn):
    learning_db.upsert_insight(conn, _insight())
    got = learning_db.get_insight(conn, "shorter_cta")
    assert got is not None
    assert got.confidence == "medium"


def test_upsert_insight_overwrites_by_pattern_key(conn):
    learning_db.upsert_insight(conn, _insight(evidence_count=50, confidence="medium"))
    learning_db.upsert_insight(conn, _insight(evidence_count=200, confidence="high"))
    got = learning_db.get_insight(conn, "shorter_cta")
    assert got.evidence_count == 200
    assert got.confidence == "high"
    assert len(conn.tables["learning_insights"]) == 1


def test_list_insights_filters_by_status_and_category(conn):
    learning_db.upsert_insight(conn, _insight("a", category="cta", status="active"))
    learning_db.upsert_insight(conn, _insight("b", category="length", status="insufficient_data"))
    active = learning_db.list_insights(conn, status="active")
    assert {i.pattern_key for i in active} == {"a"}
    cta = learning_db.list_insights(conn, category="cta")
    assert {i.pattern_key for i in cta} == {"a"}


def test_get_insight_missing_returns_none(conn):
    assert learning_db.get_insight(conn, "nope") is None


# --- learning_events -----------------------------------------------------------

def test_add_and_list_learning_events(conn):
    learning_db.add_learning_event(conn, LearningEvent(
        event_id="e1", pattern_key="shorter_cta", event_type="insight_detected", detail={},
    ))
    events = learning_db.list_learning_events(conn)
    assert len(events) == 1
    assert events[0].event_type == "insight_detected"


def test_list_learning_events_respects_limit(conn):
    for i in range(10):
        learning_db.add_learning_event(conn, LearningEvent(
            event_id=f"e{i}", pattern_key="shorter_cta", event_type="insight_detected", detail={},
        ))
    events = learning_db.list_learning_events(conn, limit=3)
    assert len(events) == 3
