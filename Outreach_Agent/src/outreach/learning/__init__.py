"""Feedback Learning / Optimization Agent -- reads outcomes, never owns them.

This package turns AI-draft-vs-human-edit history and existing outcome data
(replies, bookings, no-replies -- all already tracked by outreach_items /
outreach_events / conversation_messages) into explainable, evidence-gated
"learned insights" that can optionally inform future drafts. It runs inside
the Outreach Agent's own backend process and against the same Supabase
project, reusing its db.py/config.py/models.py rather than duplicating them --
same approach as conversation/, and, like that package, self-contained:
deleting this directory and its one include_router() line in api.py would
leave the rest of the system exactly as it was before this agent existed.

Module map:

    models.py      DraftVersion / LearningInsight / LearningEvent Pydantic models
    diffing.py     pure, rule-based text diffing -- no LLM calls, no DB
    db.py          repository functions for the 3 new tables, plus read-only
                   outcome lookups over outreach_items/conversation_messages
    engine.py      the fixed catalog of pattern detectors + confidence scoring
    performance.py before/after and campaign-comparison metrics
    context.py     builds the short guidance blurb injected into draft prompts
    routes.py      read-only API surface for the dashboard

Three invariants hold everywhere in this package:

1. Read-only against everything but its own three tables. This agent never
   writes outreach_items, outreach_events, or conversation_messages, never
   changes a stage, and never sends or receives email -- it only reads
   existing outcomes and writes draft_versions/learning_insights/
   learning_events.
2. Every call site that uses this package (the revise/redraft hooks in
   api.py and conversation/routes.py, and the learning_context fetch in
   personalize.py's callers) wraps it in try/except so a failure here can
   never block drafting, revising, redrafting, sending, or conversation
   behavior. See rl_prompt.md section 12.
3. No insight is ever labeled with a confidence tier above what its sample
   size and effect size actually support -- see engine.compute_confidence.
   A pattern with too little evidence stays 'insufficient_data', not a
   fabricated 'low'.
"""

from __future__ import annotations
