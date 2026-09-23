"""Builds the short, structured guidance blurb injected into campaign
creation / draft prompts, in the same shape and spirit as
../learning/context.py's blurb. Never hard-codes a specific sentence --
always generated from the live campaign_optimization_insights rows'
descriptions and evidence counts.

This is deliberately a SEPARATE blurb from ../learning/context.py's, not a
merge of the two -- the Learning Agent's guidance is about how a human
tends to edit a draft; this one is about which campaign-level strategy has
performed better historically. Both are appended independently wherever a
caller chooses to fetch them (see personalize.py's _append_learning_context,
which is deliberately reused as-is here since the blurb shape is identical:
a header line plus bullet points, appended before the prompt's final
instructions).
"""

from __future__ import annotations

from ..db import SupabaseConn
from . import db as opt_db
from .engine import recompute_insights

_HEADER = (
    "Based on prior campaign performance (observed correlations, not proven "
    "causes), the following strategy patterns have been associated with "
    "different outcome rates -- use as guidance, not hard rules:"
)


def get_optimization_context_summary(
    conn: SupabaseConn, *, segment: str = "", max_insights: int = 5,
) -> str | None:
    """Returns a short bullet-list guidance blurb built from only
    status='active' insights (optionally scoped to one ICP segment, e.g.
    'tier=A', falling back to global insights when no segment is given or
    none qualify for it), sorted by confidence then evidence_count, or None
    when no qualifying insight exists (campaign creation then proceeds
    completely unmodified -- same no-op contract as
    ../learning/context.get_learning_context_summary)."""
    recompute_insights(conn)
    insights = opt_db.list_insights(conn, status="active", segment=segment) if segment else []
    if not insights:
        insights = opt_db.list_insights(conn, status="active", segment="")
    if not insights:
        return None

    rank = {"high": 2, "medium": 1, "low": 0}
    insights = sorted(insights, key=lambda i: (rank.get(i.confidence, 0), i.evidence_count), reverse=True)[:max_insights]

    lines = [_HEADER]
    for insight in insights:
        lines.append(f"- {insight.description} (evidence: {insight.evidence_count} emails)")
    return "\n".join(lines)


def fetch_optimization_context_safely(conn: SupabaseConn, *, segment: str = "") -> str | None:
    """Convenience wrapper for call sites outside this package: fetches the
    optimization guidance blurb, swallowing any failure and returning None
    instead -- the same try/except-with-deferred-import contract every call
    site in api.py/conversation/routes.py/ingest.py already applies manually
    to ../learning/context.get_learning_context_summary. Using this wrapper
    keeps that contract in one place instead of re-implementing the
    try/except at every call site."""
    try:
        return get_optimization_context_summary(conn, segment=segment)
    except Exception:
        return None
