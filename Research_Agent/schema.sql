-- CSG Research/ICP Agent -- Postgres (Supabase) schema.
-- Run this once in the Supabase SQL Editor (Dashboard -> SQL Editor ->
-- New query -> paste -> Run) to provision the schema before pointing the
-- app at a (new) Supabase project. Originally SQLite; migrated so the data
-- is reachable from a browser/phone/other machine instead of a local file.
--
-- JSON payloads (dimensions/explanation/metrics/raw_json) are JSONB --
-- native, queryable JSON. Timestamps are TIMESTAMPTZ. RLS is left disabled:
-- this schema is accessed only via the service_role key from a trusted
-- backend script, never from a browser, so table-level RLS policies are not
-- needed here.
--
-- Every other table's `domain` column is a foreign key into companies.domain
-- with ON DELETE CASCADE: if a company row is ever deleted -- through the
-- app or directly in Supabase -- every row referencing it (job postings,
-- scores, qualifications, saved-list entries, contacts, suppression) is
-- deleted automatically by Postgres. This is enforced at the database level
-- specifically so it holds no matter how the deletion happens, not just
-- through whatever cleanup code a specific API endpoint remembers to run.

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    label       TEXT,             -- optional human-given name, e.g. "CSG research"
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    profile     TEXT,
    seed_count  INTEGER,
    notes       TEXT
);

-- Researched facts (employee_count/revenue/funding) are JSONB provenance
-- envelopes, never bare scalars -- see models.ProvenanceValue:
--   {"value": 250, "source": "gemini_grounded_search", "url": "...",
--    "fetched_at": "2026-09-16T...", "method": "estimated"|"reported"|"unknown",
--    "confidence": "high"|"medium"|"low"}
-- `method` is what keeps an LLM estimate distinguishable from a reported
-- figure, which the ICP brief requires for every financial number.
CREATE TABLE IF NOT EXISTS companies (
    domain          TEXT PRIMARY KEY,
    name            TEXT,
    website         JSONB,
    headquarters    JSONB,
    company_type    JSONB,
    employee_count  JSONB,         -- provenance envelope (see above)
    revenue         JSONB,         -- provenance envelope (see above)
    funding         JSONB,         -- provenance envelope (see above)
    classification_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    research_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    research_outcomes JSONB NOT NULL DEFAULT '{}'::jsonb,
    description     TEXT,
    region          TEXT,          -- discovery region, e.g. 'GA', 'East Coast'
    industry        TEXT,
    hq_state        TEXT,
    ats_provider    TEXT,
    ats_token       TEXT,
    ats_detected_by TEXT,          -- 'careers_page' | 'token_probe' | NULL
    careers_url     TEXT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_checked    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'discovered',
    dq_reason       TEXT
);

CREATE TABLE IF NOT EXISTS discovery_sources (
    domain        TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    provider_name TEXT NOT NULL,
    url           TEXT,
    discovered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, provider_name)
);

CREATE TABLE IF NOT EXISTS job_postings (
    domain          TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    ats_provider    TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT,
    location        TEXT,
    department      TEXT,
    employment_type TEXT,
    url             TEXT,
    posted_at       TIMESTAMPTZ,    -- from ATS; NULL when provider omits it
    first_seen      TIMESTAMPTZ NOT NULL, -- our own observation; age fallback
    last_seen       TIMESTAMPTZ NOT NULL,
    description     TEXT,
    raw_json        JSONB,
    delivery_fit    TEXT,           -- 'US' | 'LATAM' | 'Pakistan' | NULL (see delivery.py)
    practice_area   TEXT,           -- key from icp.yaml practice_areas
    PRIMARY KEY (domain, ats_provider, external_id)
);

CREATE INDEX IF NOT EXISTS idx_postings_domain ON job_postings(domain);
-- Indexes on the newer columns live in the migration section at the bottom,
-- after the ALTER TABLE statements that add those columns -- on a database
-- created before them, indexing here would fail with "column does not exist".

