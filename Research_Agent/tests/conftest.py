from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def disable_external_gemini_scheduler(monkeypatch, request):
    if Path(request.node.path).name == "test_gemini_queue.py":
        return
    from csg import gemini_queue
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {"wait_seconds": 0, "current_rpm": 0.5})
    monkeypatch.setattr(gemini_queue, "_record", lambda *_args, **_kwargs: {"current_rpm": 0.5})


# --- Provider telemetry isolation -------------------------------------------
# usage_tracker and diagnostics write through a daemon thread per event. A
# test that exercises a provider call site therefore keeps writing after the
# test (and after any function-scoped db monkeypatch) is torn down, which is
# how fake domains like acme.com/slow.example ended up in the live api_usage
# and research_diagnostic_events tables. Isolation has to live for the whole
# session and be checked when the write happens, not installed per test.
#
# Tests that assert on the persisted row opt back into the real writer
# explicitly (`_persist_row` / `_persist_event`) inside their own fixture.

@pytest.fixture(scope="session")
def isolated_usage_rows():
    from csg import usage_tracker

    rows: list[dict] = []
    original = usage_tracker._write_row
    usage_tracker._write_row = rows.append
    try:
        yield rows
    finally:
        usage_tracker._write_row = original


@pytest.fixture(scope="session")
def isolated_diagnostic_events():
    from csg import diagnostics

    events: list[dict] = []
    original = diagnostics._write_event
    diagnostics._write_event = events.append
    try:
        yield events
    finally:
        diagnostics._write_event = original


@pytest.fixture(autouse=True)
def isolate_provider_telemetry(isolated_usage_rows, isolated_diagnostic_events):
    """Offline tests must never write provider telemetry to a live database."""
    return None