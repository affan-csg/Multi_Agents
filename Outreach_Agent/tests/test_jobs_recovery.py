from __future__ import annotations

from datetime import datetime, timedelta

from outreach import api, jobs
from outreach.db import utcnow


class FakeConn:
    def __init__(self, row):
        self.row = row

    def select(self, table, **params):
        if params.get("job_id") == f"eq.{self.row['job_id']}":
            return [self.row]
        return []

    def update_returning(self, table, data, **filters):
        if filters.get("job_id") != f"eq.{self.row['job_id']}":
            return []
        if self.row.get("status") == "pending" and filters.get("status") == "eq.pending":
            self.row.update(data)
            return [self.row]
        if self.row.get("status") != "running" or filters.get("status") != "eq.running":
            return []
        lease_filter = filters.get("lease_until", "")
        if not lease_filter.startswith("lt."):
            return []
        if datetime.fromisoformat(self.row["lease_until"]) >= datetime.fromisoformat(lease_filter[3:]):
            return []
        self.row.update(data)
        self.row["status"] = "running"
        return [self.row]


def test_claim_reclaims_expired_running_job():
    now = utcnow()
    row = {
        "job_id": "job-1",
        "status": "running",
        "worker_id": "worker-a",
        "lease_until": (now - timedelta(minutes=5)).isoformat(),
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "updated_at": now.isoformat(),
    }

    conn = FakeConn(row)

    assert jobs.claim(conn, "job-1", "worker-b") is True
    assert row["worker_id"] == "worker-b"
    assert row["status"] == "running"
    assert row["lease_until"] != (now - timedelta(minutes=5)).isoformat()


def test_claim_does_not_reclaim_unexpired_running_job():
    now = utcnow()
    row = {
        "job_id": "job-2",
        "status": "running",
        "worker_id": "worker-a",
        "lease_until": (now + timedelta(minutes=5)).isoformat(),
    }

    assert jobs.claim(FakeConn(row), "job-2", "worker-b") is False
    assert row["worker_id"] == "worker-a"


def test_recovery_scan_fails_closed_for_stale_send_job(monkeypatch):
    job_row = {
        "job_id": "job-3", "kind": "send", "status": "running",
        "campaign_id": "campaign-1", "request_payload": {"campaign_id": "campaign-1"},
        "lease_until": (utcnow() - timedelta(minutes=5)).isoformat(),
    }
    updates = []

    class ScanConn:
        def select(self, table, **params):
            if params.get("status") == "eq.running":
                return [job_row]
            return []

        def update(self, table, data, **filters):
            updates.append((data, filters))

        def close(self):
            pass

    class CaptureLoop:
        def run_in_executor(self, executor, function, *args):
            raise AssertionError("stale sends must not be replayed automatically")

    monkeypatch.setattr(api.db, "connect", lambda: ScanConn())
    monkeypatch.setattr(api, "_MAIN_LOOP", CaptureLoop())
    monkeypatch.setattr(api, "_JOBS", {})

    api._recover_jobs_once()

    assert updates
    assert updates[0][0]["status"] == "error"
    assert "manual reconciliation" in updates[0][0]["error"]
