-- ---------------------------------------------------------------------------
-- Migration 003 -- multi-level job discovery fallback
--
-- Run this ONCE against an existing Supabase database (Dashboard -> SQL
-- Editor -> New query -> paste -> Run). A database created fresh from
-- schema.sql already has these columns and does not need it.
--
-- Safe to re-run: every statement is IF NOT EXISTS / ADD COLUMN IF NOT
-- EXISTS, no data is dropped or rewritten.
--
-- What it adds: provenance for job discovery so "no ATS detected" can never
-- be silently read as "no jobs" / "no hiring need" (see jobs_discovery.py
-- and pipeline.py). Four new columns on companies record how job data was
-- (or wasn't) found; one new column on job_postings records which of the
-- fallback methods produced a given posting, since not every posting comes
-- from an ATS adapter any more.
-- ---------------------------------------------------------------------------

ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_discovery_method TEXT;
-- 'ats' | 'careers_page' | 'web_search' | 'unknown' | NULL (not yet processed)

ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_source TEXT;
-- human-readable detail, e.g. the ATS provider name, "careers page scrape",
-- or "tavily search"

ALTER TABLE companies ADD COLUMN IF NOT EXISTS hiring_signal TEXT;
-- 'confirmed' | 'unknown' -- whether the job count behind this company's
-- score is real evidence (a source was actually read, zero postings or not)
-- or an absence of evidence (every discovery method was exhausted).
-- Scoring must never treat 'unknown' as a confirmed zero.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS job_discovery_notes TEXT;
-- failure reason when hiring_signal = 'unknown', e.g. "ATS not detected;
-- careers page unreachable (timeout); web search found no results"

ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'ats';
-- 'ats' | 'careers_page' | 'web_search' -- how this specific posting was
-- found. Existing rows (all from an ATS adapter, the only method that
-- existed before this migration) default to 'ats' correctly with no backfill
-- needed.
