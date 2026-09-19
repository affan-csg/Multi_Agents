-- ---------------------------------------------------------------------------
-- Migration 002 -- API usage/quota tracking
--
-- Run this ONCE against an existing Supabase database (Dashboard -> SQL
-- Editor -> New query -> paste -> Run). A database created fresh from
-- schema.sql already has this table and does not need it.
--
-- Safe to re-run: CREATE TABLE/INDEX IF NOT EXISTS, no data is dropped.
--
-- What it adds: the api_usage table backing the Settings > Usage tab --
-- one row per outbound call to Gemini, Groq, or Tavily, written best-effort
-- by usage_tracker.py. Purely observational (a metrics/cost record), not
-- linked by foreign key to runs/companies -- it must keep recording usage
-- made outside any run_id and must survive a run being deleted.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_usage (
    id            BIGSERIAL PRIMARY KEY,
    provider      TEXT NOT NULL,       -- 'gemini' | 'groq' | 'tavily' | 'hunter'
    call_kind     TEXT NOT NULL,       -- e.g. 'discovery_search', 'enrich_firmographics', 'qualify', 'groq_parse', 'tavily_search'
    run_id        TEXT,
    domain        TEXT,
    success       BOOLEAN NOT NULL,
    status_code   INTEGER,
    error_kind    TEXT,                -- 'quota_exhausted' | 'rate_limited' | 'connection_error' | 'parse_error' | 'other'
    retry_count   INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    created_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_usage_provider_time ON api_usage(provider, created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_created_at    ON api_usage(created_at);
