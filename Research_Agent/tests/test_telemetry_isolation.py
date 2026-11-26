"""Regression tests for offline telemetry isolation.

usage_tracker and diagnostics write on daemon threads. A test that exercises a
provider call site therefore used to keep writing after teardown -- and after a
function-scoped ``csg.db.connect`` monkeypatch was reverted -- straight into the
live ``api_usage`` and ``research_diagnostic_events`` tables. The session-wide
fixture in conftest.py replaces the write indirection for the whole suite; these
tests prove the indirection is what a write actually goes through.
"""

from __future__ import annotations

import threading
import time

import pytest

from csg import diagnostics, usage_tracker


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_usage_rows_are_captured_without_touching_the_database(isolated_usage_rows, monkeypatch):
    connects: list[int] = []

    def fail_connect():
        connects.append(1)
        raise AssertionError("offline test attempted a live database write")

    monkeypatch.setattr("csg.db.connect", fail_connect)

    usage_tracker.record_usage(
        "gemini",
        "enrich_firmographics",
        run_id="isolation-usage",
        domain="isolation.example",
        success=False,
        error_kind="timeout",
    )

    assert _wait_for(
        lambda: any(row.get("run_id") == "isolation-usage" for row in isolated_usage_rows)
    )
    row = next(row for row in isolated_usage_rows if row.get("run_id") == "isolation-usage")
    assert row["domain"] == "isolation.example"
    assert row["error_kind"] == "timeout"
    assert connects == []


def test_diagnostic_events_are_captured_without_touching_the_database(
    isolated_diagnostic_events, monkeypatch
):
    connects: list[int] = []

    def fail_connect():
        connects.append(1)
        raise AssertionError("offline test attempted a live database write")

    monkeypatch.setattr("csg.db.connect", fail_connect)

    diagnostics.log_event(
        "telemetry_isolation_probe",
        run_id="isolation-diagnostics",
        domain="isolation.example",
    )

    assert _wait_for(
        lambda: any(
            event.get("event") == "telemetry_isolation_probe"
            for event in isolated_diagnostic_events
        )
    )
    event = next(
        event
        for event in isolated_diagnostic_events
        if event.get("event") == "telemetry_isolation_probe"
    )
    assert event["run_id"] == "isolation-diagnostics"
    assert connects == []


def test_a_write_after_a_function_scoped_patch_is_removed_still_uses_the_sink(
    isolated_usage_rows,
):
    """The real failure mode was a daemon thread that ran after the test's
    ``csg.db.connect`` patch had already been reverted. The session fixture
    must still route that late write away from the live table."""

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            usage_tracker,
            "_write_row",
            lambda _row: (_ for _ in ()).throw(AssertionError("late write used the real writer")),
        )

    def delayed_write():
        time.sleep(0.05)
        usage_tracker.record_usage(
            "gemini",
            "enrich_firmographics",
            run_id="isolation-outlive",
            domain="isolation.example",
        )

    thread = threading.Thread(target=delayed_write, daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert _wait_for(
        lambda: any(row.get("run_id") == "isolation-outlive" for row in isolated_usage_rows)
    )


def test_the_suite_never_installs_the_live_writers():
    """A module-level fixture, not a per-test patch, is what makes this hold
    for daemon threads and for tests that forget to patch anything."""
    assert usage_tracker._write_row is not usage_tracker._persist_row
    assert diagnostics._write_event is not diagnostics._persist_event
