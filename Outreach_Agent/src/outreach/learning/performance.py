"""Windowed before/after comparisons and campaign comparisons, computed
on-demand straight from outreach_items/conversation_messages -- no new table
needed, since that data is already the canonical, immutable history once an
item reaches a terminal-ish stage.

Every comparison here explicitly reports `insufficient_data=True` (never a
manufactured 0% or extrapolated number) when the sample size required to
make the comparison meaningful is not met -- see rl_prompt.md section 9:
"If there isn't enough data yet, explicitly say: Not enough data yet. Do not
manufacture improvement." And section 3: never claim causation, only report
an "observed" difference.
"""

from __future__ import annotations

from typing import Any, Optional

from ..db import SupabaseConn
from . import db as learning_db

_METRICS = ("reply_rate", "positive_reply_rate", "booking_rate", "no_reply_rate", "failure_rate")


def _rate(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _metric_value(metric: str, outcomes: dict[str, Any], item_ids: list[str]) -> float:
    total = len(item_ids)
    if total == 0:
        return 0.0
    if metric == "reply_rate":
        n = sum(1 for i in item_ids if outcomes.get(i, {}).get("replied"))
    elif metric == "positive_reply_rate":
        n = sum(1 for i in item_ids if outcomes.get(i, {}).get("positive_reply"))
    elif metric == "booking_rate":
        n = sum(1 for i in item_ids if outcomes.get(i, {}).get("booked"))
    elif metric == "no_reply_rate":
        n = sum(1 for i in item_ids if not outcomes.get(i, {}).get("replied"))
    elif metric == "failure_rate":
        n = sum(1 for i in item_ids if outcomes.get(i, {}).get("failed"))
    else:
        raise ValueError(f"unknown metric: {metric}")
    return _rate(n, total)


def before_after_comparison(conn: SupabaseConn, *, metric: str, window: int = 30) -> dict[str, Any]:
    """Splits sent items into the previous `window` and the latest `window`
    by sent_at order. Returns insufficient_data=True (before/after=None) when
    fewer than 2*window sent items exist -- an explicit, honest empty state
    rather than a comparison built on too little data."""
    if metric not in _METRICS:
        raise ValueError(f"unknown metric: {metric}")

    sent_items = [
        i for i in conn.select("outreach_items", order="sent_at.asc")
        if i.get("sent_at")
    ]
    if len(sent_items) < 2 * window:
        return {"metric": metric, "before": None, "after": None, "change_pct": None, "insufficient_data": True}

    before_ids = [i["item_id"] for i in sent_items[-2 * window:-window]]
    after_ids = [i["item_id"] for i in sent_items[-window:]]
    outcomes = learning_db.outreach_outcome_summary(conn, before_ids + after_ids)

    before_value = _metric_value(metric, outcomes, before_ids)
    after_value = _metric_value(metric, outcomes, after_ids)
    change_pct = None
    if before_value:
        change_pct = round(100.0 * (after_value - before_value) / before_value, 1)

    return {
        "metric": metric,
        "before": {"value": before_value, "n": len(before_ids)},
        "after": {"value": after_value, "n": len(after_ids)},
        "change_pct": change_pct,
        "insufficient_data": False,
    }


def learning_era_comparison(conn: SupabaseConn, *, metric: str) -> dict[str, Any]:
    """"Before learning vs after learning": splits sent items by whether any
    draft_versions row existed for that campaign before the item's send --
    i.e. whether the learning layer had any evidence to draw on yet.
    Same insufficient-data guard as before_after_comparison."""
    if metric not in _METRICS:
        raise ValueError(f"unknown metric: {metric}")

    versions = learning_db.list_draft_versions(conn)
    if not versions:
        return {"metric": metric, "before": None, "after": None, "change_pct": None, "insufficient_data": True}
    earliest_learning_at = min(v.created_at for v in versions if v.created_at)

    sent_items = [i for i in conn.select("outreach_items") if i.get("sent_at")]
    before_ids = [i["item_id"] for i in sent_items if i["sent_at"] < earliest_learning_at.isoformat()]
    after_ids = [i["item_id"] for i in sent_items if i["sent_at"] >= earliest_learning_at.isoformat()]

    min_window = 30
    if len(before_ids) < min_window or len(after_ids) < min_window:
        return {"metric": metric, "before": None, "after": None, "change_pct": None, "insufficient_data": True}

    outcomes = learning_db.outreach_outcome_summary(conn, before_ids + after_ids)
    before_value = _metric_value(metric, outcomes, before_ids)
    after_value = _metric_value(metric, outcomes, after_ids)
    change_pct = round(100.0 * (after_value - before_value) / before_value, 1) if before_value else None

    return {
        "metric": metric,
        "before": {"value": before_value, "n": len(before_ids)},
        "after": {"value": after_value, "n": len(after_ids)},
        "change_pct": change_pct,
        "insufficient_data": False,
    }


def campaign_comparison(conn: SupabaseConn, campaign_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Per-campaign reply/positive-reply/booking/no-reply rates, computed
    straight from outreach_items/conversation_messages."""
    campaigns = conn.select("campaigns")
    if campaign_ids:
        wanted = set(campaign_ids)
        campaigns = [c for c in campaigns if c["campaign_id"] in wanted]

    results = []
    for c in campaigns:
        items = [
            i for i in conn.select("outreach_items", campaign_id=f"eq.{c['campaign_id']}")
            if i.get("sent_at")
        ]
        if not items:
            continue
        item_ids = [i["item_id"] for i in items]
        outcomes = learning_db.outreach_outcome_summary(conn, item_ids)
        results.append({
            "campaign_id": c["campaign_id"],
            "name": c.get("name"),
            "sent": len(items),
            "reply_rate": _metric_value("reply_rate", outcomes, item_ids),
            "positive_reply_rate": _metric_value("positive_reply_rate", outcomes, item_ids),
            "booking_rate": _metric_value("booking_rate", outcomes, item_ids),
            "no_reply_rate": _metric_value("no_reply_rate", outcomes, item_ids),
            "failure_rate": _metric_value("failure_rate", outcomes, item_ids),
        })
    return results


def followup_performance(conn: SupabaseConn) -> dict[str, Any]:
    """Reply rate by followup_count bucket (0, 1, 2+)."""
    items = [i for i in conn.select("outreach_items") if i.get("sent_at")]
    if not items:
        return {"buckets": [], "insufficient_data": True}

    item_ids = [i["item_id"] for i in items]
    outcomes = learning_db.outreach_outcome_summary(conn, item_ids)

    buckets: dict[str, list[str]] = {"0": [], "1": [], "2+": []}
    for i in items:
        count = i.get("followup_count") or 0
        key = "2+" if count >= 2 else str(count)
        buckets[key].append(i["item_id"])

    out = []
    for key in ("0", "1", "2+"):
        ids = buckets[key]
        out.append({
            "followup_count": key,
            "n": len(ids),
            "reply_rate": _metric_value("reply_rate", outcomes, ids),
        })
    return {"buckets": out, "insufficient_data": False}
