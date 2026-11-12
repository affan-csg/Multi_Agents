ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS research_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS research_outcomes JSONB NOT NULL DEFAULT '{}'::jsonb;