CREATE TABLE IF NOT EXISTS gemini_scheduler_state (
    scheduler_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (scheduler_id = 1),
    current_rpm DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    next_slot_at TIMESTAMPTZ,
    blocked_until TIMESTAMPTZ,
    blocked_reason TEXT,
    successful_calls BIGINT NOT NULL DEFAULT 0,
    rate_limited_calls BIGINT NOT NULL DEFAULT 0,
    quota_blocked_jobs BIGINT NOT NULL DEFAULT 0,
    retry_count BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS quota_blocked_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_agent_jobs_quota_blocked
    ON agent_jobs(status, quota_blocked_until);

ALTER TABLE gemini_scheduler_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE gemini_scheduler_state FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE gemini_scheduler_state TO service_role;

CREATE OR REPLACE FUNCTION reserve_gemini_request_slot()
RETURNS TABLE(wait_seconds DOUBLE PRECISION, current_rpm DOUBLE PRECISION,
              queue_depth BIGINT, blocked_reason TEXT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    state gemini_scheduler_state%ROWTYPE;
    slot_time TIMESTAMPTZ;
BEGIN
    INSERT INTO public.gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
    SELECT * INTO state FROM public.gemini_scheduler_state WHERE scheduler_id = 1 FOR UPDATE;
    IF state.blocked_reason IS NOT NULL AND
       (state.blocked_until IS NULL OR state.blocked_until > NOW()) THEN
        RETURN QUERY SELECT
            (CASE WHEN state.blocked_until IS NULL THEN -1.0
                  ELSE EXTRACT(EPOCH FROM state.blocked_until - NOW()) END)::DOUBLE PRECISION,
            state.current_rpm,
            (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked')),
            state.blocked_reason;
        RETURN;
    END IF;
    IF state.blocked_until IS NOT NULL AND state.blocked_until <= NOW() THEN
        UPDATE public.gemini_scheduler_state
           SET blocked_until = NULL, blocked_reason = NULL, updated_at = NOW()
         WHERE scheduler_id = 1;
    END IF;
    slot_time := GREATEST(COALESCE(state.next_slot_at, NOW()), NOW());
     UPDATE public.gemini_scheduler_state AS scheduler
         SET next_slot_at = slot_time + make_interval(secs => 60.0 / GREATEST(scheduler.current_rpm, 0.1)),
           updated_at = NOW()
     WHERE scheduler_id = 1;
    RETURN QUERY SELECT
        GREATEST(0.0, EXTRACT(EPOCH FROM slot_time - NOW()))::DOUBLE PRECISION,
        state.current_rpm,
        (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked')),
        NULL::TEXT;
END;
$$;

CREATE OR REPLACE FUNCTION record_gemini_request_result(
    p_success BOOLEAN,
    p_classification TEXT DEFAULT NULL,
    p_retry_after_seconds DOUBLE PRECISION DEFAULT NULL,
    p_total_tokens BIGINT DEFAULT NULL
) RETURNS TABLE(current_rpm DOUBLE PRECISION, queue_depth BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO public.gemini_scheduler_state (scheduler_id) VALUES (1) ON CONFLICT DO NOTHING;
    IF p_success THEN
        UPDATE public.gemini_scheduler_state AS scheduler
           SET current_rpm = scheduler.current_rpm + 0.05,
               successful_calls = scheduler.successful_calls + 1,
               total_tokens = scheduler.total_tokens + COALESCE(p_total_tokens, 0),
               updated_at = NOW()
         WHERE scheduler_id = 1;
    ELSE
        UPDATE public.gemini_scheduler_state AS scheduler
           SET current_rpm = CASE WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity')
                                  THEN GREATEST(0.1, scheduler.current_rpm / 2.0) ELSE scheduler.current_rpm END,
               rate_limited_calls = scheduler.rate_limited_calls +
                   CASE WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity') THEN 1 ELSE 0 END,
               quota_blocked_jobs = scheduler.quota_blocked_jobs +
                   CASE WHEN p_classification IN ('daily_quota', 'monthly_quota') THEN 1 ELSE 0 END,
               retry_count = scheduler.retry_count + 1,
               blocked_until = CASE
                   WHEN p_classification IN ('daily_quota', 'monthly_quota') AND p_retry_after_seconds IS NULL THEN NULL
                   WHEN p_classification IN ('daily_quota', 'monthly_quota') THEN
                       NOW() + make_interval(secs => GREATEST(0.0, p_retry_after_seconds))
                   WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity') THEN
                       NOW() + make_interval(secs => GREATEST(2.0, COALESCE(p_retry_after_seconds, 2.0)))
                   ELSE scheduler.blocked_until END,
               blocked_reason = CASE
                   WHEN p_classification IN ('rpm', 'tpm', 'temporary_capacity', 'daily_quota', 'monthly_quota')
                   THEN p_classification ELSE blocked_reason END,
               updated_at = NOW()
         WHERE scheduler_id = 1;
    END IF;
        RETURN QUERY SELECT state.current_rpm,
        (SELECT COUNT(*) FROM public.agent_jobs WHERE status IN ('pending', 'running', 'quota_blocked'))
            FROM public.gemini_scheduler_state AS state WHERE scheduler_id = 1;
END;
$$;

CREATE OR REPLACE FUNCTION resume_gemini_quota()
RETURNS TABLE(status TEXT, current_rpm DOUBLE PRECISION)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
        UPDATE public.gemini_scheduler_state AS scheduler
       SET blocked_until = NULL, blocked_reason = NULL,
                     current_rpm = GREATEST(0.1, scheduler.current_rpm / 2.0), updated_at = NOW()
     WHERE scheduler_id = 1;
        RETURN QUERY SELECT 'resumed'::TEXT, scheduler.current_rpm
            FROM public.gemini_scheduler_state AS scheduler WHERE scheduler_id = 1;
END;
$$;

REVOKE ALL ON FUNCTION reserve_gemini_request_slot() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION record_gemini_request_result(BOOLEAN, TEXT, DOUBLE PRECISION, BIGINT)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION resume_gemini_quota() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION reserve_gemini_request_slot() TO service_role;
GRANT EXECUTE ON FUNCTION record_gemini_request_result(BOOLEAN, TEXT, DOUBLE PRECISION, BIGINT)
    TO service_role;
GRANT EXECUTE ON FUNCTION resume_gemini_quota() TO service_role;