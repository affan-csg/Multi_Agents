"""Best-effort usage/quota tracking for Gemini, Groq, Tavily, and Hunter.io calls.

Feeds the Settings > Usage tab (requests, tokens, 429s, retries over time).
This module is purely observational: `record()` must never raise into the
caller and must never slow down the research/qualify/contacts flow it is
instrumenting. A dropped usage row is an acceptable loss; a broken research
run is not.

Writes go to Supabase in a fire-and-forget background thread so a slow or
unreachable DB never adds latency to the provider call it is recording. Call
sites wrap an existing provider call with `track()` and add no other
control-flow changes -- retries, fallbacks, and exceptions raised to callers
are exactly what they were before instrumentation.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

_LOCK = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Usage:
    provider: str
    call_kind: str
    run_id: Optional[str] = None
    domain: Optional[str] = None
    success: bool = True
    status_code: Optional[int] = None
    error_kind: Optional[str] = None
    retry_count: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    _start: float = field(default_factory=time.monotonic)

    def mark_failure(self, status_code: Optional[int] = None, error_kind: Optional[str] = None) -> None:
        self.success = False
        if status_code is not None:
            self.status_code = status_code
        if error_kind is not None:
            self.error_kind = error_kind

    def set_tokens(self, prompt: Optional[int] = None, completion: Optional[int] = None, total: Optional[int] = None) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total if total is not None else (
            (prompt or 0) + (completion or 0) if prompt is not None or completion is not None else None
        )


@contextmanager
def track(
    provider: str,
    call_kind: str,
    run_id: Optional[str] = None,
    domain: Optional[str] = None,
) -> Iterator[_Usage]:
    """Wrap one outbound call. On any exception raised inside the block, the
    row is recorded as a failure (best-guess status_code/error_kind from the
    exception) and the exception is re-raised unchanged -- this context
    manager never swallows or alters caller behavior.

    Usage:
        with track("gemini", "enrich_firmographics", domain=company.domain) as u:
            response = client.models.generate_content(...)
            u.set_tokens(*extract_gemini_tokens(response))
    """
    usage = _Usage(provider=provider, call_kind=call_kind, run_id=run_id, domain=domain)
    try:
        yield usage
    except Exception as exc:
        if usage.success:  # not already marked by the caller
            usage.mark_failure(status_code=_guess_status_code(exc), error_kind=classify_error(exc))
        raise
    finally:
        latency_ms = int((time.monotonic() - usage._start) * 1000)
        _record_async(usage, latency_ms)


def _guess_status_code(exc: Exception) -> Optional[int]:
    # httpx.HTTPStatusError (raised by resp.raise_for_status(), the common
    # path for every plain-httpx call site here) carries the real status on
    # .response, not on .code/.status_code -- check it first so a 4xx/5xx
    # from Hunter/Tavily/Groq is recorded exactly, not just guessed at 429.
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    if "429" in str(exc):
        return 429
    return None


def classify_error(exc: Exception) -> str:
    """Best-guess classification of any provider exception into one of:
    'timeout', 'connection_error', 'quota_exhausted', 'rate_limited',
    'parse_error', 'other'. Shared by usage_tracker's own recording and by
    call sites (discovery/research.py, discover.py) that want the SAME
    classification to show up in a run's user-facing failure reason, not
    just in the Usage tab -- so "why did this fail" reads the same way in
    both places instead of two independently-maintained heuristics drifting
    apart. Never raises -- an exception during classification just falls
    through to 'other', since misclassifying a failure must never itself
    break error handling.
    """
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
            return "timeout"
    except Exception:
        pass
    try:
        from .discovery.gemini_keys import is_connection_error, is_daily_quota_exhausted
        if is_connection_error(exc):
            return "connection_error"
        if is_daily_quota_exhausted(exc):
            return "quota_exhausted"
    except Exception:
        pass
    message = str(exc)
    if "timed out" in message.lower() or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if "429" in message or "RESOURCE_EXHAUSTED" in message or "rate limit" in message.lower():
        return "rate_limited"
    return "other"


def _record_async(usage: _Usage, latency_ms: int) -> None:
    row = {
        "provider": usage.provider,
        "call_kind": usage.call_kind,
        "run_id": usage.run_id,
        "domain": usage.domain,
        "success": usage.success,
        "status_code": usage.status_code,
        "error_kind": usage.error_kind,
        "retry_count": usage.retry_count,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "created_at": _utc_now_iso(),
    }
    # A daemon thread per call is deliberately simple (usage volume is low --
    # at most a few dozen calls per research run) rather than a shared queue/
    # worker: it guarantees this module can never block the caller waiting
    # for a DB round trip, and a process exit mid-write just drops that one
    # row, which is fine for a best-effort metrics table.
    threading.Thread(target=_write_row, args=(row,), daemon=True).start()


def _write_row(row: dict[str, Any]) -> None:
    try:
        from . import db
        conn = db.connect()
        try:
            db.insert_api_usage(conn, [row])
        finally:
            conn.close()
    except Exception:
        pass  # usage logging must never surface an error to anything
