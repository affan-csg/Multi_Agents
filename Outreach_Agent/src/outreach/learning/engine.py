"""Insight computation: turns draft_versions + outcome data into
confidence-scored, explainable learning_insights rows.

Non-ML, fully explainable -- see rl_prompt.md's "This is NOT true
neural-network reinforcement learning" and section 15's "I should be able to
click an insight and understand why the system believes it." Every insight
this module produces is traceable to a fixed, enumerable pattern_key (see
PATTERN_DETECTORS below), a stored evidence_count, and a metric_comparison
built directly from draft_versions rows.

Recomputation is on-demand (called from every read endpoint in routes.py
that needs current insights), not on a background schedule -- no background
poller exists anywhere else in this codebase either (the Conversation
Agent's tick is manual-only), so this matches the existing manual-trigger
philosophy. At current/expected data volumes this is a handful of SELECTs
plus in-Python aggregation, the same cost class as api._aggregate_analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..db import SupabaseConn, utcnow
from . import db as learning_db
from .models import Confidence, DraftVersion, InsightCategory, LearningEvent, LearningInsight

# --- confidence thresholds, kept as named constants for easy tuning --------
# See rl_prompt.md section 5: "5 emails should NOT create a High confidence
# rule." Below MIN_EVIDENCE_FOR_ANY_INSIGHT, a pattern never even reaches
# 'active' -- it stays 'insufficient_data' regardless of how large the
# observed gap looks, since small-n percentages swing wildly.
MIN_EVIDENCE_FOR_ANY_INSIGHT = 20
MIN_GROUP_SIZE = 10           # each compared group must independently clear this
LOW_CONFIDENCE_MIN = 20
MEDIUM_CONFIDENCE_MIN = 50
HIGH_CONFIDENCE_MIN = 150
MIN_EFFECT_SIZE_PP = 1.5      # minimum percentage-point gap to count as an observed pattern at all


def compute_confidence(evidence_count: int, group_a_n: int, group_b_n: int, effect_pct_points: float) -> Optional[Confidence]:
    """Returns None when the pattern does not clear the sample-size or
    effect-size gate (caller should mark the insight 'insufficient_data'),
    otherwise the confidence tier the evidence supports. Two gates, both
    must pass:

    1. Sample-size gate: evidence_count >= MIN_EVIDENCE_FOR_ANY_INSIGHT, AND
       each compared group >= MIN_GROUP_SIZE -- a lopsided comparison (95
       short-CTA emails vs 3 long-CTA emails) must not read as strong
       evidence just because the total is large.
    2. Effect-size gate: the observed rate difference must be
       >= MIN_EFFECT_SIZE_PP percentage points -- a 0.1pp difference at any
       sample size is noise, not a pattern (rl_prompt.md section 3: "Do not
       claim causation when the data only shows correlation" -- and do not
       even claim correlation when the gap is this small).
    """
    if evidence_count < MIN_EVIDENCE_FOR_ANY_INSIGHT:
        return None
    if group_a_n < MIN_GROUP_SIZE or group_b_n < MIN_GROUP_SIZE:
        return None
    if abs(effect_pct_points) < MIN_EFFECT_SIZE_PP:
        return None
    if evidence_count >= HIGH_CONFIDENCE_MIN:
        return "high"
    if evidence_count >= MEDIUM_CONFIDENCE_MIN:
        return "medium"
    return "low"


def _rate(n_true: int, n_total: int) -> float:
    return round(100.0 * n_true / n_total, 1) if n_total else 0.0


@dataclass
class _GroupResult:
    label: str
    n: int
    reply_rate: float
    item_ids: list[str]


def _split_by_flag(
    versions: list[DraftVersion], flag: str, *, label_true: str, label_false: str,
) -> tuple[_GroupResult, _GroupResult]:
    group_true = [v for v in versions if getattr(v, flag)]
    group_false = [v for v in versions if not getattr(v, flag)]

    def _result(label: str, group: list[DraftVersion]) -> _GroupResult:
        replied = sum(1 for v in group if v.outcome_replied)
        return _GroupResult(label=label, n=len(group), reply_rate=_rate(replied, len(group)), item_ids=[v.item_id for v in group])

    return _result(label_true, group_true), _result(label_false, group_false)


def _outcome_linked_insight(
    pattern_key: str,
    pattern_label: str,
    category: InsightCategory,
    description: str,
    group_a: _GroupResult,
    group_b: _GroupResult,
    campaign_ids: set[str],
) -> LearningInsight:
    evidence_count = group_a.n + group_b.n
    effect = group_a.reply_rate - group_b.reply_rate
    confidence = compute_confidence(evidence_count, group_a.n, group_b.n, effect)
    status = "active" if confidence is not None else "insufficient_data"
    now = utcnow()
    return LearningInsight(
        pattern_key=pattern_key,
        pattern_label=pattern_label,
        category=category,
        description=description,
        evidence_count=evidence_count,
        metric_comparison={
            "group_a": {"label": group_a.label, "reply_rate": group_a.reply_rate, "n": group_a.n},
            "group_b": {"label": group_b.label, "reply_rate": group_b.reply_rate, "n": group_b.n},
            "effect_pct_points": round(effect, 1),
        },
        confidence=confidence or "low",
        status=status,
        evidence_campaign_ids=sorted(campaign_ids),
        evidence_item_ids=(group_a.item_ids + group_b.item_ids)[:50],
        last_updated_at=now,
    )


def _detect_shorter_cta(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    relevant = [v for v in versions if v.cta_changed and v.outcome_replied is not None]
    if not relevant:
        return None
    shortened, not_shortened = _split_by_flag(
        relevant, "cta_shortened", label_true="Shorter CTA", label_false="Longer/same CTA",
    )
    return _outcome_linked_insight(
        "shorter_cta", "Shorter CTAs", "cta",
        "Emails whose call-to-action was shortened show a different observed reply rate than those whose CTA was changed but not shortened.",
        shortened, not_shortened, {v.campaign_id for v in relevant},
    )


def _detect_shorter_body(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    relevant = [v for v in versions if (v.shortened or v.lengthened) and v.outcome_replied is not None]
    if not relevant:
        return None
    shortened, lengthened = _split_by_flag(
        relevant, "shortened", label_true="Shortened body", label_false="Lengthened body",
    )
    return _outcome_linked_insight(
        "shorter_body", "Shorter email bodies", "length",
        "Emails whose body was shortened show a different observed reply rate than those whose body was lengthened.",
        shortened, lengthened, {v.campaign_id for v in relevant},
    )


def _detect_more_personalization(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    relevant = [v for v in versions if v.outcome_replied is not None]
    if not relevant:
        return None

    def _flag(v: DraftVersion) -> bool:
        return v.personalization_delta > 0

    more = [v for v in relevant if _flag(v)]
    less = [v for v in relevant if not _flag(v)]
    if not more or not less:
        return None

    def _result(label: str, group: list[DraftVersion]) -> _GroupResult:
        replied = sum(1 for v in group if v.outcome_replied)
        return _GroupResult(label=label, n=len(group), reply_rate=_rate(replied, len(group)), item_ids=[v.item_id for v in group])

    group_a = _result("More personalization added", more)
    group_b = _result("No personalization added", less)
    return _outcome_linked_insight(
        "more_personalization", "More specific personalization", "personalization",
        "Emails where an edit added prospect-specific references (name/company/domain) show a different observed reply rate than edits that did not.",
        group_a, group_b, {v.campaign_id for v in relevant},
    )


def _detect_opening_changed(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    relevant = [v for v in versions if v.outcome_replied is not None]
    if not relevant:
        return None
    changed, unchanged = _split_by_flag(
        relevant, "opening_changed", label_true="Opening changed", label_false="Opening unchanged",
    )
    return _outcome_linked_insight(
        "opening_changed", "Rewritten openings", "opening",
        "Emails whose opening sentence was rewritten show a different observed reply rate than emails whose opening was left as drafted.",
        changed, unchanged, {v.campaign_id for v in relevant},
    )


def _detect_followup_timing(conn: SupabaseConn) -> Optional[LearningInsight]:
    """Not draft_versions-based -- reads outreach_items.followup_count +
    outcome directly (rl_prompt.md's "follow-up performance"/"number of
    follow-ups")."""
    items = conn.select("outreach_items", stage="in.(sent,waiting,replied,booked)")
    if not items:
        return None
    item_ids = [i["item_id"] for i in items]
    outcomes = learning_db.outreach_outcome_summary(conn, item_ids)

    buckets: dict[str, list[str]] = {"0": [], "1": [], "2+": []}
    for i in items:
        count = i.get("followup_count") or 0
        key = "2+" if count >= 2 else str(count)
        buckets[key].append(i["item_id"])

    def _result(label: str, ids: list[str]) -> _GroupResult:
        replied = sum(1 for iid in ids if outcomes.get(iid, {}).get("replied"))
        return _GroupResult(label=label, n=len(ids), reply_rate=_rate(replied, len(ids)), item_ids=ids)

    zero = _result("No follow-up", buckets["0"])
    one_plus = _result("1+ follow-ups", buckets["1"] + buckets["2+"])
    if zero.n == 0 or one_plus.n == 0:
        return None

    evidence_count = zero.n + one_plus.n
    effect = one_plus.reply_rate - zero.reply_rate
    confidence = compute_confidence(evidence_count, zero.n, one_plus.n, effect)
    status = "active" if confidence is not None else "insufficient_data"
    campaign_ids = {i["campaign_id"] for i in items}
    return LearningInsight(
        pattern_key="followup_timing",
        pattern_label="Follow-up impact",
        category="followup",
        description="Items that received at least one follow-up show a different observed reply rate than items that received none.",
        evidence_count=evidence_count,
        metric_comparison={
            "group_a": {"label": one_plus.label, "reply_rate": one_plus.reply_rate, "n": one_plus.n},
            "group_b": {"label": zero.label, "reply_rate": zero.reply_rate, "n": zero.n},
            "effect_pct_points": round(effect, 1),
        },
        confidence=confidence or "low",
        status=status,
        evidence_campaign_ids=sorted(campaign_ids),
        evidence_item_ids=(one_plus.item_ids + zero.item_ids)[:50],
        last_updated_at=utcnow(),
    )


# --- human-edit-frequency patterns (rl_prompt.md section 7) -----------------
# These do NOT compare outcomes -- they report the FREQUENCY of a diff flag
# across all edits, since an edit is evidence of preference regardless of
# whether that particular email has been replied to yet. Confidence is
# gated by edit count instead of by two compared outcome groups.

def _human_edit_frequency_insight(
    pattern_key: str, pattern_label: str, description_fmt: str,
    versions: list[DraftVersion], flag: str,
) -> Optional[LearningInsight]:
    edits = [v for v in versions if v.source in ("revise", "redraft")]
    if len(edits) < MIN_EVIDENCE_FOR_ANY_INSIGHT:
        return None
    matching = [v for v in edits if getattr(v, flag)]
    pct = _rate(len(matching), len(edits))
    # Effect-size gate here means "the behavior is common enough to be a
    # pattern, not just something that happened once or twice" -- reuse the
    # same percentage-point-style threshold against a 0% baseline.
    if pct < MIN_EFFECT_SIZE_PP:
        return LearningInsight(
            pattern_key=pattern_key, pattern_label=pattern_label, category="human_edit_pattern",
            description=description_fmt.format(pct=pct), evidence_count=len(edits),
            metric_comparison={"matching": len(matching), "total": len(edits), "pct": pct},
            confidence="low", status="insufficient_data",
            evidence_campaign_ids=sorted({v.campaign_id for v in edits}),
            evidence_item_ids=[v.item_id for v in matching][:50],
            last_updated_at=utcnow(),
        )
    confidence: Confidence
    if len(edits) >= HIGH_CONFIDENCE_MIN:
        confidence = "high"
    elif len(edits) >= MEDIUM_CONFIDENCE_MIN:
        confidence = "medium"
    else:
        confidence = "low"
    return LearningInsight(
        pattern_key=pattern_key, pattern_label=pattern_label, category="human_edit_pattern",
        description=description_fmt.format(pct=pct), evidence_count=len(edits),
        metric_comparison={"matching": len(matching), "total": len(edits), "pct": pct},
        confidence=confidence, status="active",
        evidence_campaign_ids=sorted({v.campaign_id for v in edits}),
        evidence_item_ids=[v.item_id for v in matching][:50],
        last_updated_at=utcnow(),
    )


def _detect_human_edit_shorten_cta(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    return _human_edit_frequency_insight(
        "human_edit_shorten_cta", "Human editing: shortens the CTA",
        "{pct}% of human edits shorten the call-to-action.", versions, "cta_shortened",
    )


def _detect_human_edit_remove_intro(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    return _human_edit_frequency_insight(
        "human_edit_remove_intro", "Human editing: removes intro sentences",
        "{pct}% of human edits remove sentences from the opening/introduction.", versions, "opening_changed",
    )


def _detect_human_edit_specific_opening(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    return _human_edit_frequency_insight(
        "human_edit_specific_opening", "Human editing: makes personalization more specific",
        "{pct}% of human edits add more specific prospect personalization.", versions, "personalization_delta",
    )


def _detect_human_edit_reduce_length(versions: list[DraftVersion]) -> Optional[LearningInsight]:
    return _human_edit_frequency_insight(
        "human_edit_reduce_length", "Human editing: reduces email length",
        "{pct}% of human edits reduce the overall email length.", versions, "shortened",
    )


# Fixed, enumerable catalog -- every insight the dashboard can ever show is
# listed here, not auto-discovered. Detectors taking `versions` only operate
# on draft_versions; `_detect_followup_timing` also needs `conn` since it
# reads outreach_items directly.
_VERSION_DETECTORS: list[Callable[[list[DraftVersion]], Optional[LearningInsight]]] = [
    _detect_shorter_cta,
    _detect_shorter_body,
    _detect_more_personalization,
    _detect_opening_changed,
    _detect_human_edit_shorten_cta,
    _detect_human_edit_remove_intro,
    _detect_human_edit_specific_opening,
    _detect_human_edit_reduce_length,
]


def recompute_insights(conn: SupabaseConn) -> list[LearningInsight]:
    """Entry point called by every read endpoint that needs current
    insights. Refreshes stale outcome columns, runs every detector in the
    fixed catalog, upserts learning_insights, appends learning_events for
    any confidence/status change, and returns the current full list."""
    learning_db.refresh_draft_version_outcomes(conn)
    versions = learning_db.list_draft_versions(conn)

    results: list[LearningInsight] = []
    for detector in _VERSION_DETECTORS:
        insight = detector(versions)
        if insight is not None:
            results.append(insight)
    followup_insight = _detect_followup_timing(conn)
    if followup_insight is not None:
        results.append(followup_insight)

    for insight in results:
        _persist_with_history(conn, insight)

    return learning_db.list_insights(conn)


def _persist_with_history(conn: SupabaseConn, insight: LearningInsight) -> None:
    previous = learning_db.get_insight(conn, insight.pattern_key)
    if previous is not None:
        insight = insight.model_copy(update={"first_observed_at": previous.first_observed_at})
        _record_transition(conn, previous, insight)
    else:
        insight = insight.model_copy(update={"first_observed_at": insight.last_updated_at})
        if insight.status == "active":
            learning_db.add_learning_event(conn, LearningEvent(
                event_id=f"{insight.pattern_key}-{int(utcnow().timestamp() * 1000)}",
                pattern_key=insight.pattern_key, event_type="insight_detected",
                detail={"evidence_count": insight.evidence_count, "confidence": insight.confidence},
            ))
    learning_db.upsert_insight(conn, insight)


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _record_transition(conn: SupabaseConn, previous: LearningInsight, current: LearningInsight) -> None:
    now_ms = int(utcnow().timestamp() * 1000)
    if previous.status != "active" and current.status == "active":
        learning_db.add_learning_event(conn, LearningEvent(
            event_id=f"{current.pattern_key}-{now_ms}-detected",
            pattern_key=current.pattern_key, event_type="insight_detected",
            detail={"evidence_count": current.evidence_count, "confidence": current.confidence},
        ))
        return
    if previous.status == "active" and current.status != "active":
        learning_db.add_learning_event(conn, LearningEvent(
            event_id=f"{current.pattern_key}-{now_ms}-retired",
            pattern_key=current.pattern_key, event_type="insight_retired",
            detail={"reason": "evidence no longer meets confidence gate"},
        ))
        return
    if previous.status != "active" or current.status != "active":
        return
    prev_rank = _CONFIDENCE_RANK.get(previous.confidence, 0)
    curr_rank = _CONFIDENCE_RANK.get(current.confidence, 0)
    if curr_rank > prev_rank:
        learning_db.add_learning_event(conn, LearningEvent(
            event_id=f"{current.pattern_key}-{now_ms}-up",
            pattern_key=current.pattern_key, event_type="confidence_increased",
            detail={"from": previous.confidence, "to": current.confidence, "evidence_count": current.evidence_count},
        ))
    elif curr_rank < prev_rank:
        learning_db.add_learning_event(conn, LearningEvent(
            event_id=f"{current.pattern_key}-{now_ms}-down",
            pattern_key=current.pattern_key, event_type="confidence_decreased",
            detail={"from": previous.confidence, "to": current.confidence, "evidence_count": current.evidence_count},
        ))
