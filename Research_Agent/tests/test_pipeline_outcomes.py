from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from csg import pipeline
from csg.models import Company, JobPosting, ScoreResult


class _Fetcher:
    stats = {"hits": 0, "misses": 0}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class _Conn:
    def __init__(self):
        self.statuses = []

    def select(self, table, **_params):
        return []

    def update(self, table, values, **_filters):
        self.statuses.append((table, values))

    def commit(self):
        pass


def _async_value(value):
    async def result(*_args, **_kwargs):
        return value
    return result


def _run_pipeline(monkeypatch, *, hiring_signal, postings, score_error=None):
    conn = _Conn()
    job_result = SimpleNamespace(
        ats_provider=None,
        ats_token=None,
        ats_detected_by=None,
        careers_url=None,
        method="unknown" if hiring_signal == "unknown" else "careers_page",
        source="careers page" if postings else None,
        hiring_signal=hiring_signal,
        notes="no current job source" if hiring_signal == "unknown" else None,
        postings=postings,
    )
    monkeypatch.setattr(pipeline.ats, "detect", _async_value(None))
    monkeypatch.setattr(pipeline, "discover_jobs", _async_value(job_result))
    monkeypatch.setattr(pipeline, "Fetcher", _Fetcher)
    monkeypatch.setattr(pipeline.db, "load_suppression", lambda _conn: set())
    monkeypatch.setattr(pipeline.db, "start_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "finish_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "upsert_company", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "upsert_postings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "save_score", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "set_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "is_validated_current_posting", lambda _posting: True)
    monkeypatch.setattr(pipeline, "refresh_company_assessment_safely", lambda *_args, **_kwargs: None)

    def evaluate(_cfg, company, _postings, _now):
        if score_error:
            raise score_error
        return ScoreResult(domain=company.domain, score=50, tier="C")

    monkeypatch.setattr(pipeline, "evaluate", evaluate)
    cfg = SimpleNamespace(
        http={"user_agent": "test", "per_host_delay_seconds": 0, "timeout_seconds": 1,
              "cache_ttl_hours": 1, "max_concurrency": 1},
        raw={"profile": "test"},
        classify=lambda _title: [],
    )
    stats = asyncio.run(pipeline.run(cfg, [Company(domain="acme.example")], conn, "run-1"))
    return stats


def test_missing_job_data_is_reported_but_does_not_skip_scoring(monkeypatch):
    stats = _run_pipeline(monkeypatch, hiring_signal="unknown", postings=[])

    assert stats.scored == 1
    assert stats.failed == 0
    assert stats.company_outcomes == [{
        "domain": "acme.example",
        "outcome": "scored",
        "reason": "score_created",
        "job_data_available": False,
        "job_data_reason": "no_confirmed_job_signal",
        "job_discovery_method": "unknown",
        "hiring_signal": "unknown",
        "posting_count": 0,
    }]


def test_successful_scoring_reports_usable_job_data(monkeypatch):
    posting = JobPosting(
        domain="acme.example", external_id="job-1", title="Data Engineer",
    )
    stats = _run_pipeline(monkeypatch, hiring_signal="confirmed", postings=[posting])

    assert stats.scored == 1
    assert stats.company_outcomes[0]["outcome"] == "scored"
    assert stats.company_outcomes[0]["job_data_available"] is True
    assert stats.company_outcomes[0]["posting_count"] == 1
    assert stats.company_outcomes[0]["job_data_reason"] is None


def test_scoring_exception_is_reported_as_company_failure(monkeypatch):
    stats = _run_pipeline(
        monkeypatch, hiring_signal="unknown", postings=[],
        score_error=ValueError("invalid score input"),
    )

    assert stats.scored == 0
    assert stats.failed == 1
    assert stats.company_outcomes == [{
        "domain": "acme.example",
        "outcome": "failed",
        "reason": "pipeline_error",
        "error": "ValueError: invalid score input",
    }]