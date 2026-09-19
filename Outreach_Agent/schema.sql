-- CSG Outreach Agent -- Postgres (Supabase) schema.
-- Run this once in the Supabase SQL Editor (Dashboard -> SQL Editor -> New
-- query -> paste -> Run) to provision the schema before pointing the app at
-- a (new) Supabase project.
--
-- This is a SEPARATE Supabase project from the Research/ICP Agent's. The two
-- are never joined at the database level -- prospects.research_domain and
-- research_run_id are plain reference strings copied at import time, not
-- foreign keys into another database (Postgres can't FK across projects
-- anyway). Everything Outreach knows about a company/contact is a cached
-- snapshot pulled from the Research Agent's API (see src/outreach/
-- research_client.py); if Research re-scores a company, Outreach's copy goes
-- stale until re-imported -- that's fine, since it's only used for
-- personalizing a message at send time, not as a system of record for
-- scoring.
--
-- JSON payloads are JSONB. Timestamps are TIMESTAMPTZ. RLS is left disabled:
-- accessed only via the service_role key from a trusted backend, same as the
-- Research Agent.

-- ---------------------------------------------------------------------------
-- prospects: cached snapshot of a qualified company + its best contact,
-- imported from the Research Agent's API. Keyed by domain, same identity the
-- Research Agent uses -- there is no separate Outreach-side company ID.
-- Re-importing overwrites the snapshot (see db.upsert_prospect); it never
-- writes back to Research.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prospects (
    research_domain     TEXT PRIMARY KEY,       -- = companies.domain in the Research DB
    research_run_id     TEXT,                   -- which Research run this snapshot came from
    name                TEXT,
    tier                TEXT,
    score                REAL,
    industry            TEXT,
    region              TEXT,
    description         TEXT,
    pain_summary        TEXT,                   -- from Research's qualify step, if present
    recommended_angle   TEXT,                   -- from Research's qualify step, if present
    open_reqs           JSONB,                  -- sample of open reqs, for personalization context
    -- Best contact only (email required by Research's own outreach_ready
    -- gate -- see Research_Agent/src/csg/models.py Prospect.compute_outreach_flags).
    -- If Research later returns multiple contacts worth messaging separately,
    -- this becomes a child table; one row is enough for v1.
    contact_email       TEXT,
    contact_name        TEXT,
    contact_position    TEXT,
    contact_linkedin_url TEXT,
    imported_at         TIMESTAMPTZ NOT NULL,
    raw_prospect        JSONB                   -- full Prospect payload as imported, for audit/debug
);

CREATE INDEX IF NOT EXISTS idx_prospects_tier ON prospects(tier);
CREATE INDEX IF NOT EXISTS idx_prospects_run   ON prospects(research_run_id);

-- ---------------------------------------------------------------------------
-- campaigns: one batch of selected top-N prospects targeted with a given
-- template/channel. A campaign is the unit "select top prospects" produces;
-- outreach_items are the per-prospect rows it creates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id   TEXT PRIMARY KEY,
    name          TEXT,
    research_run_id TEXT,                       -- which Research run prospects were pulled from
    channel       TEXT NOT NULL DEFAULT 'email',
    selection     JSONB,                        -- the selection rule actually applied (tiers, limit, min_score...)
    created_at    TIMESTAMPTZ NOT NULL,
    notes         TEXT
);

-- ---------------------------------------------------------------------------
-- outreach_items: the actual pipeline unit -- one row per (prospect,
-- campaign). `stage` is the 5-stage pipeline. This agent only ever writes
-- pending -> sent (and failed, on a send error); waiting/replied/booked are
-- advanced by a person in the UI today and by a future reply-tracking agent
-- later -- see stage_history for who/what moved it and when.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_items (
    item_id            TEXT PRIMARY KEY,
    campaign_id        TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    research_domain    TEXT NOT NULL REFERENCES prospects(research_domain) ON DELETE CASCADE,
    stage              TEXT NOT NULL DEFAULT 'pending',  -- needs_review|pending|sent|waiting|replied|booked|failed
    channel            TEXT NOT NULL DEFAULT 'email',
    subject            TEXT,
    body               TEXT,
    personalized_from  TEXT,                    -- 'pain_summary' | 'recommended_angle' | 'template_only'
    to_email            TEXT,
    provider_message_id TEXT,                   -- e.g. SMTP/provider's Message-ID, for delivery lookups
    error               TEXT,                    -- last send error, if failed
    scheduled_at        TIMESTAMPTZ,              -- when the user wants this sent; informational only today, see cli/api "schedule" -- no background job fires it yet
    created_at          TIMESTAMPTZ NOT NULL,
    sent_at             TIMESTAMPTZ,
    stage_updated_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (campaign_id, research_domain)
);

CREATE INDEX IF NOT EXISTS idx_items_campaign ON outreach_items(campaign_id);
CREATE INDEX IF NOT EXISTS idx_items_domain   ON outreach_items(research_domain);
CREATE INDEX IF NOT EXISTS idx_items_stage    ON outreach_items(stage);

-- ---------------------------------------------------------------------------
-- outreach_events: append-only audit trail per item -- one row per stage
-- transition or delivery event (sent, bounced, manually-marked-replied, ...).
-- Kept separate from outreach_items (which holds only current state) so the
-- history of who/what moved a card is never lost to an overwrite.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_events (
    event_id     TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,   -- 'stage_change' | 'send_attempt' | 'send_failed' | 'delivery_bounced'
    from_stage   TEXT,
    to_stage     TEXT,
    actor        TEXT,            -- 'system' | 'user' | future agent name
    detail       JSONB,
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_item ON outreach_events(item_id);
