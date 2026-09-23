-- Migration 006 -- Campaign-Level Optimization layer
--
-- Run this ONCE in the Outreach project's Supabase SQL editor if that project
-- was provisioned before this migration existed. A freshly provisioned
-- project does not need this file -- schema.sql already has everything below.
-- Safe to run more than once (IF NOT EXISTS).
--
-- Purely additive, same contract as migration 005 (the Feedback Learning
-- Agent): no existing table is altered, no existing column is dropped,
-- renamed, or retyped, and nothing outside src/outreach/optimization/ writes
-- these tables. This layer extends the Learning Agent's evidence-gating
-- philosophy (see learning/engine.py's compute_confidence) from per-edit
-- diffs up to campaign/strategy-level comparisons -- it does NOT replace or
-- modify anything in src/outreach/learning/.
--
-- Reversibility: both tables below have foreign keys pointing OUT into
-- existing tables (campaigns) -- never the reverse. No existing table has a
-- column referencing these, so `DROP TABLE campaign_optimization_insights,
-- optimization_recommendation_adoptions;` is always a clean, isolated
-- removal.
--
-- See src/outreach/optimization/__init__.py for the package this schema backs.

-- ---------------------------------------------------------------------------
-- campaign_optimization_insights: current state of every detected
-- campaign/strategy-level pattern -- one row per pattern_key, upserted on
-- recompute, same shape/spirit as learning_insights but scoped to
-- campaign-level dimensions (ICP segment, email style, subject/opening
-- style, CTA style, length bucket, personalization level, follow-up
-- count) rather than single-edit diffs.
--
-- pattern_key is a stable, structured value combining the dimension and the
-- segment it was computed within, e.g. 'cta_style:direct-vs-soft' (global)
-- or 'cta_style:direct-vs-soft@tier=A' (segmented) -- every insight the
-- dashboard can ever show is enumerable from
-- src/outreach/optimization/engine.py's dimension catalog, not
-- auto-discovered free text.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_optimization_insights (
    pattern_key      TEXT PRIMARY KEY,
    pattern_label    TEXT NOT NULL,   -- human-readable, e.g. "Direct vs soft CTA style"
    dimension        TEXT NOT NULL,   -- 'email_style' | 'subject_style' | 'opening_style' | 'cta_style' | 'length_bucket' | 'personalization_level' | 'followup_count' | 'followup_timing'
    -- Segment this insight was computed within -- NULL/'' means computed
    -- across ALL campaigns (no ICP split). Non-null values look like
    -- 'tier=A' or 'industry=SaaS' -- see engine.py's SEGMENT_DIMENSIONS.
    segment          TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL,   -- one-sentence plain-English statement of the observed pattern
    evidence_count   INT NOT NULL DEFAULT 0,
    -- e.g. {"group_a": {"label": "Direct CTA", "reply_rate": 12.4,
    --       "positive_reply_rate": 5.1, "booking_rate": 2.0, "n": 40},
    --       "group_b": {...}, "primary_metric": "positive_reply_rate",
    --       "effect_pct_points": 3.1}
    metric_comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence        TEXT NOT NULL DEFAULT 'low',              -- 'low' | 'medium' | 'high'
    status             TEXT NOT NULL DEFAULT 'insufficient_data', -- 'active' | 'insufficient_data' | 'retired'
    -- Traceability: which campaigns/items contributed evidence.
    -- evidence_item_ids is a capped sample, not necessarily every
    -- contributing item.
    evidence_campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_item_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_observed_at       TIMESTAMPTZ NOT NULL,
    last_updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_status    ON campaign_optimization_insights(status);
CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_dimension ON campaign_optimization_insights(dimension);
CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_segment   ON campaign_optimization_insights(segment);

-- ---------------------------------------------------------------------------
-- optimization_recommendation_adoptions: records that a person marked a
-- specific campaign as "built following recommendation X", purely so
-- before/after performance can later be reported for that campaign relative
-- to the recommendation's suggested direction. This is an explicit, opt-in,
-- human action (see routes.py's POST /optimization/adoptions) -- the
-- optimization layer never auto-tags a campaign as adopting anything.
--
-- Reported comparisons from this table are always labeled as an OBSERVED
-- before/after difference for that one campaign, never as a proven causal
-- effect of the recommendation -- see performance.adoption_outcome.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS optimization_recommendation_adoptions (
    adoption_id   TEXT PRIMARY KEY,
    pattern_key   TEXT NOT NULL,   -- which campaign_optimization_insights row this adoption is against
    campaign_id   TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    note          TEXT,            -- optional free-text note on what was changed
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opt_adoptions_pattern  ON optimization_recommendation_adoptions(pattern_key);
CREATE INDEX IF NOT EXISTS idx_opt_adoptions_campaign ON optimization_recommendation_adoptions(campaign_id);
