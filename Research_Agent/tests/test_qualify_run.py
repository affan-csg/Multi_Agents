"""Tests for qualify_run's persistence/retry behavior and failed_rows/
retry_failed_run -- the requirements that:
  - AI qualification actually runs for every eligible (pending) row
  - transient LLM/API failures are retried with backoff
  - a failure is never silently marked complete
  - the qualification result is stored reliably (success AND failure)
  - a failed qualification can be retried later

No real network/LLM: `_select_backend` and `db.*` are monkeypatched with an
in-memory fake backend/store. `_complete_with_retry`'s own backoff-retry
loop is exercised directly against a scripted backend that fails N times
then succeeds, with asyncio.sleep patched out so the test doesn't actually
wait on BASE_RETRY_DELAY.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from csg import qualify


class _FakeConn:
    """qualify_run/worker only ever calls conn.commit() on this -- everything
    else goes through the monkeypatched db.* functions, which ignore `conn`
    entirely and read/write the shared _FakeStore instead."""

    def commit(self) -> None:
        pass


class _FakeStore:
    """In-memory stand-in for the qualifications/qualification_attempts
    tables, keyed the same way db.py's real functions are (domain, run_id)."""

    def __init__(self) -> None:
        self.qualifications: dict[tuple[str, str], dict[str, Any]] = {}
        self.attempts: dict[tuple[str, str], dict[str, Any]] = {}

    def save_qualification(self, conn, run_id, domain, *, pain_summary, recommended_angle, model):
        self.qualifications[(domain, run_id)] = {
            "domain": domain, "run_id": run_id, "pain_summary": pain_summary,
            "recommended_angle": recommended_angle, "model": model,
        }

    def get_qualifications(self, conn, run_id):
        return {d: v for (d, r), v in self.qualifications.items() if r == run_id}

    def save_qualification_attempt(self, conn, run_id, domain, status, error_reason=None, error_detail=None):
        key = (domain, run_id)
        prev = self.attempts.get(key)
        self.attempts[key] = {
            "domain": domain, "run_id": run_id, "status": status,
            "error_reason": error_reason, "error_detail": error_detail,
            "attempt_count": (prev["attempt_count"] + 1) if prev else 1,
        }

    def get_qualification_attempt(self, conn, domain, run_id):
        return self.attempts.get((domain, run_id))

    def get_failed_qualifications(self, conn, run_id):
        return [v for (d, r), v in self.attempts.items() if r == run_id and v["status"] == "failed"]


class _FakeBackend:
    """Scripted backend: `outcomes[domain]` is either a _Qualification-shaped
    object (success) or an Exception instance/class (failure). is_retryable
    always returns False here -- retry-loop behavior itself is tested
    separately against _complete_with_retry directly."""

    model = "fake-model"
    max_concurrency = 4

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.closed = False

    async def complete(self, prompt: str):
        # domain isn't in the prompt in a way we want to parse -- the test
        # instead monkeypatches _render_prompt to just return the domain.
        domain = prompt
        self.calls.append(domain)
        outcome = self.outcomes[domain]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("boom")
        return outcome

    async def close(self):
        self.closed = True

    def is_retryable(self, exc: Exception) -> bool:
        return False


def _row(domain: str, tier: str = "A") -> dict:
    return {
        "domain": domain, "name": domain, "employee_count": 100, "industry": "Software",
        "tier": tier, "score": 80.0, "dimensions": {}, "explanation": [],
        "metrics": {}, "hiring_signal": "unknown",
    }


class _Qual:
    def __init__(self, pain_summary: str, recommended_angle: str = "angle"):
        self.pain_summary = pain_summary
        self.recommended_angle = recommended_angle


@pytest.fixture
def store(monkeypatch):
    s = _FakeStore()
    monkeypatch.setattr(qualify.db, "save_qualification", s.save_qualification)
    monkeypatch.setattr(qualify.db, "get_qualifications", s.get_qualifications)
    monkeypatch.setattr(qualify.db, "save_qualification_attempt", s.save_qualification_attempt)
    monkeypatch.setattr(qualify.db, "get_qualification_attempt", s.get_qualification_attempt)
    monkeypatch.setattr(qualify.db, "get_failed_qualifications", s.get_failed_qualifications)
    monkeypatch.setattr(qualify.db, "get_postings", lambda conn, domain: [])
    # _render_prompt normally serializes the whole row -- for these tests we
    # only need it to identify which domain a call is for.
    monkeypatch.setattr(qualify, "_render_prompt", lambda row, postings: row["domain"])
    return s


