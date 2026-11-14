CREATE TABLE IF NOT EXISTS agent_job_checkpoints (
    job_id TEXT NOT NULL REFERENCES agent_jobs(job_id) ON DELETE CASCADE,
    checkpoint_key TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, checkpoint_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_job_checkpoints_job ON agent_job_checkpoints(job_id);

ALTER TABLE agent_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE agent_jobs FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE agent_jobs TO service_role;
ALTER TABLE agent_job_checkpoints ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE agent_job_checkpoints FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE agent_job_checkpoints TO service_role;

CREATE OR REPLACE FUNCTION save_agent_job_checkpoint(
    p_job_id TEXT,
    p_checkpoint_key TEXT,
    p_worker_id TEXT,
    p_status TEXT,
    p_payload JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
        current_worker TEXT;
        current_status TEXT;
        current_lease TIMESTAMPTZ;
BEGIN
        SELECT worker_id, status, lease_until
            INTO current_worker, current_status, current_lease
            FROM public.agent_jobs
         WHERE job_id = p_job_id
         FOR UPDATE;

        IF current_worker IS DISTINCT FROM p_worker_id
             OR current_status <> 'running'
             OR current_lease IS NULL
             OR current_lease <= NOW() THEN
                RETURN FALSE;
        END IF;

    INSERT INTO public.agent_job_checkpoints (job_id, checkpoint_key, status, worker_id, payload, updated_at)
        VALUES (p_job_id, p_checkpoint_key, p_status, p_worker_id,
                        COALESCE(p_payload, '{}'::jsonb), NOW())
    ON CONFLICT (job_id, checkpoint_key) DO UPDATE
       SET status = EXCLUDED.status,
           worker_id = EXCLUDED.worker_id,
           payload = EXCLUDED.payload,
           updated_at = EXCLUDED.updated_at;
    RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION save_agent_job_checkpoint(TEXT, TEXT, TEXT, TEXT, JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION save_agent_job_checkpoint(TEXT, TEXT, TEXT, TEXT, JSONB)
    TO service_role;