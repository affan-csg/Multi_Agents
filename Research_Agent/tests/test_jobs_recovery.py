from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import threading
from types import SimpleNamespace

import pytest

from csg import api, jobs
from csg.db import utcnow


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.updated = threading.Event()

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
        if filters.get("worker_id") and not filters.get("status"):
            if filters["worker_id"] != f"eq.{self.row.get('worker_id')}":
                return []
            self.row.update(data)
            self.updated.set()
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

    def update(self, table, data, **filters):
        if filters.get("job_id") != f"eq.{self.row['job_id']}":
            return
        if filters.get("worker_id") and filters["worker_id"] != f"eq.{self.row.get('worker_id')}":
            return
        self.row.update(data)
        self.updated.set()

    def close(self):
        pass


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


def test_running_job_update_renews_only_the_owning_workers_lease():
    now = utcnow()
    row = {
        "job_id": "job-lease", "status": "running", "worker_id": "worker-a",
        "lease_until": (now + timedelta(seconds=1)).isoformat(),
    }
    conn = FakeConn(row)

    jobs.update(conn, "job-lease", worker_id="worker-b", status="running")
    assert row["lease_until"] == (now + timedelta(seconds=1)).isoformat()

    jobs.update(conn, "job-lease", worker_id="worker-a", status="running")
    assert datetime.fromisoformat(row["lease_until"]) > now + timedelta(minutes=5)


def test_discovery_heartbeat_thread_renews_the_persisted_job(monkeypatch):
    now = utcnow()
    row = {
        "job_id": "job-heartbeat", "status": "running", "worker_id": "worker-a",
        "lease_until": (now + timedelta(seconds=1)).isoformat(),
    }
    conn = FakeConn(row)
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(api, "_JOB_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    job = api.Job(job_id="job-heartbeat", kind="discover", status="running", worker_id="worker-a")
    heartbeat = api._start_job_heartbeat(job, "worker-a")

    try:
        assert conn.updated.wait(1)
        assert datetime.fromisoformat(row["lease_until"]) > now + timedelta(minutes=5)
    finally:
        api._stop_job_heartbeat(heartbeat)
    assert not heartbeat[1].is_alive()


def test_reclaimed_worker_cannot_publish_over_current_owner(monkeypatch):
    now = utcnow()
    row = {
        "job_id": "job-owner", "status": "running", "worker_id": "worker-a",
        "lease_until": (now + timedelta(seconds=1)).isoformat(),
        "done": 0, "total": 2,
    }
    conn = FakeConn(row)
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    job = api.Job(
        job_id="job-owner", kind="discover", status="running", done=0, total=2,
        worker_id="worker-a",
    )

    assert job.publish("worker-a", done=1, current_domain="enrich: first.example")
    row["lease_until"] = (utcnow() - timedelta(seconds=1)).isoformat()
    assert jobs.claim(conn, "job-owner", "worker-b")
    job.set_worker_id("worker-b")

    assert not job.publish("worker-a", done=99, current_domain="stale update")
    assert job.done == 1
    assert row["done"] == 1
    assert job.publish("worker-b", done=2, current_domain="pipeline: second.example")
    assert row["done"] == 2


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_discovery_heartbeat_stops_when_worker_exits(monkeypatch, outcome):
    row = {
        "job_id": f"job-{outcome}", "status": "pending", "worker_id": None,
        "lease_until": None, "done": 0, "total": 0,
    }
    conn = FakeConn(row)
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(api.jobs, "worker_id", lambda: "worker-test")
    monkeypatch.setattr(api, "_JOB_HEARTBEAT_INTERVAL_SECONDS", 3600)
    heartbeats = []
    start_heartbeat = api._start_job_heartbeat

    def capture_heartbeat(job, worker_id):
        heartbeat = start_heartbeat(job, worker_id)
        heartbeats.append(heartbeat)
        return heartbeat

    monkeypatch.setattr(api, "_start_job_heartbeat", capture_heartbeat)
    monkeypatch.setattr(api, "load_config", lambda: object())
    job = api.Job(job_id=row["job_id"], kind="discover")
    request = api.DiscoverRequest(limit=2)

    if outcome == "failed":
        monkeypatch.setattr(api, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("config failed")))
    elif outcome == "cancelled":
        async def cancelled_run(*_args, **_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(api.discover_mod, "run_discovery", cancelled_run)
    else:
        async def successful_run(*_args, **_kwargs):
            return SimpleNamespace(run_id="run-1", as_dict=lambda: {"ok": True})

        monkeypatch.setattr(api.discover_mod, "run_discovery", successful_run)

    if outcome == "cancelled":
        api._run_discover_job(job, request)
    else:
        api._run_discover_job(job, request)

    assert len(heartbeats) == 1
    assert not heartbeats[0][1].is_alive()
    if outcome == "done":
        assert row["status"] == "done"
    elif outcome == "failed":
        assert row["status"] == "error"
        assert row["error"] == "RuntimeError: config failed"
    else:
        assert row["status"] == "error"
        assert row["error"] == "Research job was cancelled and can be retried."


def test_recovery_scan_dispatches_persisted_discovery(monkeypatch):
    job_row = {
        "job_id": "job-3", "kind": "discover", "status": "running",
        "request_payload": {"region": "US", "limit": 4}, "run_id": None,
        "lease_until": (utcnow() - timedelta(minutes=5)).isoformat(),
    }

    class ScanConn:
        def select(self, table, **params):
            if params.get("status") == "eq.running":
                return [job_row]
            return []

        def close(self):
            pass

    class CaptureLoop:
        def __init__(self):
            self.calls = []

        def run_in_executor(self, executor, function, *args):
            self.calls.append((function, args))

    loop = CaptureLoop()
    monkeypatch.setattr(api.db, "connect", lambda: ScanConn())
    monkeypatch.setattr(api, "_MAIN_LOOP", loop)
    monkeypatch.setattr(api, "_JOBS", {})

    api._recover_jobs_once()

    assert len(loop.calls) == 1
    function, args = loop.calls[0]
    assert function is api._run_discover_job
    assert args[0].job_id == "job-3"
    assert args[1].region == "US"
    assert args[1].limit == 4