def _set_scores(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(qualify.db, "get_scores", lambda conn, run_id: rows)


# --- success path ------------------------------------------------------------

def test_qualify_run_persists_success_to_both_tables(monkeypatch, store):
    _set_scores(monkeypatch, [_row("a.com")])
    backend = _FakeBackend({"a.com": _Qual("pain")})
    monkeypatch.setattr(qualify, "_select_backend", lambda: backend)

    stats = asyncio.run(qualify.qualify_run(conn=_FakeConn(), run_id="run1"))

    assert stats == {"qualified": 1, "skipped": 0, "errors": 0, "error_details": {}}
    assert store.qualifications[("a.com", "run1")]["pain_summary"] == "pain"
    assert store.attempts[("a.com", "run1")]["status"] == "succeeded"
    assert backend.closed


# --- failure path -- must be persisted, not silently dropped -----------------

def test_qualify_run_persists_failure_instead_of_dropping_it(monkeypatch, store):
    """The bug this closes: a permanent failure used to leave NO row anywhere
    -- indistinguishable from "never attempted". It must now show up in the
    attempts table with status='failed' and a real reason."""
    _set_scores(monkeypatch, [_row("bad.com")])
    backend = _FakeBackend({"bad.com": RuntimeError("LLM exploded")})
    monkeypatch.setattr(qualify, "_select_backend", lambda: backend)

    stats = asyncio.run(qualify.qualify_run(conn=_FakeConn(), run_id="run1"))

    assert stats["qualified"] == 0
    assert stats["errors"] == 1
    assert "bad.com" in stats["error_details"]
    assert ("bad.com", "run1") not in store.qualifications
    attempt = store.attempts[("bad.com", "run1")]
    assert attempt["status"] == "failed"
    assert attempt["error_reason"] == "other"
    assert "LLM exploded" in attempt["error_detail"]


def test_qualify_run_handles_partial_failure_across_multiple_companies(monkeypatch, store):
    """Requirement: partial failures must be handled -- one bad company must
    not prevent the others in the same run from being qualified and saved."""
    _set_scores(monkeypatch, [_row("good.com"), _row("bad.com"), _row("good2.com")])
    backend = _FakeBackend({
        "good.com": _Qual("pain1"),
        "bad.com": RuntimeError("503 from provider"),
        "good2.com": _Qual("pain2"),
    })
    monkeypatch.setattr(qualify, "_select_backend", lambda: backend)

    stats = asyncio.run(qualify.qualify_run(conn=_FakeConn(), run_id="run1"))

    assert stats["qualified"] == 2
    assert stats["errors"] == 1
    assert set(store.qualifications.keys()) == {("good.com", "run1"), ("good2.com", "run1")}
    assert store.attempts[("bad.com", "run1")]["status"] == "failed"
    assert store.attempts[("good.com", "run1")]["status"] == "succeeded"


# --- retry_failed_run ---------------------------------------------------------

def test_retry_failed_run_only_retries_domains_that_failed(monkeypatch, store):
    _set_scores(monkeypatch, [_row("failed.com"), _row("succeeded.com"), _row("never-tried.com")])
    store.attempts[("failed.com", "run1")] = {
        "domain": "failed.com", "run_id": "run1", "status": "failed",
        "error_reason": "timeout", "error_detail": "timed out", "attempt_count": 1,
    }
    store.qualifications[("succeeded.com", "run1")] = {"pain_summary": "already qualified"}
    store.attempts[("succeeded.com", "run1")] = {
        "domain": "succeeded.com", "run_id": "run1", "status": "succeeded",
        "error_reason": None, "error_detail": None, "attempt_count": 1,
    }
    # never-tried.com has no attempt row at all -- must NOT be picked up by retry_failed_run
    # (that's what qualify_run's normal pending_rows path is for).

    backend = _FakeBackend({"failed.com": _Qual("recovered")})
    monkeypatch.setattr(qualify, "_select_backend", lambda: backend)

    stats = asyncio.run(qualify.retry_failed_run(conn=_FakeConn(), run_id="run1"))

    assert backend.calls == ["failed.com"]
    assert stats["qualified"] == 1
    assert store.qualifications[("failed.com", "run1")]["pain_summary"] == "recovered"
    assert store.attempts[("failed.com", "run1")]["status"] == "succeeded"
    # attempt_count carried forward (this is the 2nd attempt), not reset
    assert store.attempts[("failed.com", "run1")]["attempt_count"] == 2


def test_retry_failed_run_no_failures_is_a_cheap_noop(monkeypatch, store):
    _set_scores(monkeypatch, [_row("ok.com")])

    def fail_if_called():
        raise AssertionError("_select_backend must not be called when nothing is failed")
    monkeypatch.setattr(qualify, "_select_backend", lambda: fail_if_called())

    stats = asyncio.run(qualify.retry_failed_run(conn=_FakeConn(), run_id="run1"))
    assert stats == {"qualified": 0, "skipped": 0, "errors": 0, "error_details": {}}


def test_failed_rows_excludes_domains_that_later_succeeded(monkeypatch, store):
    """A domain whose attempt history has a 'failed' row but ALSO has a
    successful qualifications row (e.g. failed once, force-re-qualified
    successfully later without going through retry_failed_run) must not be
    offered up for retry again."""
    _set_scores(monkeypatch, [_row("recovered.com")])
    store.attempts[("recovered.com", "run1")] = {
        "domain": "recovered.com", "run_id": "run1", "status": "failed",
        "error_reason": "timeout", "error_detail": "x", "attempt_count": 1,
    }
    store.qualifications[("recovered.com", "run1")] = {"pain_summary": "now has one"}

    rows = qualify.failed_rows(conn=None, run_id="run1")
    assert rows == []


# --- _complete_with_retry: backoff on transient errors, no retry on permanent

class _RetryableExc(Exception):
    pass


class _PermanentExc(Exception):
    pass


class _ScriptedRetryBackend:
    model = "fake"
    max_concurrency = 1

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.attempts = 0

    async def complete(self, prompt: str):
        self.attempts += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, _RetryableExc)

    async def close(self):
        pass


