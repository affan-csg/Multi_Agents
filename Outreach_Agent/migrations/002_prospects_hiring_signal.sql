-- Migration 002 -- add prospects.hiring_signal, which was missing from
-- schema.sql despite being part of the ImportedProspect model since Research
-- Agent hand-off. Run this ONCE in the Outreach project's Supabase SQL
-- editor.
--
-- A fresh project provisioned from the current schema.sql does not need
-- this file -- the column is already there. Safe to run more than once
-- (IF NOT EXISTS); no data is read, rewritten, or deleted.
--
-- Symptom this fixes: POST /campaigns/test (and any prospect
-- import/upsert) failing with
--   PGRST204: Could not find the 'hiring_signal' column of 'prospects'
--   in the schema cache
-- because the application code (models.py / ingest.py) has always written
-- this field, but the live table never had a column for PostgREST to write
-- it into.

ALTER TABLE prospects ADD COLUMN IF NOT EXISTS hiring_signal TEXT;
