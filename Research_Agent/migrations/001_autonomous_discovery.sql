-- ---------------------------------------------------------------------------
-- Migration 001 -- autonomous discovery
--
-- Run this ONCE against an existing Supabase database (Dashboard -> SQL
-- Editor -> New query -> paste -> Run). A database created fresh from
-- schema.sql already has everything here and does not need it.
--
-- Safe to re-run: every statement is IF NOT EXISTS or guarded, so running it
-- twice is a no-op. It only adds columns and backfills -- no data is dropped.
--
-- What it fixes: revenue, funding, description, region, delivery_fit and
-- practice_area existed in the application models but were never persisted,
-- so researched data was silently discarded between the enrichment step and
-- scoring. employee_count also moved from a bare integer to a JSONB
-- provenance envelope.
-- ---------------------------------------------------------------------------

-- 1. Firmographics researched by the enrichment step.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS revenue     JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS funding     JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS region      TEXT;

-- 2. Req classification feeding the CSG Delivery Fit score.
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS delivery_fit  TEXT;
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS practice_area TEXT;

-- 3. employee_count: bare integer -> JSONB provenance envelope.
--    Existing scalars are wrapped and marked 'legacy_import' so they stay
--    distinguishable from freshly researched values. Already-JSONB columns
--    are left untouched.
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

-- 4. Indexes -- created last, after the columns they reference exist.
CREATE INDEX IF NOT EXISTS idx_companies_region ON companies(region);
CREATE INDEX IF NOT EXISTS idx_scores_run       ON scores(run_id);
CREATE INDEX IF NOT EXISTS idx_postings_domain  ON job_postings(domain);

-- 5. Verify. Expect 6 rows: description, funding, region, revenue (companies)
--    and delivery_fit, practice_area (job_postings); employee_count = jsonb.
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE (table_name = 'companies'
       AND column_name IN ('revenue', 'funding', 'description', 'region', 'employee_count'))
   OR (table_name = 'job_postings'
       AND column_name IN ('delivery_fit', 'practice_area'))
ORDER BY table_name, column_name;
