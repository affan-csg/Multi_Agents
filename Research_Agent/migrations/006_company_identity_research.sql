-- Preserve source, retrieval date, and confidence for basic company identity facts.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS website JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS headquarters JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS company_type JSONB;