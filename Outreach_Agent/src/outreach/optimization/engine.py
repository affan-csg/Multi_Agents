"""Insight computation: turns strategy profiles + outcome data into
confidence-scored, explainable campaign_optimization_insights rows.

Extends ../learning/engine.py's evidence-gating philosophy from single-edit
diffs to campaign/strategy-level comparisons. Reuses the exact same
thresholds (imported, not re-declared) so "how much evidence is enough" means
the same thing everywhere in this codebase -- there is no separate, looser
bar for campaign-level claims than for edit-level ones.

Every insight is traceable to a fixed, enumerable pattern_key (dimension +
optional segment), a stored evidence_count, and a metric_comparison built
directly from strategy profiles + outcome_summary. Recomputation is
on-demand (called from every read endpoint that needs current insights),
matching the Learning Agent's manual-trigger philosophy -- no background
poller exists anywhere in this codebase.

Segmentation (invariant 3 in __init__.py): for each dimension, this module
first tries every enumerable ICP segment (prospects.tier, prospects.industry)
that independently clears the sample-size gate, and ALSO always computes the
global (unsegmented) comparison. A segment is only ever reported as 'active'
on its own evidence -- a global pattern being active never implies a segment
is, and vice versa. This is what keeps a real per-segment effect (e.g. "short
CTAs work well for tier A, not tier C") from being flattened into one
misleading global number, and keeps a real global effect from being hidden
just because no single segment alone has quite enough data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..db import SupabaseConn, utcnow
from ..learning.engine import (
    HIGH_CONFIDENCE_MIN,
    MEDIUM_CONFIDENCE_MIN,
    MIN_EFFECT_SIZE_PP,
    MIN_EVIDENCE_FOR_ANY_INSIGHT,
    MIN_GROUP_SIZE,
    compute_confidence,
)
from . import db as opt_db
from .models import CampaignOptimizationInsight, Confidence, StrategyProfile

# Booking rate is the primary metric wherever a comparison has enough booked
# outcomes to be meaningful (see rl_prompt-equivalent guidance: "more
# importantly, positive-reply and booking rate"), falling back to
# positive_reply_rate, then reply_rate, purely so the headline number shown
# is the most business-meaningful one that the data can actually support --
# all four rates are always included in metric_comparison regardless.
_METRIC_PRIORITY = ("booking_rate", "positive_reply_rate", "reply_rate")
_MIN_METRIC_EVENTS = 3  # need at least this many positive events for a metric to be "supported", not just technically nonzero


def _rate(n_true: int, n_total: int) -> float:
    return round(100.0 * n_true / n_total, 1) if n_total else 0.0


@dataclass
class _GroupResult:
    label: str
    n: int
    reply_rate: float
    positive_reply_rate: float
    booking_rate: float
    no_reply_rate: float
    # Share of this group that ended in a clear rejection (the terminal
    # not_interested stage, "Failed" in the UI). Reported alongside the other
    # rates as context -- never fed into _primary_metric/_metric_effect,
    # since "higher is better" does not hold for it the way it does for the
    # other three, and folding it into the same max-seeking comparison would
    # silently surface "this segment fails more" as if it were a preferred
    # pattern.
    failure_rate: float
    item_ids: list[str]


def _group_result(label: str, item_ids: list[str], outcomes: dict[str, Any]) -> _GroupResult:
    n = len(item_ids)
    replied = sum(1 for i in item_ids if outcomes.get(i, {}).get("replied"))
    positive = sum(1 for i in item_ids if outcomes.get(i, {}).get("positive_reply"))
    booked = sum(1 for i in item_ids if outcomes.get(i, {}).get("booked"))
    failed = sum(1 for i in item_ids if outcomes.get(i, {}).get("failed"))
    no_reply = n - replied
    return _GroupResult(
        label=label, n=n,
        reply_rate=_rate(replied, n), positive_reply_rate=_rate(positive, n),
        booking_rate=_rate(booked, n), no_reply_rate=_rate(no_reply, n),
        failure_rate=_rate(failed, n),
        item_ids=item_ids,
    )


def _primary_metric(group_a: _GroupResult, group_b: _GroupResult, outcomes: dict[str, Any]) -> str:
    """Picks the most business-meaningful metric that the data can actually
    support -- booking events are rare, so a comparison with too few booked
    outcomes falls back to positive-reply, then plain reply rate, rather than
    reporting a booking-rate "pattern" built on 1-2 bookings."""
    for metric_key, outcome_key in (("booking_rate", "booked"), ("positive_reply_rate", "positive_reply")):
        n_events = sum(1 for i in (group_a.item_ids + group_b.item_ids) if outcomes.get(i, {}).get(outcome_key))
        if n_events >= _MIN_METRIC_EVENTS:
            return metric_key
    return "reply_rate"


def _metric_effect(group_a: _GroupResult, group_b: _GroupResult, metric: str) -> float:
    return getattr(group_a, metric) - getattr(group_b, metric)


def _build_insight(
    pattern_key: str, pattern_label: str, dimension: str, segment: str, description: str,
    group_a: _GroupResult, group_b: _GroupResult, campaign_ids: set[str], outcomes: dict[str, Any],
) -> CampaignOptimizationInsight:
    evidence_count = group_a.n + group_b.n
    metric = _primary_metric(group_a, group_b, outcomes)
    effect = _metric_effect(group_a, group_b, metric)
    confidence = compute_confidence(evidence_count, group_a.n, group_b.n, effect)
    status = "active" if confidence is not None else "insufficient_data"

    def _group_payload(g: _GroupResult) -> dict[str, Any]:
        return {
            "label": g.label, "n": g.n, "reply_rate": g.reply_rate,
            "positive_reply_rate": g.positive_reply_rate, "booking_rate": g.booking_rate,
            "no_reply_rate": g.no_reply_rate, "failure_rate": g.failure_rate,
        }

    return CampaignOptimizationInsight(
        pattern_key=pattern_key, pattern_label=pattern_label, dimension=dimension, segment=segment,
        description=description, evidence_count=evidence_count,
        metric_comparison={
            "group_a": _group_payload(group_a), "group_b": _group_payload(group_b),
            "primary_metric": metric, "effect_pct_points": round(effect, 1),
        },
        confidence=confidence or "low", status=status,
        evidence_campaign_ids=sorted(campaign_ids),
        evidence_item_ids=(group_a.item_ids + group_b.item_ids)[:50],
        last_updated_at=utcnow(),
    )


# --- dimension detectors: each splits profiles into two comparable groups ---
# Fixed, enumerable catalog -- every (attribute_value_a, attribute_value_b)
# pair a dimension can compare is listed here, not auto-discovered from
# whatever text happens to appear.
_DIMENSION_SPLITS: dict[str, tuple[str, str, str]] = {
    # dimension -> (attribute name on StrategyProfile, value_a, value_b)
    "email_style": ("email_style", "research_backed", "template_only"),
    "subject_style": ("subject_style", "question", "statement"),
    "opening_style": ("opening_style", "personalized_fact", "generic"),
    "cta_style": ("cta_style", "direct", "soft"),
    "length_bucket": ("length_bucket", "short", "long"),
    "personalization_level": ("personalization_level", "deep", "none"),
}

_DIMENSION_LABELS = {
    "email_style": "Research-backed vs. template-only emails",
    "subject_style": "Question vs. statement subject lines",
    "opening_style": "Personalized-fact vs. generic openings",
    "cta_style": "Direct vs. soft calls-to-action",
    "length_bucket": "Short vs. long emails",
    "personalization_level": "Deep vs. no personalization",
}


def _split_profiles(
    profiles: list[StrategyProfile], attribute: str, value_a: str, value_b: str,
) -> tuple[list[str], list[str], set[str]]:
    a_ids = [p.item_id for p in profiles if getattr(p, attribute) == value_a]
    b_ids = [p.item_id for p in profiles if getattr(p, attribute) == value_b]
    campaign_ids = {p.campaign_id for p in profiles if p.item_id in a_ids or p.item_id in b_ids}
    return a_ids, b_ids, campaign_ids


def _detect_dimension(
    dimension: str, profiles: list[StrategyProfile], outcomes: dict[str, Any], *, segment: str = "",
) -> Optional[CampaignOptimizationInsight]:
    attribute, value_a, value_b = _DIMENSION_SPLITS[dimension]
    a_ids, b_ids, campaign_ids = _split_profiles(profiles, attribute, value_a, value_b)
    if not a_ids or not b_ids:
        return None

    group_a = _group_result(value_a.replace("_", " ").title(), a_ids, outcomes)
    group_b = _group_result(value_b.replace("_", " ").title(), b_ids, outcomes)

    segment_suffix = f" within segment {segment}" if segment else " across all campaigns"
    description = (
        f"Emails classified as '{group_a.label}' show a different observed outcome rate than "
        f"'{group_b.label}' emails{segment_suffix}."
    )
    pattern_key = f"{dimension}:{value_a}-vs-{value_b}" + (f"@{segment}" if segment else "")
    pattern_label = _DIMENSION_LABELS[dimension] + (f" ({segment})" if segment else "")
    return _build_insight(pattern_key, pattern_label, dimension, segment, description, group_a, group_b, campaign_ids, outcomes)


def _detect_followup_count(profiles_by_item: dict[str, StrategyProfile], items: list[dict[str, Any]], outcomes: dict[str, Any], *, segment: str = "") -> Optional[CampaignOptimizationInsight]:
    """Not a StrategyProfile attribute -- reads outreach_items.followup_count
    directly, same data source as ../learning/engine.py's
    _detect_followup_timing, but scoped/segmentable here and reported with
    all four outcome rates rather than reply rate alone."""
    relevant = [i for i in items if i["item_id"] in profiles_by_item]
    if not relevant:
        return None
    zero_ids = [i["item_id"] for i in relevant if (i.get("followup_count") or 0) == 0]
    one_plus_ids = [i["item_id"] for i in relevant if (i.get("followup_count") or 0) >= 1]
    if not zero_ids or not one_plus_ids:
        return None

    group_a = _group_result("1+ follow-ups", one_plus_ids, outcomes)
    group_b = _group_result("No follow-up", zero_ids, outcomes)
    campaign_ids = {profiles_by_item[i].campaign_id for i in zero_ids + one_plus_ids}
    segment_suffix = f" within segment {segment}" if segment else " across all campaigns"
    description = f"Items that received at least one follow-up show a different observed outcome rate than items that received none{segment_suffix}."
    pattern_key = "followup_count:1plus-vs-0" + (f"@{segment}" if segment else "")
    pattern_label = "Follow-up count" + (f" ({segment})" if segment else "")
    return _build_insight(pattern_key, pattern_label, "followup_count", segment, description, group_a, group_b, campaign_ids, outcomes)


# --- ICP segmentation --------------------------------------------------------
# Fixed, enumerable set of segment dimensions -- prospects.tier and
# prospects.industry are the only ICP/company-type fields currently on the
# prospects snapshot (see schema.sql). Adding a new segment dimension means
# adding it here explicitly, not auto-discovering arbitrary column values.
SEGMENT_DIMENSIONS = ("tier", "industry")

# A segment is only evaluated at all once it has at least this many sent
# items -- below this, even attempting the two-group split inside it is
# almost certain to fail the MIN_GROUP_SIZE gate anyway, so skipping early
# just avoids wasted work, not a separate, looser bar.
_MIN_SEGMENT_ITEMS = MIN_EVIDENCE_FOR_ANY_INSIGHT


def _enumerate_segments(items: list[dict[str, Any]]) -> list[str]:
    """Returns segment labels like 'tier=A' for every ICP value that appears
    on at least _MIN_SEGMENT_ITEMS sent items -- values with too little data
    are never even attempted, so they can't produce a spurious insight."""
    segments = []
    for dim in SEGMENT_DIMENSIONS:
        counts: dict[str, int] = {}
        for i in items:
            value = (i.get("_prospect") or {}).get(dim)
            if value:
                counts[value] = counts.get(value, 0) + 1
        for value, count in counts.items():
            if count >= _MIN_SEGMENT_ITEMS:
                segments.append(f"{dim}={value}")
    return segments


