"""Tests for qualify.py's prompt-context building -- pure functions, no
network/LLM calls needed (backend selection and the actual API calls are
exercised only by hand against a live key, same as personalize.py's LLM
calls in the Outreach Agent).

Regression cover for the bug class where a company's job signal is
genuinely unknown (every discovery method exhausted -- see
jobs_discovery.py) but the qualification LLM's context made that
indistinguishable from "confirmed: this company has zero open reqs",
letting the model confidently (and wrongly) write "not currently hiring"
into pain_summary/recommended_angle.
"""

from __future__ import annotations

import asyncio

import httpx

from csg.qualify import _build_context, _render_prompt
from csg.qualify import _complete_with_retry
from csg.usage_tracker import classify_error


def _row(**overrides) -> dict:
    base = {
        "domain": "acme.com",
        "name": "Acme Data",
        "employee_count": 400,
        "industry": "Software",
        "tier": "B",
        "score": 55.0,
        "dimensions": {"Hiring Need": 0.0},
        "explanation": ["+0 Hiring Need: unavailable"],
        "metrics": {"req_count": 0, "top_titles": ["ignored"]},
    }
    base.update(overrides)
    return base


def test_context_carries_confirmed_hiring_signal():
    ctx = _build_context(_row(hiring_signal="confirmed"), [])
    assert ctx["hiring_signal"] == "confirmed"


def test_context_carries_unknown_hiring_signal():
    ctx = _build_context(_row(hiring_signal="unknown"), [])
    assert ctx["hiring_signal"] == "unknown"


def test_context_defaults_missing_hiring_signal_to_unknown():
    """A row with no hiring_signal key at all (e.g. a pre-migration score
    row) must default to the conservative "unknown" reading, never silently
    read as "confirmed" -- that would be the same bug this fix closes, just
    moved to a different blind spot."""
    row = _row()
    row.pop("hiring_signal", None)
    ctx = _build_context(row, [])
    assert ctx["hiring_signal"] == "unknown"


def test_prompt_renders_with_zero_reqs_and_unknown_signal():
    """End-to-end sanity: the actual prompt text sent to the LLM must not
    crash and must be built from context that includes the signal (the
    template itself references {context_json}, which _render_prompt dumps
    the whole context into -- see qualify.md)."""
    prompt = _render_prompt(_row(hiring_signal="unknown"), [])
    assert '"hiring_signal": "unknown"' in prompt


def test_retry_honors_retry_after_and_recovers(monkeypatch):
    calls = {"count": 0}
    delays: list[float] = []

    class RateLimited(RuntimeError):
        def __init__(self):
            self.response = httpx.Response(429, headers={"Retry-After": "7"})

    class Backend:
        model = "test"

        async def complete(self, prompt, *, run_id=None, domain=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RateLimited()
            return type("Result", (), {"pain_summary": "ok", "recommended_angle": "ok"})()

        def is_retryable(self, exc):
            return True

    monkeypatch.setattr("csg.qualify.random.uniform", lambda *_: 0.0)
    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("csg.qualify.asyncio.sleep", record_sleep)
    result = asyncio.run(_complete_with_retry(Backend(), "prompt", run_id="run-1", domain="acme.com"))

    assert result.pain_summary == "ok"
    assert calls["count"] == 2
    assert delays == [7.0]


def test_malformed_qualification_is_parse_error():
    assert classify_error(ValueError("invalid JSON response")) == "parse_error"
