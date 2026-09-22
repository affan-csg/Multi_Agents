"""rl_prompt.md section 14: deterministic synthetic fixtures proving the
learning system can detect a real pattern (short CTA -> higher observed
reply rate) at a sufficient sample size, while NOT declaring a pattern when
the sample size is too small -- and that a recurring human-edit preference
(shortening the CTA) is detected across many edits."""

from __future__ import annotations

from outreach.learning import db as learning_db
from outreach.learning.engine import recompute_insights
from outreach.learning.models import DraftVersion


def _build_synthetic_dataset(
    conn, *, n_short_cta: int = 60, n_long_cta: int = 60,
    short_reply_every: int = 2, long_reply_every: int = 20,
    campaign_id: str = "synthetic-camp",
):
    """Deterministic (modulo-based, no randomness) synthetic campaign: half
    the items get a shortened-CTA revision, half get a changed-but-not-
    shortened CTA revision. Outcomes assigned by exact modulo rather than
    random.random(), so expected reply-rate numbers are exact integers, not
    approximately-N flaky assertions."""
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": "Synthetic"})
    total = n_short_cta + n_long_cta
    for i in range(total):
        item_id = f"syn-item-{i}"
        conn.tables.setdefault("outreach_items", []).append({
            "item_id": item_id, "campaign_id": campaign_id, "research_domain": f"syn{i}.com",
            "stage": "sent", "channel": "email", "subject": "s", "body": "b",
            "personalized_from": "pain_summary", "sent_at": "2026-01-01T00:00:00",
            "created_at": "2026-01-01T00:00:00", "stage_updated_at": "2026-01-01T00:00:00",
            "followup_count": 0,
        })
        is_short = i < n_short_cta
        learning_db.insert_draft_version(conn, DraftVersion(
            version_id=f"syn-v-{i}", item_id=item_id, campaign_id=campaign_id, source="revise",
            before_subject="a", before_body="b", after_subject="c", after_body="d",
            cta_changed=True, cta_shortened=is_short,
        ))
        if is_short and i % short_reply_every == 0:
            conn.tables.setdefault("conversation_messages", []).append({
                "message_id": f"syn-m-{i}", "item_id": item_id, "direction": "inbound",
                "intent": "interested", "created_at": "2026-01-02T00:00:00",
            })
        elif not is_short and i % long_reply_every == 0:
            conn.tables.setdefault("conversation_messages", []).append({
                "message_id": f"syn-m-{i}", "item_id": item_id, "direction": "inbound",
                "intent": "interested", "created_at": "2026-01-02T00:00:00",
            })
    return total


def test_detects_short_cta_pattern_at_sufficient_sample_size(conn):
    """120 synthetic emails, exact 50% vs 5% reply-rate gap -- must surface
    an active, medium-or-higher confidence shorter_cta insight."""
    total = _build_synthetic_dataset(conn, n_short_cta=60, n_long_cta=60)
    assert total == 120

    insights = recompute_insights(conn)
    shorter_cta = next((i for i in insights if i.pattern_key == "shorter_cta"), None)

    assert shorter_cta is not None
    assert shorter_cta.status == "active"
    assert shorter_cta.confidence in ("medium", "high")
    assert shorter_cta.evidence_count == 120
    group_a = shorter_cta.metric_comparison["group_a"]
    group_b = shorter_cta.metric_comparison["group_b"]
    assert group_a["n"] == 60
    assert group_b["n"] == 60
    # Exact deterministic rates: 30/60 short-CTA replies = 50%, 3/60 long = 5%.
    assert group_a["reply_rate"] == 50.0
    assert group_b["reply_rate"] == 5.0


def test_small_sample_does_not_trigger_false_insight(conn):
    """Same shape, n=6 total (3 short / 3 long) -- even with a dramatic
    percentage swing at this size, must stay insufficient_data."""
    total = _build_synthetic_dataset(
        conn, n_short_cta=3, n_long_cta=3, short_reply_every=1, long_reply_every=99,
        campaign_id="tiny-camp",
    )
    assert total == 6

    insights = recompute_insights(conn)
    active_keys = {i.pattern_key for i in insights if i.status == "active"}
    assert "shorter_cta" not in active_keys

    shorter_cta = next((i for i in insights if i.pattern_key == "shorter_cta"), None)
    if shorter_cta is not None:
        assert shorter_cta.status == "insufficient_data"


def test_human_edit_pattern_detected_across_many_edits(conn):
    """30 human edits (source='revise'), an exact 22/30 (~73%) shorten the
    CTA -- must surface human_edit_shorten_cta as an active pattern whose
    reported percentage matches the fixture's exact ratio."""
    conn.tables.setdefault("campaigns", []).append({"campaign_id": "edit-camp", "name": "Edits"})
    for i in range(30):
        item_id = f"edit-item-{i}"
        conn.tables.setdefault("outreach_items", []).append({
            "item_id": item_id, "campaign_id": "edit-camp", "research_domain": f"edit{i}.com",
            "stage": "sent", "created_at": "2026-01-01T00:00:00",
            "stage_updated_at": "2026-01-01T00:00:00", "followup_count": 0,
        })
        shortened = i < 22  # exactly 22/30
        learning_db.insert_draft_version(conn, DraftVersion(
            version_id=f"edit-v-{i}", item_id=item_id, campaign_id="edit-camp", source="revise",
            before_subject="a", before_body="b", after_subject="c", after_body="d",
            cta_shortened=shortened,
        ))

    insights = recompute_insights(conn)
    pattern = next((i for i in insights if i.pattern_key == "human_edit_shorten_cta"), None)

    assert pattern is not None
    assert pattern.status == "active"
    assert pattern.evidence_count == 30
    assert pattern.metric_comparison["matching"] == 22
    assert pattern.metric_comparison["pct"] == 73.3
