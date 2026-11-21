from __future__ import annotations

import threading

from csg import diagnostics


def test_log_event_persists_runtime_context_and_sanitized_details(monkeypatch):
    monkeypatch.setenv("RESEARCH_WORKER_VERSION", "image-sha-123")
    monkeypatch.setattr(diagnostics.logger, "info", lambda *_args, **_kwargs: None)
    persisted = []
    completed = threading.Event()

    def capture(row):
        persisted.append(row)
        completed.set()

    monkeypatch.setattr(diagnostics, "_write_event", capture)

    diagnostics.log_event(
        "gemini_request_failed",
        run_id="disc-test",
        job_id="job-test",
        worker_id="worker-test",
        provider="gemini",
        call_kind="enrich_firmographics",
        domain="acme.example",
        error_details={"message": "quota limit", "api_key": "must-not-persist"},
    )

    assert completed.wait(timeout=1)
    row = persisted[0]
    assert row["worker_version"] == "image-sha-123"
    assert row["run_id"] == "disc-test"
    assert row["job_id"] == "job-test"
    assert row["provider"] == "gemini"
    assert row["details"]["error_details"] == {"message": "quota limit"}


def test_worker_version_has_implementation_fallback(monkeypatch):
    monkeypatch.delenv("RESEARCH_WORKER_VERSION", raising=False)
    monkeypatch.delenv("K_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)

    assert diagnostics.worker_version() == "gemini-only-shared-queue-v1"
