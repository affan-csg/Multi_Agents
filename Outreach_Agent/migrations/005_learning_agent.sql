-- Migration 005 -- Feedback Learning / Optimization Agent
--
-- Run this ONCE in the Outreach project's Supabase SQL editor if that
-- project was provisioned before this migration existed. A freshly
-- provisioned project does not need this file -- schema.sql already has
-- everything below. Safe to run more than once (IF NOT EXISTS).
--
-- Purely additive: no existing table is altered, no existing column is
-- dropped, renamed, or retyped, and this feature adds nothing that the
-- Outreach or Conversation Agents read or write. It is written and read
-- exclusively by src/outreach/learning/.
--
-- Reversibility: all three tables below have foreign keys pointing OUT into
-- existing tables (campaigns, outreach_items, conversation_messages) -- never
-- the reverse. No existing table has a column referencing these, so dropping
-- all three (`DROP TABLE draft_versions, learning_insights, learning_events;`)
-- is always a clean, isolated removal with no cascade into anything the rest
-- of the system depends on.
--
-- See src/outreach/learning/__init__.py for the package this schema backs.

-- ---------------------------------------------------------------------------
-- draft_versions: one row per draft-mutating action that has a real "before"
-- to diff against -- i.e. POST /items/{id}/revise, /items/{id}/redraft,
-- /campaigns/{id}/redraft-generic (per item), and the conversation-reply
-- equivalents POST /conversation/messages/{id}/revise and .../redraft. The
-- very first draft at campaign-creation time is NOT stored here -- there is
-- no prior version to diff against yet.
--
-- before_subject/before_body are the item's (or message's) stored
-- subject/body immediately before the mutating call overwrote them --
-- either the original AI draft, or an earlier human edit if this is a
-- second-or-later revision. after_subject/after_body are what the call
-- produced. Both are snapshotted verbatim (not diffed lazily) so evidence
-- shown on an insight detail page is exactly what was seen at the time, even
-- if the diff heuristic (diff_version) changes later.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draft_versions (
    version_id       TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    campaign_id      TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    -- 'revise' | 'redraft' | 'redraft_generic' | 'conversation_revise' | 'conversation_redraft'
    source           TEXT NOT NULL,
    -- Which conversation_messages row this snapshot is for -- only set when
    -- source starts with 'conversation_'. NULL for cold-outreach drafts.
    message_id       TEXT REFERENCES conversation_messages(message_id) ON DELETE CASCADE,
    before_subject   TEXT,
    before_body      TEXT,
    after_subject    TEXT,
    after_body       TEXT,
    instruction      TEXT,  -- the free-text /revise instruction; NULL for redraft (no instruction)

    -- --- precomputed structured diff flags (rl_prompt.md section 2's checklist) ---
    diff_version             INT NOT NULL DEFAULT 1,  -- heuristic version that produced the flags below
    subject_changed          BOOLEAN NOT NULL DEFAULT false,
    opening_changed          BOOLEAN NOT NULL DEFAULT false,  -- first sentence of body materially different
    cta_changed               BOOLEAN NOT NULL DEFAULT false,  -- closing-ask sentence (before the signature) materially different
    cta_shortened              BOOLEAN NOT NULL DEFAULT false,  -- cta_changed AND after CTA is shorter than before
    length_delta_chars          INT,                              -- after body length - before body length
    length_delta_words          INT,
    shortened                    BOOLEAN NOT NULL DEFAULT false,  -- length_delta_words beyond the noise threshold, negative
    lengthened                    BOOLEAN NOT NULL DEFAULT false,
    sentences_removed_count        INT NOT NULL DEFAULT 0,          -- before-sentence count - after-sentence count, floored at 0
    personalization_delta           INT NOT NULL DEFAULT 0,          -- +/- occurrences of prospect-specific tokens (name/company/domain) after minus before
    formatting_changed               BOOLEAN NOT NULL DEFAULT false,  -- line-break/bullet structure differs

    -- --- outcome linkage (denormalized cache, refreshed on each insight recompute) ---
    -- Source of truth stays outreach_items/conversation_messages; these
    -- columns exist only so insight queries don't need to join+aggregate
    -- against them on every request.
    outcome_stage           TEXT,      -- outreach_items.stage as of last recompute
    outcome_replied          BOOLEAN,
    outcome_positive_reply     BOOLEAN,  -- true when intent in ('interested', 'booked')
    outcome_booked              BOOLEAN,

    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_versions_item     ON draft_versions(item_id);
CREATE INDEX IF NOT EXISTS idx_draft_versions_campaign ON draft_versions(campaign_id);
CREATE INDEX IF NOT EXISTS idx_draft_versions_source   ON draft_versions(source);
CREATE INDEX IF NOT EXISTS idx_draft_versions_created  ON draft_versions(created_at);

-- ---------------------------------------------------------------------------
-- learning_insights: current state of every detected pattern. One row per
-- pattern_key, upserted on recompute -- NOT append-only. History of how a
-- pattern's confidence changed over time lives in learning_events, not here.
--
-- pattern_key is a stable, fixed catalog value (e.g. 'shorter_cta',
-- 'shorter_body', 'human_edit_shorten_cta') defined in
-- src/outreach/learning/engine.py's PATTERN_DETECTORS -- every insight the
-- dashboard can ever show is enumerable, not auto-discovered.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_insights (
    pattern_key      TEXT PRIMARY KEY,
    pattern_label    TEXT NOT NULL,   -- human-readable, e.g. "Shorter CTAs"
    category         TEXT NOT NULL,   -- 'cta' | 'length' | 'personalization' | 'opening' | 'subject' | 'followup' | 'human_edit_pattern'
    description      TEXT NOT NULL,   -- one-sentence plain-English statement of the observed pattern
    evidence_count   INT NOT NULL DEFAULT 0,
    -- e.g. {"group_a": {"label": "Short CTA", "reply_rate": 8.4, "n": 24},
    --       "group_b": {"label": "Long CTA",  "reply_rate": 4.9, "n": 23}}
    metric_comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence        TEXT NOT NULL DEFAULT 'low',              -- 'low' | 'medium' | 'high'
    status             TEXT NOT NULL DEFAULT 'insufficient_data', -- 'active' | 'insufficient_data' | 'retired'
    -- Traceability (rl_prompt.md section 15): which campaigns/items
    -- contributed evidence. evidence_item_ids is a capped sample, not
    -- necessarily every contributing item.
    evidence_campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_item_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_observed_at       TIMESTAMPTZ NOT NULL,
    last_updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_insights_status   ON learning_insights(status);
CREATE INDEX IF NOT EXISTS idx_learning_insights_category ON learning_insights(category);

-- ---------------------------------------------------------------------------
-- learning_events: append-only timeline, purely for the dashboard's
-- "Learning History" section -- NOT a source of truth for current state
-- (learning_insights is). Written by the insight engine whenever a recompute
-- detects a change worth showing on a timeline (new insight, confidence tier
-- change, status change). Mirrors outreach_events' shape/spirit for a
-- familiar pattern, scoped to the learning layer.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_events (
    event_id    TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    -- 'insight_detected' | 'confidence_increased' | 'confidence_decreased' |
    -- 'insight_retired' | 'human_edit_pattern_confirmed'
    event_type  TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"from": "low", "to": "medium", "evidence_count": 47}
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_events_pattern ON learning_events(pattern_key);
CREATE INDEX IF NOT EXISTS idx_learning_events_created ON learning_events(created_at);
