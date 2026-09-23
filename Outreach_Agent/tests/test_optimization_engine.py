"""Engine-level tests: global dimension comparisons, confidence gating,
insufficient-data handling, and ICP segmentation -- the core evidence-based
comparison logic the user asked for."""

from __future__ import annotations

from outreach.optimization import db as opt_db
from outreach.optimization.engine import _group_result, recompute_insights
from test_optimization_fixtures import (
    build_cta_style_dataset,
    build_length_dataset,
    build_segmented_dataset,
    seed_campaign,
    seed_prospect,
)


def test_detects_cta_style_pattern_at_sufficient_sample_size(conn):
    total = build_cta_style_dataset(conn, n_direct=40, n_soft=40)
    assert total == 80

    insights = recompute_insights(conn)
    cta_insight = next((i for i in insights if i.pattern_key == "cta_style:direct-vs-soft"), None)

    assert cta_insight is not None
    assert cta_insight.status == "active"
    assert cta_insight.confidence in ("medium", "high")
    assert cta_insight.evidence_count == 80
    assert cta_insight.segment == ""
    group_a = cta_insight.metric_comparison["group_a"]
    group_b = cta_insight.metric_comparison["group_b"]
    assert group_a["n"] == 40
    assert group_b["n"] == 40
    # Exact deterministic reply rates: 20/40 direct = 50%, 2/40 soft = 5%.
    assert group_a["reply_rate"] == 50.0
    assert group_b["reply_rate"] == 5.0


def test_small_sample_does_not_trigger_false_insight(conn):
    """Same shape, n=6 total -- must stay insufficient_data even with a
    dramatic swing, matching the Learning Agent's own small-sample test."""
    total = build_cta_style_dataset(
        conn, campaign_id="tiny-cta-camp", n_direct=3, n_soft=3,
        direct_reply_every=1, soft_reply_every=99,
    )
    assert total == 6

    insights = recompute_insights(conn)
    active_keys = {i.pattern_key for i in insights if i.status == "active"}
    assert "cta_style:direct-vs-soft" not in active_keys

    cta_insight = next((i for i in insights if i.pattern_key == "cta_style:direct-vs-soft"), None)
    if cta_insight is not None:
        assert cta_insight.status == "insufficient_data"


def test_no_insight_at_all_when_no_campaigns_exist(conn):
    """Zero sent items anywhere -- must return an empty/insufficient list,
    never a fabricated recommendation."""
    insights = recompute_insights(conn)
    assert all(i.status != "active" for i in insights)


def test_lopsided_group_sizes_do_not_trigger_false_confidence(conn):
    """95 direct-CTA emails vs. only 3 soft-CTA emails -- even though the
    TOTAL evidence count clears 20, the tiny group must fail the
    per-group MIN_GROUP_SIZE gate."""
    total = build_cta_style_dataset(
        conn, campaign_id="lopsided-camp", n_direct=95, n_soft=3,
        direct_reply_every=2, soft_reply_every=1,
    )
    assert total == 98

    insights = recompute_insights(conn)
    cta_insight = next((i for i in insights if i.pattern_key == "cta_style:direct-vs-soft"), None)
    assert cta_insight is not None
    assert cta_insight.status == "insufficient_data"


def test_detects_length_pattern(conn):
    total = build_length_dataset(conn, n_short=40, n_long=40)
    assert total == 80

    insights = recompute_insights(conn)
    length_insight = next((i for i in insights if i.pattern_key == "length_bucket:short-vs-long"), None)
    assert length_insight is not None
    assert length_insight.status == "active"
    assert length_insight.dimension == "length_bucket"


def test_segmentation_surfaces_tier_specific_pattern_without_fabricating_global_one(conn):
    """Direct CTA strongly helps tier A (50% vs 5%) but has NO effect in tier
    C (20% vs 20%) -- the segmented tier=A insight must be active, the
    segmented tier=C insight must NOT claim a pattern, and the blended
    global comparison (which mixes both, diluting the real tier-A effect)
    must not overstate what tier C's flat data shows."""
    total = build_segmented_dataset(conn)
    assert total == 120

    insights = recompute_insights(conn)
    by_key = {i.pattern_key: i for i in insights}

    tier_a = by_key.get("cta_style:direct-vs-soft@tier=A")
    assert tier_a is not None
    assert tier_a.status == "active"
    assert tier_a.segment == "tier=A"
    assert tier_a.metric_comparison["group_a"]["reply_rate"] == 50.0
    assert tier_a.metric_comparison["group_b"]["reply_rate"] == 3.3

    tier_c = by_key.get("cta_style:direct-vs-soft@tier=C")
    # Tier C's true rates are identical (20% vs 20%) -- effect size is 0,
    # so it must never be reported as active regardless of sample size.
    assert tier_c is not None
    assert tier_c.status == "insufficient_data"
    assert tier_c.metric_comparison["effect_pct_points"] == 0.0


def test_segmented_and_global_insights_are_independent_rows(conn):
    """A segment being active must not be conflated with, or overwrite, the
    global (unsegmented) pattern_key for the same dimension -- they are
    different rows with different pattern_keys."""
    build_segmented_dataset(conn)
    insights = recompute_insights(conn)
    keys = {i.pattern_key for i in insights}
    assert "cta_style:direct-vs-soft" in keys  # global
    assert "cta_style:direct-vs-soft@tier=A" in keys  # segmented
    assert keys.issuperset({"cta_style:direct-vs-soft", "cta_style:direct-vs-soft@tier=A", "cta_style:direct-vs-soft@tier=C"})


