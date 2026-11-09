-- ---------------------------------------------------------------------------
-- Migration 006 -- company-level ICP, hiring-model, evidence, and readiness
--
-- Safe to re-run. Adds Research-owned assessment storage only; it does not
-- change scoring, qualification, deduplication, job delivery_fit, or Outreach.
-- schema.sql contains the same definitions for new and existing projects.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS company_research_assessments (
    assessment_id              TEXT PRIMARY KEY,
    domain                     TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    assessment_version         INTEGER NOT NULL CHECK (assessment_version > 0),
    schema_version             INTEGER NOT NULL DEFAULT 1,
    source_run_id              TEXT,
    readiness_status           TEXT NOT NULL CHECK (readiness_status IN (
                                   'unverified_pipeline', 'contact_enrichment_pending',
                                   'fully_researched', 'rejected')),
    prospecting_disposition    TEXT NOT NULL CHECK (prospecting_disposition IN (
                                   'eligible', 'needs_review', 'excluded_existing_client', 'rejected')),
    rejection_reason           TEXT,
    category_assessments       JSONB NOT NULL DEFAULT '[]'::jsonb,
    hiring_model_assessments   JSONB NOT NULL DEFAULT '[]'::jsonb,
    criteria_assessments       JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision_makers            JSONB NOT NULL DEFAULT '[]'::jsonb,
    assessed_at                TIMESTAMPTZ NOT NULL,
    UNIQUE (domain, assessment_version)
);

CREATE TABLE IF NOT EXISTS company_evidence (
    evidence_id          TEXT NOT NULL,
    assessment_id        TEXT NOT NULL REFERENCES company_research_assessments(assessment_id) ON DELETE CASCADE,
    domain               TEXT NOT NULL REFERENCES companies(domain) ON DELETE CASCADE,
    criterion            TEXT NOT NULL,
    subject              TEXT,
    claim                JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_status      TEXT NOT NULL CHECK (evidence_status IN ('verified', 'inferred', 'unavailable')),
    confidence           TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source_name          TEXT,
    source_type          TEXT CHECK (source_type IS NULL OR source_type IN (
                            'official_company', 'official_job_board', 'linkedin', 'third_party', 'aggregator', 'other')),
    source_url           TEXT,
    source_published_at  TIMESTAMPTZ,
    retrieved_at         TIMESTAMPTZ,
    excerpt              TEXT,
    conflict_group       TEXT,
    unavailable_reason   TEXT,
    CHECK (
        (evidence_status = 'unavailable' AND unavailable_reason IS NOT NULL)
        OR
        (evidence_status <> 'unavailable' AND source_name IS NOT NULL
         AND source_type IS NOT NULL AND source_url IS NOT NULL)
    ),
    PRIMARY KEY (assessment_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_research_assessments_domain_version
    ON company_research_assessments(domain, assessment_version DESC);
CREATE INDEX IF NOT EXISTS idx_company_evidence_assessment
    ON company_evidence(assessment_id);
CREATE INDEX IF NOT EXISTS idx_company_evidence_domain_criterion
    ON company_evidence(domain, criterion);