ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS worker_version TEXT;
ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS error_details JSONB;

CREATE TABLE IF NOT EXISTS research_diagnostic_events (
    event_id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    run_id TEXT,
    job_id TEXT,
    worker_id TEXT,
    worker_version TEXT NOT NULL,
    provider TEXT,
    call_kind TEXT,
    domain TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_diagnostic_events_time
    ON research_diagnostic_events(created_at);
CREATE INDEX IF NOT EXISTS idx_research_diagnostic_events_run
    ON research_diagnostic_events(run_id, created_at);

ALTER TABLE research_diagnostic_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE research_diagnostic_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE research_diagnostic_events TO service_role;
