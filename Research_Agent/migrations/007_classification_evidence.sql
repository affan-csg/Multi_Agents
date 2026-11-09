ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS classification_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;