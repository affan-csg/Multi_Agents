"""Builds the short, structured guidance blurb injected into draft/revise
prompts (see personalize.py's `learning_context` param and
conversation/responder.py's equivalent). Never hard-codes a specific
sentence -- always generated from the live learning_insights rows' labels
and evidence counts, per rl_prompt.md section 6: "Do NOT hard-code
individual learned sentences into the email generator. Use structured
learning/context instead."
"""

from __future__ import annotations

from ..db import SupabaseConn
from . import db as learning_db
from .engine import recompute_insights

_HEADER = (
    "Based on prior outreach performance, the following patterns have been "
    "observed (use as guidance, not hard rules):"
)


def get_learning_context_summary(conn: SupabaseConn, *, max_insights: int = 5) -> str | None:
    """Returns a short bullet-list guidance blurb built from only
    status='active' insights, sorted by confidence then evidence_count, or
    None when no qualifying insight exists (draft generation then proceeds
    completely unmodified)."""
    recompute_insights(conn)
    insights = learning_db.list_insights(conn, status="active")
    if not insights:
        return None

    rank = {"high": 2, "medium": 1, "low": 0}
    insights = sorted(insights, key=lambda i: (rank.get(i.confidence, 0), i.evidence_count), reverse=True)[:max_insights]

    lines = [_HEADER]
    for insight in insights:
        lines.append(f"- {insight.description} (evidence: {insight.evidence_count} emails)")
    return "\n".join(lines)
