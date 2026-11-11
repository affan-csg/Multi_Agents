from __future__ import annotations

from datetime import timedelta

from outreach import jobs
from outreach.db import utcnow


class FakeConn:
    def __init__(self, row):
        self.row = row

    def select(self, table, **params):
        if params.get("job_id") == f"eq.{self.row['job_id']}":
            return [self.row]
        return []

    def update_returning(self, table, data, **filters):
        if self.row.get("status") != "running":
            return []
        if filters.get("job_id") != f"eq.{self.row['job_id']}":
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