CREATE TABLE IF NOT EXISTS scores (
    domain        TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    run_id        TEXT NOT NULL,
    score         REAL NOT NULL,
    tier          TEXT NOT NULL,
    dimensions    JSONB NOT NULL,   -- {dim: points}
    explanation   JSONB NOT NULL,   -- [str]
    metrics       JSONB NOT NULL,   -- raw computed metrics
    scored_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, run_id)
);

CREATE TABLE IF NOT EXISTS suppression (
    domain   TEXT PRIMARY KEY REFERENCES companies(domain) ON DELETE CASCADE,
    reason   TEXT,
    added_at TIMESTAMPTZ NOT NULL
);

-- Step 4: LLM qualification layer. One row per (domain, run_id) -- separate
-- from scores so the deterministic score is never mixed with LLM output, and
-- so qualification can be re-run (different model/prompt) without rescoring.
CREATE TABLE IF NOT EXISTS qualifications (
    domain             TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    pain_summary       TEXT NOT NULL,
    recommended_angle  TEXT NOT NULL,
    model              TEXT NOT NULL,
    qualified_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, run_id)
);

-- Per-(domain, run_id) qualification attempt ledger -- see migrations/004 for
-- the full rationale. `qualifications` above only ever gets a row on success;
-- this table records every attempt, success or failure, so a failed
-- qualification is queryable and retryable instead of looking identical to
-- "never attempted".
CREATE TABLE IF NOT EXISTS qualification_attempts (
    domain             TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    status             TEXT NOT NULL,  -- 'failed' | 'succeeded'
    error_reason       TEXT,           -- classify_error() bucket; NULL when status='succeeded'
    error_detail       TEXT,           -- human-readable exception text for the last attempt
    attempt_count      INT NOT NULL DEFAULT 1,
    last_attempted_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, run_id)
);

-- Saved companies ("saved lists" in the UI). Keyed by domain only, not
-- run_id: a save is a human judgement about the company itself, so it
-- outlives the run that surfaced it and stays valid across re-scores.
-- saved_from_run records which run prompted the save, for provenance.
CREATE TABLE IF NOT EXISTS saved_companies (
    domain        TEXT PRIMARY KEY REFERENCES companies(domain) ON DELETE CASCADE,
    saved_from_run TEXT,
    note          TEXT,
    saved_at      TIMESTAMPTZ NOT NULL
);

-- Contact discovery (Hunter.io). Keyed by domain only, not run_id -- a
-- person's email doesn't change between scoring runs, and Hunter's free tier
-- is a hard 25 searches/MONTH, so a domain is looked up at most once ever
-- unless explicitly forced.
CREATE TABLE IF NOT EXISTS contacts (
    domain        TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    position      TEXT,
    seniority     TEXT,
    department    TEXT,
    linkedin_url  TEXT,
    confidence    INTEGER,          -- Hunter's 0-100 match confidence
    verification  TEXT,             -- Hunter's email-deliverability status
    target_decision_maker_type TEXT,
    source        TEXT NOT NULL,    -- 'hunter'
    found_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, email)
);


-- ---------------------------------------------------------------------------
-- Upgrade path for databases created before the autonomous-discovery work.
--
-- The CREATE TABLE statements above are all IF NOT EXISTS, so on an existing
-- database they are skipped entirely -- which means columns added later would
-- never appear. These ALTERs close that gap, so this file is safe to run
-- against both a brand-new and an existing database.
--
-- Identical to migrations/001_autonomous_discovery.sql; either one is enough.
-- ---------------------------------------------------------------------------

-- Firmographics researched by the enrichment step.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS revenue     JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS funding     JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS classification_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS research_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS research_outcomes JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS region      TEXT;

-- Req classification feeding the CSG Delivery Fit score.
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS delivery_fit  TEXT;
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS practice_area TEXT;

