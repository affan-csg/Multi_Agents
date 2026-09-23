"""Campaign-Level Optimization -- reads outcomes, never owns them.

This package extends the Feedback Learning Agent's evidence-gated approach
(see ../learning/__init__.py) from single-edit diffs up to whole-campaign and
strategy-level comparisons: ICP/company type, email style, subject/opening
style, CTA style, length, personalization level, and follow-up count/timing,
compared against reply rate, positive-reply rate, booking rate, and no-reply
rate across campaigns. It runs inside the Outreach Agent's own backend
process and against the same Supabase project, reusing its db.py/config.py
and the Learning Agent's draft_versions for style evidence -- it does NOT
modify, subclass, or route through anything in ../learning/; the two packages
are independent siblings that both read the same underlying outcome data.

Module map:

    models.py      StrategyProfile / CampaignOptimizationInsight /
                   RecommendationAdoption Pydantic models
    strategy.py    pure, rule-based extraction of an item's "strategy"
                   attributes from its stored subject/body -- no LLM calls,
                   no DB writes, same observable-only philosophy as
                   ../learning/diffing.py
    db.py          repository functions for the 2 new tables, plus read-only
                   lookups over campaigns/prospects/outreach_items/
                   conversation_messages/draft_versions
    engine.py       the fixed catalog of dimension x segment comparisons +
                   confidence scoring (reuses ../learning/engine.py's
                   thresholds and gating philosophy)
    context.py     builds the short guidance blurb injected into campaign/
                   draft prompts, in the same structured-text shape as
                   ../learning/context.py's blurb
    tracking.py    records recommendation adoption and reports observed
                   before/after performance for an adopting campaign --
                   never claims the recommendation caused the change
    routes.py      read-only (+ one opt-in write: recording an adoption) API
                   surface for the dashboard

Four invariants hold everywhere in this package:

1. Read-only against everything but its own two tables. This layer never
   writes campaigns, prospects, outreach_items, outreach_events,
   conversation_messages, or the Learning Agent's own tables -- it only
   reads existing outcomes/strategy evidence and writes
   campaign_optimization_insights / optimization_recommendation_adoptions.
2. Every call site that uses this package (the campaign-creation guidance
   fetch, and the dashboard routes) wraps it in try/except so a failure here
   can never block drafting, sending, campaign creation, or any existing
   Research/Outreach/Conversation/Learning behavior.
3. No insight is ever labeled with a confidence tier above what its sample
   size and effect size actually support, and no finding is ever computed
   globally when a meaningfully-sized segment (e.g. ICP tier) exists and
   would tell a different story -- segmented findings are preferred whenever
   there is enough data to support them. A pattern with too little evidence
   in a given scope stays 'insufficient_data', not a fabricated 'low'.
4. Every insight is reported as an observed correlation, never a proven
   causal claim -- description text and API responses are worded
   accordingly ("associated with", "observed", never "causes" or
   "improves"). Recommendation-adoption tracking (tracking.py) reports
   before/after performance for the specific campaign(s) that adopted a
   recommendation, explicitly labeled as an observed change, not a
   validated causal effect -- causality is never asserted from this data.
"""

from __future__ import annotations
