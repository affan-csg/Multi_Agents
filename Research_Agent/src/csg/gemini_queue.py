"""Shared adaptive admission control for Gemini requests."""

from __future__ import annotations

import email.utils
import os
import random
import time
from typing import Any, Callable

from .diagnostics import log_event

START_RPM = float(os.environ.get("GEMINI_QUEUE_START_RPM", "0.5"))
MIN_RPM = 0.1
MAX_RETRIES = int(os.environ.get("GEMINI_QUEUE_MAX_RETRIES", "4"))
BASE_BACKOFF_SECONDS = float(os.environ.get("GEMINI_QUEUE_BASE_BACKOFF_SECONDS", "2"))
_LOCAL_RATE = START_RPM
_LOCAL_NEXT_SLOT = 0.0
_LOCAL_BLOCKED_UNTIL = 0.0


def uses_shared_scheduler() -> bool:
    return bool(
        os.environ.get("RESEARCH_SUPABASE_URL")
        and os.environ.get("RESEARCH_SUPABASE_SERVICE_KEY")
    )


class GeminiQueueUnavailable(RuntimeError):
    """The shared Supabase scheduler could not safely admit a Gemini call."""


def _require_shared_scheduler() -> None:
    if not uses_shared_scheduler():
        raise GeminiQueueUnavailable(
            "Shared Gemini scheduler is not configured; refusing provider request"
        )


def _classification_label(classification: str) -> str:
    return {
        "rpm": "RPM throttling",
        "tpm": "TPM throttling",
        "daily_quota": "daily quota exhausted",
        "monthly_quota": "monthly quota exhausted",
        "temporary_capacity": "temporary provider capacity",
    }.get(classification, classification.replace("_", " "))


class GeminiQuotaBlocked(RuntimeError):
    def __init__(self, classification: str, retry_after: float | None = None,
                 status_code: int | None = None):
        self.classification = classification
        self.retry_after = retry_after
        self.status_code = status_code
        super().__init__(f"Gemini failure: {_classification_label(classification)}; request is quota-blocked")