def test_first_observed_at_preserved_across_recomputes(conn):
    build_cta_style_dataset(conn, n_direct=40, n_soft=40)
    first = recompute_insights(conn)
    first_ts = next(i.first_observed_at for i in first if i.pattern_key == "cta_style:direct-vs-soft")

    second = recompute_insights(conn)
    second_ts = next(i.first_observed_at for i in second if i.pattern_key == "cta_style:direct-vs-soft")

    assert first_ts == second_ts


def test_confidence_scales_with_evidence_count(conn):
    """~80 emails with a big effect should be medium/high, not the maximum
    'high' tier reserved for HIGH_CONFIDENCE_MIN (150) evidence count."""
    build_cta_style_dataset(conn, n_direct=40, n_soft=40)
    insights = recompute_insights(conn)
    cta_insight = next(i for i in insights if i.pattern_key == "cta_style:direct-vs-soft")
    assert cta_insight.confidence == "medium"


def test_high_confidence_at_large_evidence_count(conn):
    build_cta_style_dataset(conn, campaign_id="big-camp", n_direct=100, n_soft=100)
    insights = recompute_insights(conn)
    cta_insight = next(i for i in insights if i.pattern_key == "cta_style:direct-vs-soft")
    assert cta_insight.evidence_count == 200
    assert cta_insight.confidence == "high"


# ---------------------------------------------------------------------------
# failure_rate -- the not_interested ("Failed") outcome, a negative campaign
# metric reported alongside booking_rate/reply_rate/no_reply_rate but never
# used to pick a pattern's primary_metric (see engine.py's comment on
# _GroupResult.failure_rate).
# ---------------------------------------------------------------------------

def _seed_item_with_stage(conn, item_id, campaign_id, domain, *, stage):
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": item_id, "campaign_id": campaign_id, "research_domain": domain,
        "stage": stage, "channel": "email", "subject": "Hi", "body": "Body",
        "personalized_from": "pain_summary", "sent_at": "2026-01-01T00:00:00",
        "created_at": "2026-01-01T00:00:00", "stage_updated_at": "2026-01-01T00:00:00",
        "followup_count": 0,
    })


def test_outcome_summary_flags_not_interested_as_failed(conn):
    seed_campaign(conn, "fail-camp")
    seed_prospect(conn, "a.com")
    seed_prospect(conn, "b.com")
    _seed_item_with_stage(conn, "item-a", "fail-camp", "a.com", stage="not_interested")
    _seed_item_with_stage(conn, "item-b", "fail-camp", "b.com", stage="booked")

    outcomes = opt_db.outcome_summary(conn, ["item-a", "item-b"])

    assert outcomes["item-a"]["failed"] is True
    assert outcomes["item-a"]["booked"] is False
    assert outcomes["item-b"]["failed"] is False
    assert outcomes["item-b"]["booked"] is True


def test_group_result_computes_failure_rate(conn):
    seed_campaign(conn, "fail-camp-2")
    ids = []
    for i in range(4):
        domain = f"fail{i}.com"
        seed_prospect(conn, domain)
        item_id = f"item-{i}"
        # 1 of 4 rejected, 1 of 4 booked, 2 of 4 still open.
        stage = "not_interested" if i == 0 else ("booked" if i == 1 else "sent")
        _seed_item_with_stage(conn, item_id, "fail-camp-2", domain, stage=stage)
        ids.append(item_id)

    outcomes = opt_db.outcome_summary(conn, ids)
    group = _group_result("All", ids, outcomes)

    assert group.n == 4
    assert group.failure_rate == 25.0
    assert group.booking_rate == 25.0


def test_failure_rate_never_selected_as_primary_metric(conn):
    """A segment with a much higher failure_rate than another must not be
    reported as if failing MORE were the 'preferred' pattern -- failure_rate
    is informational only, never fed into _primary_metric's max-seeking
    comparison (booking_rate/positive_reply_rate/reply_rate only)."""
    from outreach.optimization.engine import _primary_metric

    seed_campaign(conn, "fail-camp-3")
    group_a_ids, group_b_ids = [], []
    for i in range(20):
        domain = f"fa{i}.com"
        seed_prospect(conn, domain)
        item_id = f"a-{i}"
        # Group A: 15 of 20 rejected, 0 booked/positive.
        stage = "not_interested" if i < 15 else "sent"
        _seed_item_with_stage(conn, item_id, "fail-camp-3", domain, stage=stage)
        group_a_ids.append(item_id)
    for i in range(20):
        domain = f"fb{i}.com"
        seed_prospect(conn, domain)
        item_id = f"b-{i}"
        # Group B: no rejections, no bookings either -- everything just sits open.
        _seed_item_with_stage(conn, item_id, "fail-camp-3", domain, stage="sent")
        group_b_ids.append(item_id)

    outcomes = opt_db.outcome_summary(conn, group_a_ids + group_b_ids)
    group_a = _group_result("A", group_a_ids, outcomes)
    group_b = _group_result("B", group_b_ids, outcomes)

    assert group_a.failure_rate == 75.0
    assert group_b.failure_rate == 0.0
    # No booked/positive_reply events anywhere -> falls back to reply_rate,
    # never "failure_rate", which is not even a valid primary_metric value.
    assert _primary_metric(group_a, group_b, outcomes) == "reply_rate"
