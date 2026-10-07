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
    research_run_id     TEXT,                   -- which Research run this snapshot's data currently reflects (most recent import)
    -- Every run_id that has EVER imported this domain, not just the most
    -- recent one -- see db.upsert_prospect/list_prospects. Re-running
    -- discovery for "the same research" always produces a fresh run_id
    -- (Research has no way to resume an old one), so without this, a prospect
    -- re-imported under a new run would silently vanish from a person's view
    -- of the old run on the Prospects page even though it was never deleted.
    research_run_ids    JSONB NOT NULL DEFAULT '[]'::jsonb,
    name                TEXT,
    tier                TEXT,
    score                REAL,
    industry            TEXT,
    region              TEXT,
    description         TEXT,
    pain_summary        TEXT,                   -- from Research's qualify step, if present
    recommended_angle   TEXT,                   -- from Research's qualify step, if present
    open_reqs           JSONB,                  -- sample of open reqs, for personalization context
    -- 'confirmed' | 'unknown' | NULL -- whether an empty open_reqs means a
    -- job board/careers page was actually read and found empty, or every
    -- discovery method failed and the true count is simply unknown. See
    -- Research_Agent's models.HiringSignal and this project's
    -- ImportedProspect.hiring_signal / personalize._render_open_reqs.
    hiring_signal       TEXT,
    -- True when Research's last qualification attempt for this domain FAILED
    -- and nothing has succeeded since (see ingest._to_imported_prospect and
    -- Research_Agent's qualification_attempts table / migrations/004). Lets
    -- the Prospects table show "qualification failed" distinctly from "never
    -- qualified" instead of both reading as a bare pain_summary IS NULL.
    qualification_failed BOOLEAN NOT NULL DEFAULT FALSE,
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
-- campaign). `stage` is the pipeline. This agent only ever writes
-- pending -> sent (and failed, on a send error); waiting/replied/booked/
-- not_interested are advanced by the Conversation Agent (see below) when
-- it's switched on for a campaign, and by a person in the UI otherwise --
-- see outreach_events for who/what moved it and when.
--
-- not_interested (added by migrations/007_conversation_not_interested_stage.sql,
-- shown as "Failed" in the Dashboard) is a DIFFERENT concept from the
-- `failed` value already in this column: `failed` means the Outreach Agent's
-- own send attempt errored (SMTP/delivery failure, the prospect never saw
-- the email); `not_interested` means the email was delivered and the
-- prospect explicitly declined. No CHECK constraint enforces the value set
-- here -- see that migration file for why, and models.Stage (Python) for the
-- authoritative list.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_items (
    item_id            TEXT PRIMARY KEY,
    campaign_id        TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    research_domain    TEXT NOT NULL REFERENCES prospects(research_domain) ON DELETE CASCADE,
    stage              TEXT NOT NULL DEFAULT 'pending',  -- needs_review|pending|sent|waiting|replied|booked|not_interested|failed
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

-- ===========================================================================
-- CONVERSATION AGENT (Agent 3)
--
-- Everything below is ADDITIVE -- it does not alter or re-key any table
-- above. The Conversation Agent runs in the SAME Supabase project as the
-- Outreach Agent (deliberately, unlike the Research/Outreach split) because
-- it operates on the very same outreach_items rows: it owns the stages the
-- Outreach Agent explicitly does not (sent -> waiting -> replied -> booked,
-- plus the terminal not_interested stage added later by migrations/
-- 007_conversation_not_interested_stage.sql), while the Outreach Agent keeps
-- owning needs_review/pending -> sent/failed. Ownership is by STAGE, not by
-- table, which is what keeps the two agents from racing each other -- see
-- src/outreach/conversation/engine.py.
--
-- Re-running this whole file on an existing project is safe: every statement
-- below is IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- conversation_settings: the per-campaign ON/OFF toggle and the agent's
-- behavior knobs. One row per campaign; absence of a row means "never
-- configured", which reads as DISABLED (see conversation/settings.py
-- get_settings) -- the agent is strictly opt-in per campaign and can never
-- start acting on a campaign nobody switched it on for.
--
-- `enabled` OFF must be a pure no-op, not a teardown: toggling it off leaves
-- every row here and in conversation_messages untouched, so turning it back
-- on resumes from the recorded state rather than replaying history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_settings (
    campaign_id         TEXT PRIMARY KEY REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    enabled             BOOLEAN NOT NULL DEFAULT false,  -- master switch; false = agent does nothing at all
    -- Second, independent switch. Even with enabled=true the agent only
    -- DRAFTS replies unless this is also true -- detection, classification,
    -- stage moves and follow-up scheduling all happen, but no reply email
    -- leaves the building without a person clicking send. Deliberately
    -- separate from `enabled` so "let the agent watch the inbox" and "let
    -- the agent talk to prospects unattended" are two different decisions.
    auto_reply          BOOLEAN NOT NULL DEFAULT false,
    followup_after_days INT NOT NULL DEFAULT 2,          -- sent -> waiting once this much silence has passed
    max_followups       INT NOT NULL DEFAULT 2,          -- hard cap on nudges per item, never exceeded
    -- Automatic follow-up sequence (migration 011). Days are measured from the
    -- original outreach_items.sent_at. Follow-ups are always drafted; they are
    -- sent unattended only when enabled AND auto_reply are true.
    followup_days          INT[]   NOT NULL DEFAULT '{7,14}',
    no_response_after_days INT     NOT NULL DEFAULT 21,
    updated_at          TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- conversation_messages: the full two-way history for an item -- every
-- inbound reply read off IMAP and every outbound message (the original
-- outreach, follow-ups, and replies). Append-only; nothing here is ever
-- UPDATEd or DELETEd except by the item's own ON DELETE CASCADE.
--
-- UNIQUE (item_id, provider_message_id) is the single most important
-- constraint in this file: it is what makes reply ingestion idempotent.
-- IMAP has no reliable "already read this" guarantee across sessions
-- (flags can be reset, a mailbox can be re-synced, a tick can crash
-- mid-way), so the agent re-reads freely and lets this constraint reject
-- duplicates instead of trying to track read-state itself. The same
-- physical email seen ten times produces exactly one row, one
-- classification, and one stage transition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id          TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    direction           TEXT NOT NULL,   -- 'inbound' (prospect -> us) | 'outbound' (us -> prospect)
    provider_message_id TEXT,            -- RFC 5322 Message-ID; the dedupe key paired with item_id
    in_reply_to         TEXT,            -- RFC 5322 In-Reply-To, how an inbound mail is threaded back to us
    subject             TEXT,
    body                TEXT,
    from_email          TEXT,
    to_email            TEXT,
    -- Classifier output, inbound rows only: 'interested' | 'booked' |
    -- 'not_interested' | 'out_of_office' | 'bounce' | 'unclear'.
    -- NULL on outbound rows and on inbound rows not yet classified.
    intent              TEXT,
    -- Set on an outbound row that the agent composed but did NOT send
    -- (auto_reply off). A person sends it from the UI, which clears this.
    draft_pending       BOOLEAN NOT NULL DEFAULT false,
    sent_at             TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    UNIQUE (item_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_msgs_item      ON conversation_messages(item_id);
CREATE INDEX IF NOT EXISTS idx_conv_msgs_direction ON conversation_messages(direction);

-- ---------------------------------------------------------------------------
-- outreach_items additions, for the Conversation Agent only. The Outreach
-- Agent never reads or writes these three, so adding them cannot change any
-- existing behavior.
--
-- conversation_locked_at is an advisory lock: the engine claims an item by
-- PATCHing this from NULL to now() and releases it afterwards, so two
-- overlapping ticks can never both act on the same item. A lock older than
-- the staleness window is reclaimable, so a crashed tick cannot strand an
-- item forever.
-- ---------------------------------------------------------------------------
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS followup_count         INT NOT NULL DEFAULT 0;
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS last_inbound_at        TIMESTAMPTZ;
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS conversation_locked_at TIMESTAMPTZ;

-- The mailbox that actually sent the item's first email (stamped at send time;
-- see migrations/014_sender_email.sql). Follow-ups/replies continue from it and
-- Analytics group by it. NULL = sent before sender tracking, not yet backfilled.
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS sender_email TEXT;
CREATE INDEX IF NOT EXISTS idx_items_sender ON outreach_items(sender_email);

-- ===========================================================================
-- FEEDBACK LEARNING / OPTIMIZATION AGENT
--
-- Everything below is ADDITIVE -- it does not alter or re-key any table
-- above, and neither the Outreach Agent nor the Conversation Agent ever
-- reads or writes these three tables. This layer only ever READS
-- outreach_items/outreach_events/conversation_messages; it never writes to
-- them and never changes a stage. See src/outreach/learning/__init__.py.
--
-- Re-running this whole file on an existing project is safe: every statement
-- below is IF NOT EXISTS. See migrations/005_learning_agent.sql for the full
-- rationale behind each table.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS draft_versions (
    version_id       TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    campaign_id      TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    source           TEXT NOT NULL,   -- 'revise' | 'redraft' | 'redraft_generic' | 'conversation_revise' | 'conversation_redraft'
    message_id       TEXT REFERENCES conversation_messages(message_id) ON DELETE CASCADE,
    before_subject   TEXT,
    before_body      TEXT,
    after_subject    TEXT,
    after_body       TEXT,
    instruction      TEXT,
    diff_version             INT NOT NULL DEFAULT 1,
    subject_changed          BOOLEAN NOT NULL DEFAULT false,
    opening_changed          BOOLEAN NOT NULL DEFAULT false,
    cta_changed               BOOLEAN NOT NULL DEFAULT false,
    cta_shortened              BOOLEAN NOT NULL DEFAULT false,
    length_delta_chars          INT,
    length_delta_words          INT,
    shortened                    BOOLEAN NOT NULL DEFAULT false,
    lengthened                    BOOLEAN NOT NULL DEFAULT false,
    sentences_removed_count        INT NOT NULL DEFAULT 0,
    personalization_delta           INT NOT NULL DEFAULT 0,
    formatting_changed               BOOLEAN NOT NULL DEFAULT false,
    outcome_stage           TEXT,
    outcome_replied          BOOLEAN,
    outcome_positive_reply     BOOLEAN,
    outcome_booked              BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_versions_item     ON draft_versions(item_id);
CREATE INDEX IF NOT EXISTS idx_draft_versions_campaign ON draft_versions(campaign_id);
CREATE INDEX IF NOT EXISTS idx_draft_versions_source   ON draft_versions(source);
CREATE INDEX IF NOT EXISTS idx_draft_versions_created  ON draft_versions(created_at);

CREATE TABLE IF NOT EXISTS learning_insights (
    pattern_key      TEXT PRIMARY KEY,
    pattern_label    TEXT NOT NULL,
    category         TEXT NOT NULL,
    description      TEXT NOT NULL,
    evidence_count   INT NOT NULL DEFAULT 0,
    metric_comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence        TEXT NOT NULL DEFAULT 'low',
    status             TEXT NOT NULL DEFAULT 'insufficient_data',
    evidence_campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_item_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_observed_at       TIMESTAMPTZ NOT NULL,
    last_updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_insights_status   ON learning_insights(status);
CREATE INDEX IF NOT EXISTS idx_learning_insights_category ON learning_insights(category);

CREATE TABLE IF NOT EXISTS learning_events (
    event_id    TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learning_events_pattern ON learning_events(pattern_key);
CREATE INDEX IF NOT EXISTS idx_learning_events_created ON learning_events(created_at);

-- ===========================================================================
-- CAMPAIGN-LEVEL OPTIMIZATION LAYER
--
-- Everything below is ADDITIVE -- it does not alter or re-key any table
-- above, and no existing agent (Outreach, Conversation, or the Feedback
-- Learning Agent above) ever reads or writes these two tables. This layer
-- only ever READS campaigns/prospects/outreach_items/conversation_messages
-- and the Learning Agent's own draft_versions (for style evidence); it never
-- writes to any of them and never changes a stage. See
-- src/outreach/optimization/__init__.py.
--
-- Re-running this whole file on an existing project is safe: every statement
-- below is IF NOT EXISTS. See migrations/006_campaign_optimization.sql for
-- the full rationale behind each table.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS campaign_optimization_insights (
    pattern_key      TEXT PRIMARY KEY,
    pattern_label    TEXT NOT NULL,
    dimension        TEXT NOT NULL,
    segment          TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL,
    evidence_count   INT NOT NULL DEFAULT 0,
    metric_comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence        TEXT NOT NULL DEFAULT 'low',
    status             TEXT NOT NULL DEFAULT 'insufficient_data',
    evidence_campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_item_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_observed_at       TIMESTAMPTZ NOT NULL,
    last_updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_status    ON campaign_optimization_insights(status);
CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_dimension ON campaign_optimization_insights(dimension);
CREATE INDEX IF NOT EXISTS idx_campaign_opt_insights_segment   ON campaign_optimization_insights(segment);

CREATE TABLE IF NOT EXISTS optimization_recommendation_adoptions (
    adoption_id   TEXT PRIMARY KEY,
    pattern_key   TEXT NOT NULL,
    campaign_id   TEXT NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opt_adoptions_pattern  ON optimization_recommendation_adoptions(pattern_key);

-- ===========================================================================
-- CALENDLY INTEGRATION
--
-- Additive, same contract as the Feedback Learning / Optimization tables
-- above: no existing table is altered here, only outreach_items.stage is
-- ever written (via db.set_stage, same audit-trail path every other stage
-- move uses) once a booking is matched to an item -- see
-- src/outreach/calendly/sync.py. Re-running this whole file on an existing
-- project is safe: every statement below is IF NOT EXISTS. See
-- migrations/008_calendly_integration.sql for the full rationale.
--
-- calendly_events: one row per (event, invitee) pair as reported by either a
-- REST API sync pull or a webhook delivery -- the raw record of a real
-- Calendly booking, independent of whether it could be matched to a
-- pipeline item. event_uri is Calendly's own resource identifier and the
-- idempotency key: a sync re-run or a webhook re-delivery of the same event
-- updates this one row (e.g. status flipping active -> canceled) rather than
-- creating a duplicate -- see calendly/db.py's upsert_event.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS calendly_events (
    event_uri        TEXT PRIMARY KEY,
    event_uuid        TEXT NOT NULL,
    invitee_uri       TEXT,
    invitee_email     TEXT,
    invitee_name      TEXT,
    status            TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'canceled'
    event_type_name   TEXT,
    starts_at         TIMESTAMPTZ,
    ends_at           TIMESTAMPTZ,
    location          TEXT,   -- join URL / meeting location, when Calendly provides one
    cancel_reason     TEXT,
    source            TEXT NOT NULL DEFAULT 'sync',    -- 'sync' | 'webhook' -- which path most recently wrote this row
    -- Matching outcome against outreach_items, by invitee email vs to_email
    -- (see calendly/sync.py's match_item) -- both NULL when no item's
    -- to_email matches this booking's invitee at all. ON DELETE SET NULL
    -- (not CASCADE): deleting a campaign/item must not destroy the record
    -- that a real Calendly booking happened, only un-link it -- it then
    -- reappears as an unmatched entry on the Bookings page rather than
    -- vanishing.
    item_id           TEXT REFERENCES outreach_items(item_id) ON DELETE SET NULL,
    research_domain   TEXT REFERENCES prospects(research_domain) ON DELETE SET NULL,
    raw               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    -- Set via POST/DELETE /invitee_no_shows (calendly/client.py's
    -- mark_no_show/unmark_no_show) -- an annotation on a past event, not a
    -- booking-status transition, so it never touches item_id's pipeline
    -- stage the way status=canceled does.
    no_show           BOOLEAN NOT NULL DEFAULT false,
    no_show_uri       TEXT
);

CREATE INDEX IF NOT EXISTS idx_calendly_events_item   ON calendly_events(item_id);
CREATE INDEX IF NOT EXISTS idx_calendly_events_email  ON calendly_events(invitee_email);
CREATE INDEX IF NOT EXISTS idx_calendly_events_status ON calendly_events(status);
CREATE INDEX IF NOT EXISTS idx_opt_adoptions_campaign ON optimization_recommendation_adoptions(campaign_id);

-- Idempotency ledger: one row per (item, follow-up number), ever. The UNIQUE
-- constraint is what makes "send follow-up #1" happen at most once across
-- overlapping ticks, restarts and replicas.
CREATE TABLE IF NOT EXISTS followup_attempts (
    attempt_id          TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    followup_number     INT NOT NULL,
    status              TEXT NOT NULL,        -- claimed | sent | drafted | failed | skipped
    attempts            INT NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    message_id          TEXT,
    error               TEXT,
    claimed_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    UNIQUE (item_id, followup_number)
);
CREATE INDEX IF NOT EXISTS idx_followup_attempts_item ON followup_attempts(item_id);

-- Scheduler state: one row per job ('sync'). Timers live here, not in process
-- memory, so a restart or redeploy resumes the schedule instead of resetting it.
CREATE TABLE IF NOT EXISTS automation_state (
    job                      TEXT PRIMARY KEY,
    interval_minutes         INT NOT NULL DEFAULT 30,
    enabled                  BOOLEAN NOT NULL DEFAULT true,  -- Automatic Sync toggle (migration 012)
    locked_by                TEXT,
    locked_until             TIMESTAMPTZ,
    last_started_at          TIMESTAMPTZ,
    last_success_at          TIMESTAMPTZ,
    next_run_at              TIMESTAMPTZ,
    last_inbox_success_at    TIMESTAMPTZ,
    last_calendly_success_at TIMESTAMPTZ,
    last_error               TEXT,
    consecutive_failures     INT NOT NULL DEFAULT 0,
    last_result              JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at               TIMESTAMPTZ NOT NULL
);