def test_complete_with_retry_recovers_after_transient_failures(monkeypatch):
    """Simulates rate-limit/5xx-style transient errors on the first two
    attempts, succeeding on the third -- within MAX_RETRIES=4."""
    async def no_sleep(seconds):
        return None
    monkeypatch.setattr(qualify.asyncio, "sleep", no_sleep)

    backend = _ScriptedRetryBackend([_RetryableExc("429"), _RetryableExc("503"), _Qual("finally")])
    result = asyncio.run(qualify._complete_with_retry(backend, "prompt"))

    assert result.pain_summary == "finally"
    assert backend.attempts == 3


def test_complete_with_retry_gives_up_after_max_retries(monkeypatch):
    async def no_sleep(seconds):
        return None
    monkeypatch.setattr(qualify.asyncio, "sleep", no_sleep)

    backend = _ScriptedRetryBackend([_RetryableExc("429")] * qualify.MAX_RETRIES)
    with pytest.raises(_RetryableExc):
        asyncio.run(qualify._complete_with_retry(backend, "prompt"))
    assert backend.attempts == qualify.MAX_RETRIES


def test_complete_with_retry_does_not_retry_permanent_errors(monkeypatch):
    """A non-retryable error (e.g. a 4xx/parse error) must fail fast, not
    burn through MAX_RETRIES worth of backoff for an error that will never
    succeed on its own."""
    async def fail_if_slept(seconds):
        raise AssertionError("must not sleep/retry a non-retryable error")
    monkeypatch.setattr(qualify.asyncio, "sleep", fail_if_slept)

    backend = _ScriptedRetryBackend([_PermanentExc("bad request")])
    with pytest.raises(_PermanentExc):
        asyncio.run(qualify._complete_with_retry(backend, "prompt"))
    assert backend.attempts == 1


# --- pending_rows: tier/domains/force interplay, including with failures -----

def test_pending_rows_reincludes_a_previously_failed_domain_without_force(monkeypatch, store):
    """A domain that failed must be picked up again on the very next
    un-forced qualify_run -- it must not require an explicit `force=True` or
    a separate retry action just to be tried once more."""
    _set_scores(monkeypatch, [_row("failed.com"), _row("qualified.com")])
    store.attempts[("failed.com", "run1")] = {
        "domain": "failed.com", "run_id": "run1", "status": "failed",
        "error_reason": "timeout", "error_detail": "x", "attempt_count": 1,
    }
    store.qualifications[("qualified.com", "run1")] = {"pain_summary": "done"}

    rows = qualify.pending_rows(conn=None, run_id="run1")
    assert [r["domain"] for r in rows] == ["failed.com"]
