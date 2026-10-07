-- ---------------------------------------------------------------------------
-- Migration 005 -- global company de-duplication across research runs
--
-- Run ONCE against an existing Supabase database (SQL Editor). Safe to
-- re-run; drops and rewrites no data. Also appended to schema.sql.
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