def recompute_insights(conn: SupabaseConn, *, campaign_ids: Optional[list[str]] = None) -> list[CampaignOptimizationInsight]:
    """Entry point called by every read endpoint that needs current
    insights. Runs every dimension detector globally AND within every ICP
    segment that has enough data, upserts campaign_optimization_insights,
    and returns the current full list."""
    items = opt_db.list_sent_items_with_context(conn, campaign_ids=campaign_ids)
    if not items:
        return opt_db.list_insights(conn)

    item_ids = [i["item_id"] for i in items]
    outcomes = opt_db.outcome_summary(conn, item_ids)
    all_profiles = opt_db.build_strategy_profiles(conn, campaign_ids=campaign_ids)
    profiles_by_item = {p.item_id: p for p in all_profiles}

    results: list[CampaignOptimizationInsight] = []

    # Global (unsegmented) pass -- always computed regardless of whether any
    # segment also qualifies, per invariant 3.
    for dimension in _DIMENSION_SPLITS:
        insight = _detect_dimension(dimension, all_profiles, outcomes)
        if insight is not None:
            results.append(insight)
    followup_insight = _detect_followup_count(profiles_by_item, items, outcomes)
    if followup_insight is not None:
        results.append(followup_insight)

    # Segmented passes -- one independent comparison per qualifying segment,
    # never blended with the global numbers or with each other.
    for segment in _enumerate_segments(items):
        dim_name, dim_value = segment.split("=", 1)
        segment_item_ids = {i["item_id"] for i in items if (i.get("_prospect") or {}).get(dim_name) == dim_value}
        segment_profiles = [p for p in all_profiles if p.item_id in segment_item_ids]
        segment_items = [i for i in items if i["item_id"] in segment_item_ids]

        for dimension in _DIMENSION_SPLITS:
            insight = _detect_dimension(dimension, segment_profiles, outcomes, segment=segment)
            if insight is not None:
                results.append(insight)
        followup_insight = _detect_followup_count(profiles_by_item, segment_items, outcomes, segment=segment)
        if followup_insight is not None:
            results.append(followup_insight)

    for insight in results:
        _persist_with_history(conn, insight)

    return opt_db.list_insights(conn)


def _persist_with_history(conn: SupabaseConn, insight: CampaignOptimizationInsight) -> None:
    """Preserves first_observed_at across recomputes, same as
    ../learning/engine.py's _persist_with_history -- a pattern's "when was
    this first detected" must not reset to now() on every read."""
    previous = opt_db.get_insight(conn, insight.pattern_key)
    if previous is not None:
        insight = insight.model_copy(update={"first_observed_at": previous.first_observed_at})
    else:
        insight = insight.model_copy(update={"first_observed_at": insight.last_updated_at})
    opt_db.upsert_insight(conn, insight)