class GeminiRetryLater(RuntimeError):
    def __init__(self, classification: str, delay_seconds: float, status_code: int | None = None):
        self.classification = classification
        self.delay_seconds = delay_seconds
        self.status_code = status_code
        super().__init__(f"Gemini failure: {_classification_label(classification)}; retry in {delay_seconds:.1f}s")


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                target = email.utils.parsedate_to_datetime(value)
                return max(0.0, target.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        error = details.get("error", details)
        for item in error.get("details", []) or []:
            if isinstance(item, dict) and item.get("retryDelay"):
                raw = str(item["retryDelay"]).rstrip("s")
                try:
                    return max(0.0, float(raw))
                except ValueError:
                    pass
    return None


def _row(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _reserve() -> dict[str, Any]:
    _require_shared_scheduler()
    from . import db
    try:
        conn = db.connect()
        try:
            admission = _row(conn.rpc("reserve_gemini_request_slot", {}))
        finally:
            conn.close()
    except Exception as exc:
        raise GeminiQueueUnavailable("Shared Gemini scheduler reservation failed") from exc
    if "wait_seconds" not in admission:
        raise GeminiQueueUnavailable("Shared Gemini scheduler returned no reservation")
    return admission


def _record(success: bool, classification: str | None = None, retry_after: float | None = None,
            total_tokens: int | None = None) -> dict[str, Any]:
    _require_shared_scheduler()
    from . import db
    try:
        conn = db.connect()
        try:
            state = _row(conn.rpc("record_gemini_request_result", {
                "p_success": success,
                "p_classification": classification,
                "p_retry_after_seconds": retry_after,
                "p_total_tokens": total_tokens,
            }))
        finally:
            conn.close()
    except Exception as exc:
        raise GeminiQueueUnavailable("Shared Gemini scheduler result update failed") from exc
    if not state:
        raise GeminiQueueUnavailable("Shared Gemini scheduler returned no result state")
    return state


def generate_content(
    request: Callable[[], Any], *, call_kind: str, run_id: str | None = None,
    domain: str | None = None,
) -> Any:
    """Pace and execute one Gemini call, sharing rate feedback across workers."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            admission = _reserve()
        except GeminiQueueUnavailable as exc:
            log_event("gemini_queue_unavailable", call_kind=call_kind, run_id=run_id,
                      domain=domain, stage="reserve", error=str(exc),
                      queue_backend="shared_supabase")
            raise
        wait = float(admission.get("wait_seconds", 0) or 0)
        if wait < 0:
            log_event("gemini_queue_blocked", call_kind=call_kind, run_id=run_id,
                      domain=domain, decision="blocked", blocked_reason=admission.get("blocked_reason"),
                      queue_backend="shared_supabase" if uses_shared_scheduler() else "process_local")
            raise GeminiQuotaBlocked(admission.get("blocked_reason") or "monthly_quota")
        if admission.get("blocked_reason"):
            log_event("gemini_queue_admission", call_kind=call_kind, run_id=run_id,
                      domain=domain, decision="deferred", blocked_reason=admission.get("blocked_reason"),
                      wait_seconds=round(wait, 3), current_rpm=admission.get("current_rpm"),
                      queue_depth=admission.get("queue_depth", 0),
                      queue_backend="shared_supabase" if uses_shared_scheduler() else "process_local")
        if admission.get("blocked_reason"):
            if wait:
                time.sleep(wait)
            continue
        if wait:
            log_event("gemini_queue_wait", call_kind=call_kind, run_id=run_id, domain=domain,
                      wait_seconds=round(wait, 3), current_rpm=admission.get("current_rpm"),
                      queue_depth=admission.get("queue_depth", 0))
            time.sleep(wait)
        log_event("gemini_queue_admission", call_kind=call_kind, run_id=run_id,
                  domain=domain, decision="admitted", wait_seconds=round(wait, 3),
                  current_rpm=admission.get("current_rpm"),
                  queue_depth=admission.get("queue_depth", 0),
                  queue_backend="shared_supabase" if uses_shared_scheduler() else "process_local")
        started = time.monotonic()
        try:
            response = request()
        except Exception as exc:
            from .discovery.gemini_keys import classify_gemini_error
            classification = classify_gemini_error(exc)
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            retry_after = _retry_after(exc)
            delay = retry_after
            if classification in ("rpm", "tpm", "temporary_capacity") and delay is None:
                delay = min(300.0, BASE_BACKOFF_SECONDS * (2 ** attempt))
            if delay is not None and classification in ("rpm", "tpm", "temporary_capacity"):
                delay += random.uniform(0.0, delay * 0.25)
            try:
                state = _record(False, classification, delay)
            except GeminiQueueUnavailable as queue_exc:
                log_event("gemini_queue_unavailable", call_kind=call_kind, run_id=run_id,
                          domain=domain, stage="record_failure", error=str(queue_exc),
                          provider_error=str(exc)[:2000], queue_backend="shared_supabase")
                raise queue_exc from exc
            log_event("gemini_request_failed", call_kind=call_kind, run_id=run_id, domain=domain,
                      classification=classification, status_code=status, retry_after_seconds=retry_after,
                      error_details={"type": type(exc).__name__, "message": str(exc)[:2000],
                                     "response": getattr(exc, "details", None)},
                      current_rpm=state.get("current_rpm"), queue_depth=state.get("queue_depth", 0),
                      attempt=attempt + 1)
            if classification in ("daily_quota", "monthly_quota"):
                raise GeminiQuotaBlocked(classification, retry_after, status) from exc
            if classification in ("rpm", "tpm", "temporary_capacity"):
                if attempt >= MAX_RETRIES:
                    raise GeminiRetryLater(classification, delay or 0.0, status) from exc
                time.sleep(delay or 0.0)
                continue
            raise
        meta = getattr(response, "usage_metadata", None)
        tokens = getattr(meta, "total_token_count", None) if meta is not None else None
        try:
            state = _record(True, total_tokens=tokens)
        except GeminiQueueUnavailable as exc:
            log_event("gemini_queue_unavailable", call_kind=call_kind, run_id=run_id,
                      domain=domain, stage="record_success", error=str(exc),
                      queue_backend="shared_supabase")
            raise
        log_event("gemini_request_succeeded", call_kind=call_kind, run_id=run_id, domain=domain,
                  latency_ms=int((time.monotonic() - started) * 1000), current_rpm=state.get("current_rpm"),
                  queue_depth=state.get("queue_depth", 0), total_tokens=tokens)
        return response
    raise AssertionError("unreachable")


def resume_global_quota() -> dict[str, Any]:
    """Clear a manually reviewed, indefinite global quota pause."""
    _require_shared_scheduler()
    from . import db
    try:
        conn = db.connect()
        try:
            return _row(conn.rpc("resume_gemini_quota", {}))
        finally:
            conn.close()
    except Exception as exc:
        raise GeminiQueueUnavailable("Shared Gemini scheduler resume failed") from exc


def scheduler_status() -> dict[str, Any]:
    _require_shared_scheduler()
    from . import db
    try:
        conn = db.connect()
        try:
            rows = conn.select("gemini_scheduler_state", scheduler_id="eq.1", limit=1)
            if not rows:
                raise GeminiQueueUnavailable("Shared Gemini scheduler state is missing")
            status = rows[0]
            active_jobs = conn.select(
                "agent_jobs", status="in.(pending,running,quota_blocked)",
                select="job_id,status", limit=1000,
            )
            status["queue_depth"] = len(active_jobs)
            status["quota_blocked_jobs"] = sum(
                row.get("status") == "quota_blocked" for row in active_jobs
            )
            return status
        finally:
            conn.close()
    except GeminiQueueUnavailable:
        raise
    except Exception as exc:
        raise GeminiQueueUnavailable("Shared Gemini scheduler status unavailable") from exc