-- employee_count: bare integer -> JSONB provenance envelope. Existing scalars
-- are wrapped and marked 'legacy_import'; already-JSONB columns are untouched.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'companies'
          AND column_name = 'employee_count'
          AND data_type <> 'jsonb'
    ) THEN
        ALTER TABLE companies
            ALTER COLUMN employee_count TYPE JSONB
            USING CASE
                WHEN employee_count IS NULL THEN NULL
                ELSE jsonb_build_object(
                    'value', employee_count,
                    'source', 'legacy_import',
                    'method', 'reported',
                    'confidence', 'medium')
            END;
    END IF;
END $$;

-- Durable background-job snapshots and worker leases for Cloud Run.
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending', done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0, current_domain TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb, result JSONB, error TEXT,
    idempotency_key TEXT UNIQUE, worker_id TEXT, lease_until TIMESTAMPTZ,
    quota_blocked_until TIMESTAMPTZ,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON agent_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_run ON agent_jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_quota_blocked ON agent_jobs(status, quota_blocked_until);

CREATE TABLE IF NOT EXISTS agent_job_checkpoints (
    job_id TEXT NOT NULL REFERENCES agent_jobs(job_id) ON DELETE CASCADE,
    checkpoint_key TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, checkpoint_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_job_checkpoints_job ON agent_job_checkpoints(job_id);

ALTER TABLE agent_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE agent_jobs FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE agent_jobs TO service_role;
ALTER TABLE agent_job_checkpoints ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE agent_job_checkpoints FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE agent_job_checkpoints TO service_role;

CREATE OR REPLACE FUNCTION save_agent_job_checkpoint(
    p_job_id TEXT,
    p_checkpoint_key TEXT,
    p_worker_id TEXT,
    p_status TEXT,
    p_payload JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
        current_worker TEXT;
        current_status TEXT;
        current_lease TIMESTAMPTZ;
BEGIN
        SELECT worker_id, status, lease_until
            INTO current_worker, current_status, current_lease
            FROM public.agent_jobs
         WHERE job_id = p_job_id
         FOR UPDATE;

        IF current_worker IS DISTINCT FROM p_worker_id
             OR current_status <> 'running'
             OR current_lease IS NULL
             OR current_lease <= NOW() THEN
                RETURN FALSE;
        END IF;

    INSERT INTO public.agent_job_checkpoints (job_id, checkpoint_key, status, worker_id, payload, updated_at)
        VALUES (p_job_id, p_checkpoint_key, p_status, p_worker_id,
                        COALESCE(p_payload, '{}'::jsonb), NOW())
    ON CONFLICT (job_id, checkpoint_key) DO UPDATE
       SET status = EXCLUDED.status,
           worker_id = EXCLUDED.worker_id,
           payload = EXCLUDED.payload,
           updated_at = EXCLUDED.updated_at;
    RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION save_agent_job_checkpoint(TEXT, TEXT, TEXT, TEXT, JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION save_agent_job_checkpoint(TEXT, TEXT, TEXT, TEXT, JSONB)
    TO service_role;

-- ---------------------------------------------------------------------------
-- Company-level ICP targeting and evidence (identical to migration 006).
-- Kept separate from job_postings.delivery_fit: these records describe
-- company segments, company hiring-model openness, and research readiness.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company_research_assessments (
    assessment_id              TEXT PRIMARY KEY,
    domain                     TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    assessment_version         INTEGER NOT NULL CHECK (assessment_version > 0),
    schema_version             INTEGER NOT NULL DEFAULT 1,
    source_run_id              TEXT,
    readiness_status           TEXT NOT NULL CHECK (readiness_status IN (
                                   'unverified_pipeline', 'contact_enrichment_pending',
                                   'fully_researched', 'rejected')),
    prospecting_disposition    TEXT NOT NULL CHECK (prospecting_disposition IN (
                                   'eligible', 'needs_review', 'excluded_existing_client', 'rejected')),
    rejection_reason           TEXT,
    category_assessments       JSONB NOT NULL DEFAULT '[]'::jsonb,
    hiring_model_assessments   JSONB NOT NULL DEFAULT '[]'::jsonb,
    criteria_assessments       JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision_makers            JSONB NOT NULL DEFAULT '[]'::jsonb,
    assessed_at                TIMESTAMPTZ NOT NULL,
    UNIQUE (domain, assessment_version)
);

CREATE TABLE IF NOT EXISTS company_evidence (
    evidence_id          TEXT NOT NULL,
    assessment_id        TEXT NOT NULL REFERENCES company_research_assessments(assessment_id) ON DELETE CASCADE,
    domain               TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    criterion            TEXT NOT NULL,
    subject              TEXT,
    claim                JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_status      TEXT NOT NULL CHECK (evidence_status IN ('verified', 'inferred', 'unavailable')),
    confidence           TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source_name          TEXT,
    source_type          TEXT CHECK (source_type IS NULL OR source_type IN (
                            'official_company', 'official_job_board', 'linkedin', 'third_party', 'aggregator', 'other')),
    source_url           TEXT,
    source_published_at  TIMESTAMPTZ,
    retrieved_at         TIMESTAMPTZ,
    excerpt              TEXT,
    conflict_group       TEXT,
    unavailable_reason   TEXT,
    CHECK (
        (evidence_status = 'unavailable' AND unavailable_reason IS NOT NULL)
        OR
        (evidence_status <> 'unavailable' AND source_name IS NOT NULL
         AND source_type IS NOT NULL AND source_url IS NOT NULL)
    ),
    PRIMARY KEY (assessment_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_research_assessments_domain_version
    ON company_research_assessments(domain, assessment_version DESC);
CREATE INDEX IF NOT EXISTS idx_company_evidence_assessment
    ON company_evidence(assessment_id);
CREATE INDEX IF NOT EXISTS idx_company_evidence_domain_criterion
    ON company_evidence(domain, criterion);

-- Indexes last, after every column they reference is guaranteed to exist.
CREATE INDEX IF NOT EXISTS idx_companies_region ON companies(region);
CREATE INDEX IF NOT EXISTS idx_scores_run       ON scores(run_id);

-- API usage/quota tracking (Settings > Usage tab). One row per outbound call
-- to Gemini, Groq, or Tavily -- written best-effort by usage_tracker.py,
-- which never raises into the calling research/qualify/discovery code, so a
-- failure to log a row can never break or slow down an actual run. Not an
-- FK into anything: it must keep recording even for calls made outside any
-- run_id (e.g. a resumed/ad-hoc enrichment) and must survive a run being
-- deleted, since usage history is a cost/ops record, not research output.
CREATE TABLE IF NOT EXISTS api_usage (
    id            BIGSERIAL PRIMARY KEY,
    provider      TEXT NOT NULL,       -- 'gemini' | 'groq' | 'tavily' | 'hunter'
    call_kind     TEXT NOT NULL,       -- e.g. 'discovery_search', 'enrich_firmographics', 'qualify', 'groq_parse', 'tavily_search'
    run_id        TEXT,                -- best-effort context; NULL when not run-scoped
    domain        TEXT,                -- best-effort context; the company this call was for, if any
    success       BOOLEAN NOT NULL,
    status_code   INTEGER,             -- HTTP/API status when known (e.g. 429, 500)
    error_kind    TEXT,                -- short classification: 'quota_exhausted' | 'rate_limited' | 'connection_error' | 'parse_error' | 'other'
    worker_version TEXT,
    error_details JSONB,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    prompt_tokens     INTEGER,         -- Gemini/Groq only; NULL for Tavily (search API, no token usage)
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_usage_provider_time ON api_usage(provider, created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_created_at    ON api_usage(created_at);

CREATE TABLE IF NOT EXISTS research_diagnostic_events (
    event_id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    run_id TEXT,
    job_id TEXT,
    worker_id TEXT,
    worker_version TEXT NOT NULL,
    provider TEXT,
    call_kind TEXT,
    domain TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_diagnostic_events_time
    ON research_diagnostic_events(created_at);
CREATE INDEX IF NOT EXISTS idx_research_diagnostic_events_run
    ON research_diagnostic_events(run_id, created_at);
ALTER TABLE research_diagnostic_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE research_diagnostic_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE research_diagnostic_events TO service_role;

-- Cross-worker adaptive Gemini admission state. Request work remains in
-- agent_jobs; this singleton only coordinates pacing, quota pauses and metrics.
CREATE TABLE IF NOT EXISTS gemini_scheduler_state (
    scheduler_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (scheduler_id = 1),
    current_rpm DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    next_slot_at TIMESTAMPTZ,
    blocked_until TIMESTAMPTZ,
    blocked_reason TEXT,
    successful_calls BIGINT NOT NULL DEFAULT 0,
    rate_limited_calls BIGINT NOT NULL DEFAULT 0,
    quota_blocked_jobs BIGINT NOT NULL DEFAULT 0,
    retry_count BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
ALTER TABLE gemini_scheduler_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE gemini_scheduler_state FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE gemini_scheduler_state TO service_role;

CREATE OR REPLACE FUNCTION reserve_gemini_request_slot()
RETURNS TABLE(wait_seconds DOUBLE PRECISION, current_rpm DOUBLE PRECISION,
              queue_depth BIGINT, blocked_reason TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    state gemini_scheduler_state%ROWTYPE;
    slot_time TIMESTAMPTZ;
BEGIN
    INSERT INTO public.gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
    SELECT * INTO state FROM public.gemini_scheduler_state WHERE scheduler_id = 1 FOR UPDATE;
    IF state.blocked_reason IS NOT NULL AND
       (state.blocked_until IS NULL OR state.blocked_until > NOW()) THEN
        RETURN QUERY SELECT
            (CASE WHEN state.blocked_until IS NULL THEN -1.0
                  ELSE EXTRACT(EPOCH FROM state.blocked_until - NOW()) END)::DOUBLE PRECISION,
            state.current_rpm,
            (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked')),
            state.blocked_reason;
        RETURN;
    END IF;
    IF state.blocked_until IS NOT NULL AND state.blocked_until <= NOW() THEN
        UPDATE public.gemini_scheduler_state
           SET blocked_until = NULL, blocked_reason = NULL, updated_at = NOW()
         WHERE scheduler_id = 1;
    END IF;
    slot_time := GREATEST(COALESCE(state.next_slot_at, NOW()), NOW());
     UPDATE public.gemini_scheduler_state AS scheduler
         SET next_slot_at = slot_time + make_interval(secs => 60.0 / GREATEST(scheduler.current_rpm, 0.1)),
           updated_at = NOW()
     WHERE scheduler_id = 1;
    RETURN QUERY SELECT
        GREATEST(0.0, EXTRACT(EPOCH FROM slot_time - NOW()))::DOUBLE PRECISION,
        state.current_rpm,
        (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked')),
        NULL::TEXT;
END;
$$;

CREATE OR REPLACE FUNCTION record_gemini_request_result(
    p_success BOOLEAN, p_classification TEXT DEFAULT NULL,
    p_retry_after_seconds DOUBLE PRECISION DEFAULT NULL, p_total_tokens BIGINT DEFAULT NULL
) RETURNS TABLE(current_rpm DOUBLE PRECISION, queue_depth BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO public.gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
    IF p_success THEN
        UPDATE public.gemini_scheduler_state AS scheduler
           SET current_rpm = scheduler.current_rpm + 0.05,
               successful_calls = scheduler.successful_calls + 1,
               total_tokens = scheduler.total_tokens + COALESCE(p_total_tokens, 0),
               updated_at = NOW()
         WHERE scheduler_id = 1;
    ELSE
        UPDATE public.gemini_scheduler_state AS scheduler
           SET current_rpm = CASE WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity')
                                  THEN GREATEST(0.1, scheduler.current_rpm / 2.0) ELSE scheduler.current_rpm END,
               rate_limited_calls = scheduler.rate_limited_calls +
                   CASE WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity') THEN 1 ELSE 0 END,
               quota_blocked_jobs = scheduler.quota_blocked_jobs +
                   CASE WHEN p_classification IN ('daily_quota', 'monthly_quota') THEN 1 ELSE 0 END,
               retry_count = scheduler.retry_count + 1,
               blocked_until = CASE
                   WHEN p_classification IN ('daily_quota', 'monthly_quota') AND p_retry_after_seconds IS NULL THEN NULL
                   WHEN p_classification IN ('daily_quota', 'monthly_quota') THEN
                       NOW() + make_interval(secs => GREATEST(0.0, p_retry_after_seconds))
                   WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity') THEN
                       NOW() + make_interval(secs => GREATEST(2.0, COALESCE(p_retry_after_seconds, 2.0)))
                   ELSE scheduler.blocked_until END,
               blocked_reason = CASE
                   WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity', 'daily_quota', 'monthly_quota')
                   THEN p_classification ELSE blocked_reason END,
               updated_at = NOW()
         WHERE scheduler_id = 1;
    END IF;
        RETURN QUERY SELECT state.current_rpm,
        (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked'))
            FROM public.gemini_scheduler_state AS state WHERE scheduler_id = 1;
END;
$$;

CREATE OR REPLACE FUNCTION resume_gemini_quota()
RETURNS TABLE(status TEXT, current_rpm DOUBLE PRECISION)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
        UPDATE public.gemini_scheduler_state AS scheduler
       SET blocked_until = NULL, blocked_reason = NULL,
                     current_rpm = GREATEST(0.1, scheduler.current_rpm / 2.0), updated_at = NOW()
     WHERE scheduler_id = 1;
        RETURN QUERY SELECT 'resumed'::TEXT, scheduler.current_rpm
            FROM public.gemini_scheduler_state AS scheduler WHERE scheduler_id = 1;
END;
$$;

REVOKE ALL ON FUNCTION reserve_gemini_request_slot() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION record_gemini_request_result(BOOLEAN, TEXT, DOUBLE PRECISION, BIGINT)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION resume_gemini_quota() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION reserve_gemini_request_slot() TO service_role;
GRANT EXECUTE ON FUNCTION record_gemini_request_result(BOOLEAN, TEXT, DOUBLE PRECISION, BIGINT)
    TO service_role;
GRANT EXECUTE ON FUNCTION resume_gemini_quota() TO service_role;

-- ---------------------------------------------------------------------------
-- Multi-level job discovery fallback (see jobs_discovery.py). "No ATS
-- detected" must never be indistinguishable from "confirmed zero jobs" --
-- these columns are how that distinction is stored. Identical to
-- migrations/003_job_discovery_fallback.sql; either one is enough.
-- ---------------------------------------------------------------------------
ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_discovery_method TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_source TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS hiring_signal TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_discovery_notes TEXT;
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ats';

-- ---------------------------------------------------------------------------
-- Global company de-duplication (identical to migrations/005_company_dedup.sql)
-- ---------------------------------------------------------------------------
-- Companies matched (not created) by a later research run. Lets a run list a
-- company it found that already exists, without inserting it again, scoring
-- it again, or handing it to Outreach as a new prospect/campaign. See
-- csg/dedupe.py and discover.run_discovery. ON DELETE CASCADE: if the company
-- is deleted, its match rows go with it (so it can be rediscovered as new).
CREATE TABLE IF NOT EXISTS run_company_matches (
    run_id     TEXT NOT NULL,
    domain     TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    matched_by TEXT NOT NULL,          -- 'domain' | 'name'
    matched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_run_matches_domain ON run_company_matches(domain);

-- New writes must use the canonical bare-host form (see dedupe.normalize_domain).
-- NOT VALID: enforced for new/updated rows only, so any legacy row that does
-- not comply is left untouched instead of failing this statement.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'companies_domain_normalized') THEN
        ALTER TABLE companies ADD CONSTRAINT companies_domain_normalized
            CHECK (domain = lower(domain) AND domain !~ '^www\.' AND domain !~ '[/:\s]') NOT VALID;
    END IF;
END $$;
