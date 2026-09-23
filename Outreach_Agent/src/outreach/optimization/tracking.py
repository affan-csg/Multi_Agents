"""Recommendation-adoption outcome tracking: reports observed before/after
performance for a campaign that a person explicitly marked as having adopted
a given recommendation (see routes.py's POST /optimization/adoptions).

This module NEVER claims the recommendation caused any observed change. It
reports exactly two numbers -- the adopting campaign's own outcome rates,
and the rates of comparable campaigns that did NOT adopt that recommendation
-- side by side, explicitly labeled as an observed difference. A real causal
claim would require a randomized assignment of strategy to campaigns, which
this system does not and cannot perform (campaigns are chosen by whoever
creates them, not by the optimization layer) -- see __init__.py invariant 4
and rl_prompt.md's original "do not claim causation" principle, extended
here from single-insight descriptions to adoption reporting.
"""

from __future__ import annotations

from typing import Any, Optional

from ..db import SupabaseConn
from . import db as opt_db
from .engine import _group_result

_MIN_WINDOW_FOR_COMPARISON = 10


def adoption_outcome(conn: SupabaseConn, pattern_key: str) -> dict[str, Any]:
    """For every campaign that recorded an adoption of `pattern_key`,
    compares that campaign's own sent-item outcome rates against the rates
    of all OTHER campaigns' sent items over the same period. Returns
    insufficient_data=True (same explicit-empty-state contract as
    ../learning/performance.py) when either side has too few sent items to
    make the comparison meaningful.

    Always returns `causality_note` reiterating that this is an observed
    association for the specific adopting campaign(s), not a controlled
    experiment -- other differences between the adopting and non-adopting
    campaigns (timing, ICP mix, sender, etc.) are not accounted for."""
    adoptions = opt_db.list_adoptions(conn, pattern_key=pattern_key)
    if not adoptions:
        return {"pattern_key": pattern_key, "insufficient_data": True, "reason": "no campaigns have recorded adopting this recommendation yet"}

    adopting_campaign_ids = {a.campaign_id for a in adoptions}
    all_items = opt_db.list_sent_items_with_context(conn)
    if not all_items:
        return {"pattern_key": pattern_key, "insufficient_data": True, "reason": "not enough sent items yet"}

    adopting_ids = [i["item_id"] for i in all_items if i["campaign_id"] in adopting_campaign_ids]
    other_ids = [i["item_id"] for i in all_items if i["campaign_id"] not in adopting_campaign_ids]

    if len(adopting_ids) < _MIN_WINDOW_FOR_COMPARISON or len(other_ids) < _MIN_WINDOW_FOR_COMPARISON:
        return {
            "pattern_key": pattern_key, "insufficient_data": True,
            "reason": "not enough sent items yet in the adopting campaign(s) or the comparison set",
            "adopting_campaign_count": len(adopting_campaign_ids),
        }

    outcomes = opt_db.outcome_summary(conn, adopting_ids + other_ids)
    adopting = _group_result("Adopting campaign(s)", adopting_ids, outcomes)
    other = _group_result("All other campaigns", other_ids, outcomes)

    return {
        "pattern_key": pattern_key,
        "insufficient_data": False,
        "adopting_campaign_ids": sorted(adopting_campaign_ids),
        "adopting": {
            "n": adopting.n, "reply_rate": adopting.reply_rate,
            "positive_reply_rate": adopting.positive_reply_rate,
            "booking_rate": adopting.booking_rate, "no_reply_rate": adopting.no_reply_rate,
            "failure_rate": adopting.failure_rate,
        },
        "comparison": {
            "n": other.n, "reply_rate": other.reply_rate,
            "positive_reply_rate": other.positive_reply_rate,
            "booking_rate": other.booking_rate, "no_reply_rate": other.no_reply_rate,
            "failure_rate": other.failure_rate,
        },
        "causality_note": (
            "This compares observed outcome rates for the campaign(s) that adopted this "
            "recommendation against all other campaigns over the same data. It is not a "
            "controlled experiment: other differences between these campaigns (timing, ICP "
            "mix, sender, follow-up cadence, etc.) are not accounted for, so this cannot be "
            "read as proof the recommendation caused any difference shown here."
        ),
    }
