"""Round-trip tests for the qualification_attempts table (db.py) -- the
ledger that records every LLM qualification attempt, success or failure, so
a failure is queryable/retryable instead of looking identical to "never
attempted" (see migrations/004_qualification_attempts.sql).

Uses a fake in-memory PostgREST (same pattern as test_db_roundtrip.py) so
these run offline against the real db.py code paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from csg import db


class FakeConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def _key(self, table: str, row: dict[str, Any]) -> tuple:
        keys = {
            "qualification_attempts": ("domain", "run_id"),
            "qualifications": ("domain", "run_id"),
        }.get(table, ("domain",))
        return tuple(row.get(k) for k in keys)

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str = "") -> None:
        store = self.tables.setdefault(table, [])
        for row in rows:
            k = self._key(table, row)
            for i, existing in enumerate(store):
                if self._key(table, existing) == k:
                    store[i] = dict(row)
                    break
            else:
                store.append(dict(row))

    def select(self, table: str, select: str = "*", limit: int | None = None,
               order: str | None = None, **filters: str) -> list[dict[str, Any]]:
        rows = self.tables.get(table, [])
        for field, expr in filters.items():
            if expr.startswith("eq."):
                want = expr[3:]
                rows = [r for r in rows if str(r.get(field)) == want]
        return rows[:limit] if limit else list(rows)

    def commit(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def conn():
    return FakeConn()


def test_save_qualification_attempt_records_failure(conn):
    db.save_qualification_attempt(
        conn, "run1", "acme.com", status="failed",
        error_reason="rate_limited", error_detail="RateLimitError: 429",
    )
    row = db.get_qualification_attempt(conn, "acme.com", "run1")
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_reason"] == "rate_limited"
    assert row["attempt_count"] == 1


def test_save_qualification_attempt_increments_attempt_count_on_repeated_failure(conn):
    """A company that fails twice in a row (e.g. two separate qualify runs,
    or a retry that also fails) must show it was actually retried, not just
    overwritten with a fresh attempt_count=1 that hides the history."""
    db.save_qualification_attempt(conn, "run1", "acme.com", status="failed", error_reason="timeout")
    db.save_qualification_attempt(conn, "run1", "acme.com", status="failed", error_reason="timeout")
    db.save_qualification_attempt(conn, "run1", "acme.com", status="failed", error_reason="rate_limited")
    row = db.get_qualification_attempt(conn, "acme.com", "run1")
    assert row["attempt_count"] == 3
    assert row["error_reason"] == "rate_limited"  # latest failure reason wins


def test_save_qualification_attempt_success_after_failure_overwrites_status(conn):
    """A company that failed once and then succeeds on retry must show
    status='succeeded' -- the failure must not linger and keep the company
    looking permanently broken once it's actually fixed."""
    db.save_qualification_attempt(conn, "run1", "acme.com", status="failed", error_reason="timeout")
    db.save_qualification_attempt(conn, "run1", "acme.com", status="succeeded")
    row = db.get_qualification_attempt(conn, "acme.com", "run1")
    assert row["status"] == "succeeded"
    assert row["attempt_count"] == 2


def test_get_qualification_attempts_scopes_to_run(conn):
    db.save_qualification_attempt(conn, "run1", "a.com", status="failed", error_reason="timeout")
    db.save_qualification_attempt(conn, "run2", "b.com", status="failed", error_reason="timeout")
    attempts = db.get_qualification_attempts(conn, "run1")
    assert set(attempts.keys()) == {"a.com"}


def test_get_failed_qualifications_returns_only_failed_status(conn):
    db.save_qualification_attempt(conn, "run1", "failed.com", status="failed", error_reason="timeout")
    db.save_qualification_attempt(conn, "run1", "ok.com", status="succeeded")
    failed = db.get_failed_qualifications(conn, "run1")
    assert [r["domain"] for r in failed] == ["failed.com"]
