-- ---------------------------------------------------------------------------
-- Migration 004 -- qualification_attempts (track every LLM qualification
-- attempt, not just successes)
--
-- Run this ONCE against an existing Supabase database (Dashboard -> SQL
-- Editor -> New query -> paste -> Run). A database created fresh from
-- schema.sql already has this table and does not need it.
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS, no data is dropped or rewritten.
--
-- Why: `qualifications` (schema.sql) only ever gets a row on SUCCESS -- a
-- company whose qualification failed (LLM/API error after retries exhausted)
-- has no row anywhere, which is indistinguishable from "never attempted".
-- That let failures pass silently: the run-level job result showed an error
-- count in a toast that disappeared after a few seconds, but nothing
-- queryable recorded WHICH companies failed, WHY, or whether a later re-run
-- actually retried them vs. skipping them as already-handled.
--
-- qualification_attempts is the per-(domain, run_id) attempt ledger:
-- status='failed' after retries are exhausted (see qualify.py's
-- _complete_with_retry/qualify_run), status='succeeded' the moment
-- db.save_qualification also writes here. `qualifications` itself keeps its
-- original, unchanged meaning (a completed pain_summary/recommended_angle) --
-- this table is additive, not a replacement, so no existing reader of
-- `qualifications` needs to change.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS qualification_attempts (
    domain             TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    status             TEXT NOT NULL,  -- 'failed' | 'succeeded'
    error_reason       TEXT,           -- classify_error() bucket, e.g. 'rate_limited'; NULL when status='succeeded'
    error_detail       TEXT,           -- human-readable exception text for the last attempt
    attempt_count      INT NOT NULL DEFAULT 1,
    last_attempted_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (domain, run_id)
);
