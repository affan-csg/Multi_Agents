-- Separate "what the company does" from company_type (ownership/maturity).
--
-- company_type keeps its meaning: Startup, SMB, System Integrator,
-- Public Company, Private Company. business_category is a grounded research
-- fact (Cybersecurity, SaaS, AI/ML, IT Services, Healthcare Technology, ...)
-- stored as a ProvenanceValue envelope:
--   {"value": "Cybersecurity", "source": "gemini_grounded_search",
--    "url": "...", "fetched_at": "...", "method": "reported",
--    "confidence": "high"}
--
-- Additive only: existing rows keep NULL until they are researched again, and
-- no existing column is rewritten.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS business_category JSONB;
