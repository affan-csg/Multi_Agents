-- Durable background-job snapshots and worker leases for Cloud Run.
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    run_id          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    done            INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    current_domain  TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result          JSONB,
    error           TEXT,
    idempotency_key TEXT UNIQUE,
    worker_id       TEXT,
    lease_until     TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON agent_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_run ON agent_jobs(run_id);
