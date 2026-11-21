from __future__ import annotations

from types import SimpleNamespace

import pytest

from csg import gemini_queue


class _GeminiError(Exception):
    def __init__(self, message: str, code: int, details: dict | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.response = SimpleNamespace(headers={"Retry-After": retry_after} if retry_after else {})


def _limited(quota_id: str | None = None, retry_after: str | None = None) -> _GeminiError:
    details = {"error": {"details": [{"violations": [{"quotaId": quota_id}]}]}} if quota_id else {}
    return _GeminiError("429 RESOURCE_EXHAUSTED", 429, details, retry_after)


@pytest.fixture(autouse=True)
def _local_scheduler(monkeypatch, request):
    if request.node.name == "test_missing_shared_scheduler_fails_closed_before_request":
        return None
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://supabase.example")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "service-key")
    scheduler = {"current_rpm": 0.5, "blocked_reason": None}
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {
        "wait_seconds": 0, **scheduler,
    })

    def record(success, classification=None, retry_after=None, total_tokens=None):
        if success:
            scheduler["current_rpm"] += 0.05
        elif classification in ("daily_quota", "monthly_quota"):
            scheduler["blocked_reason"] = classification
        elif classification in ("rpm", "tpm", "temporary_capacity"):
            scheduler["current_rpm"] = max(0.1, scheduler["current_rpm"] / 2)
        return {**scheduler, "queue_depth": 0}

    monkeypatch.setattr(gemini_queue, "_record", record)
    monkeypatch.setattr(gemini_queue, "MAX_RETRIES", 4)
    return scheduler


def test_paces_requests_and_increases_rate_after_success(monkeypatch, _local_scheduler):
    waits = []
    monkeypatch.setattr(gemini_queue.time, "sleep", waits.append)
    reservations = iter([
        {"wait_seconds": 0, "current_rpm": 0.5},
        {"wait_seconds": 120, "current_rpm": 0.55},
    ])
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: next(reservations))

    assert gemini_queue.generate_content(lambda: "first", call_kind="test") == "first"
    assert gemini_queue.generate_content(lambda: "second", call_kind="test") == "second"

    assert waits and waits[0] >= 119
    assert _local_scheduler["current_rpm"] == pytest.approx(0.6)


def test_rate_limit_decreases_rate_and_honors_retry_after(monkeypatch):
    waits = []
    monkeypatch.setattr(gemini_queue, "MAX_RETRIES", 1)
    monkeypatch.setattr(gemini_queue.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(gemini_queue.time, "sleep", waits.append)
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {"wait_seconds": 0})

    outcomes = []
    monkeypatch.setattr(gemini_queue, "_record", lambda *args, **kwargs: outcomes.append(args) or {"current_rpm": 0.25})
    responses = iter([_limited("GenerateContentPerMinutePerProjectPerModel", "7"), "ok"])

    result = gemini_queue.generate_content(lambda: next(responses) if False else _raise_or_return(next(responses)),
                                           call_kind="test")

    assert result == "ok"
    assert waits == [7.0]
    assert outcomes[0][1:3] == ("rpm", 7.0)


def _raise_or_return(value):
    if isinstance(value, Exception):
        raise value
    return value


def test_tpm_and_monthly_quota_are_classified_and_monthly_blocks(monkeypatch, _local_scheduler):
    calls = []
    reservations = iter([
        {"wait_seconds": 0, "current_rpm": 0.5},
        {"wait_seconds": -1, "blocked_reason": "monthly_quota", "current_rpm": 0.5},
    ])
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: next(reservations))
    with pytest.raises(gemini_queue.GeminiQuotaBlocked) as monthly:
        gemini_queue.generate_content(
            lambda: _raise_or_return(_limited("GenerateContentPerMonthPerProjectPerModel")),
            call_kind="test",
        )
    assert monthly.value.classification == "monthly_quota"
    with pytest.raises(gemini_queue.GeminiQuotaBlocked):
        gemini_queue.generate_content(lambda: calls.append("called"), call_kind="test")
    assert calls == []

    from csg.discovery.gemini_keys import classify_gemini_error
    assert classify_gemini_error(_limited("GenerateContentInputTokensPerMinutePerProject")) == "tpm"


def test_exponential_backoff_uses_jitter_and_retries_capacity_errors(monkeypatch):
    waits = []
    monkeypatch.setattr(gemini_queue, "MAX_RETRIES", 2)
    monkeypatch.setattr(gemini_queue.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(gemini_queue.time, "sleep", waits.append)
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {"wait_seconds": 0})
    monkeypatch.setattr(gemini_queue, "_record", lambda *_args, **_kwargs: {"current_rpm": 0.25})
    attempts = iter([_GeminiError("503 temporary capacity overload", 503),
                     _GeminiError("503 temporary capacity overload", 503), "ok"])

    result = gemini_queue.generate_content(
        lambda: _raise_or_return(next(attempts)), call_kind="test",
    )

    assert result == "ok"
    assert waits == [2.0, 4.0]


def test_requests_are_not_issued_while_quota_paused(monkeypatch, _local_scheduler):
    gemini_queue._record(False, "monthly_quota")
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {
        "wait_seconds": -1, "blocked_reason": "monthly_quota",
    })
    called = []
    with pytest.raises(gemini_queue.GeminiQuotaBlocked):
        gemini_queue.generate_content(lambda: called.append(True), call_kind="test")
    assert called == []


def test_monthly_quota_recovers_after_manual_resume(monkeypatch, _local_scheduler):
    gemini_queue._record(False, "monthly_quota")
    reservations = iter([
        {"wait_seconds": -1, "blocked_reason": "monthly_quota"},
        {"wait_seconds": 0, "current_rpm": 0.5},
    ])
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: next(reservations))
    with pytest.raises(gemini_queue.GeminiQuotaBlocked):
        gemini_queue.generate_content(lambda: "should not run", call_kind="test")

    def resume():
        _local_scheduler["blocked_reason"] = None
        return {"status": "resumed"}

    monkeypatch.setattr(gemini_queue, "resume_global_quota", resume)
    assert gemini_queue.resume_global_quota() == {"status": "resumed"}
    assert gemini_queue.generate_content(lambda: "resumed", call_kind="test") == "resumed"


def test_missing_shared_scheduler_fails_closed_before_request(monkeypatch):
    monkeypatch.delenv("RESEARCH_SUPABASE_URL", raising=False)
    monkeypatch.delenv("RESEARCH_SUPABASE_SERVICE_KEY", raising=False)
    called = []

    with pytest.raises(gemini_queue.GeminiQueueUnavailable):
        gemini_queue.generate_content(lambda: called.append(True), call_kind="test")

    assert called == []


def test_shared_reservation_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://supabase.example")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "service-key")
    called = []
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: (_ for _ in ()).throw(
        gemini_queue.GeminiQueueUnavailable("scheduler offline")
    ))

    with pytest.raises(gemini_queue.GeminiQueueUnavailable):
        gemini_queue.generate_content(lambda: called.append(True), call_kind="test")

    assert called == []