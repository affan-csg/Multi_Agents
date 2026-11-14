from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from csg import api, contacts, db, discover, pipeline, worker
from csg.db import utcnow
from csg.discovery.base import CompanyCandidate
from csg.models import Company


class SharedJobConn:
    def __init__(self, row):
        self.row = row
        self.lock = threading.Lock()

    def select(self, table, **params):
        if params.get("job_id") == f"eq.{self.row['job_id']}":
            return [self.row]
        return []

    def update_returning(self, table, data, **filters):
        with self.lock:
            if filters.get("job_id") != f"eq.{self.row['job_id']}":
                return []
            status_filter = filters.get("status")
            if status_filter == "eq.pending" and self.row.get("status") == "pending":
                self.row.update(data)
                return [self.row]
            if status_filter == "eq.running":
                lease_filter = filters.get("lease_until", "")
                if lease_filter.startswith("gt."):
                    if filters.get("worker_id") != f"eq.{self.row.get('worker_id')}":
                        return []
                    if self.row.get("lease_until") <= lease_filter[3:]:
                        return []
                    self.row.update(data)
                    return [self.row]
                if self.row.get("status") != "running" or not lease_filter.startswith("lt."):
                    return []
                if self.row.get("lease_until") >= lease_filter[3:]:
                    return []
                self.row.update(data)
                return [self.row]
            if status_filter is None:
                if filters.get("worker_id") != f"eq.{self.row.get('worker_id')}":
                    return []
                self.row.update(data)
                return [self.row]
            return []

    def close(self):
        pass


def _pending_discovery_job():
    return {
        "job_id": "worker-job-1",
        "kind": "discover",
        "run_id": "disc-stable-1",
        "status": "pending",
        "done": 0,
        "total": 1,
        "request_payload": {"region": "US", "limit": 1},
        "lease_until": None,
    }


def test_reclaimed_forced_qualify_job_skips_completed_results(monkeypatch):
    dispatched = []
    monkeypatch.setattr(
        worker.research_api,
        "_run_qualify_job",
        lambda *args: dispatched.append(args),
    )
    row = {
        "job_id": "qualify-recovery",
        "kind": "qualify",
        "run_id": "run-1",
        "status": "running",
        "request_payload": {"tiers": ["A"], "force": True},
    }

    worker._dispatch(row)

    assert dispatched[0][2].force is False


def test_completed_empty_contact_search_is_not_repeated(monkeypatch):
    calls = []

    class Conn:
        def commit(self):
            pass

    async def find_contacts(*_args, **_kwargs):
        calls.append(True)
        return []

    monkeypatch.setattr(db, "has_contacts", lambda _conn, _domain: False)
    monkeypatch.setattr(contacts, "find_contacts", find_contacts)
    monkeypatch.setattr(db, "save_contacts", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(contacts, "refresh_company_assessment_safely", lambda *_args, **_kwargs: None)
    checkpoint = {"status": "done", "payload": {"contacts_saved": 0, "result_count": 0}}

    stats = asyncio.run(contacts.discover_run(
        Conn(), ["acme.example"], force=True,
        checkpoint_load=lambda _key: checkpoint,
        checkpoint_save=lambda *_args: True,
    ))

    assert calls == []
    assert stats["skipped"] == 1


def test_replayed_hunter_quota_checkpoint_stops_remaining_domains(monkeypatch):
    calls = []

    class Conn:
        def commit(self):
            pass

    async def find_contacts(domain, **_kwargs):
        calls.append(domain)
        return []

    monkeypatch.setattr(db, "has_contacts", lambda _conn, _domain: False)
    monkeypatch.setattr(contacts, "find_contacts", find_contacts)
    monkeypatch.setattr(contacts, "refresh_company_assessment_safely", lambda *_args, **_kwargs: None)
    checkpoints = {
        "contacts:quota.example": {
            "status": "failed",
            "payload": {"reason": "Hunter quota exhausted", "error_kind": "quota_exhausted"},
        },
    }

    stats = asyncio.run(contacts.discover_run(
        Conn(), ["quota.example", "later.example"],
        checkpoint_load=lambda key: checkpoints.get(key),
        checkpoint_save=lambda *_args: True,
    ))

    assert calls == []
    assert stats["errors"] == 1


def test_two_workers_competing_for_same_job_execute_once(monkeypatch):
    row = _pending_discovery_job()
    conn = SharedJobConn(row)
    execution_lock = threading.Lock()
    executions = []

    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(api, "_JOB_HEARTBEAT_INTERVAL_SECONDS", 3600)
    worker_ids = iter(("worker-a", "worker-b"))
    monkeypatch.setattr(api.jobs, "worker_id", lambda: next(worker_ids))
    monkeypatch.setattr(api, "load_config", lambda: object())

    async def run_discovery(*_args, **_kwargs):
        with execution_lock:
            executions.append(True)
        return SimpleNamespace(run_id="disc-stable-1", as_dict=lambda: {"scored": 1})

    monkeypatch.setattr(api.discover_mod, "run_discovery", run_discovery)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker._dispatch, dict(row)) for _ in range(2)]
        for future in futures:
            future.result()

    assert len(executions) == 1
    assert row["status"] == "done"
    assert row["run_id"] == "disc-stable-1"


def test_expired_running_job_is_reclaimed_by_worker(monkeypatch):
    now = utcnow()
    row = _pending_discovery_job()
    row.update(
        status="running",
        worker_id="old-worker",
        lease_until=(now - timedelta(seconds=1)).isoformat(),
    )
    conn = SharedJobConn(row)
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(api, "_JOB_HEARTBEAT_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(api.jobs, "worker_id", lambda: "replacement-worker")
    monkeypatch.setattr(api, "load_config", lambda: object())

    async def run_discovery(*_args, **_kwargs):
        return SimpleNamespace(run_id="disc-stable-1", as_dict=lambda: {"scored": 1})

    monkeypatch.setattr(api.discover_mod, "run_discovery", run_discovery)

    worker._dispatch(dict(row))

    assert row["status"] == "done"
    assert row["worker_id"] == "replacement-worker"


def test_completed_enrichment_checkpoint_is_reused_after_restart(monkeypatch):
    candidate = CompanyCandidate(
        domain="acme.example",
        name="Acme",
        source="test",
        discovered_at=datetime.now(timezone.utc),
    )
    checkpoints = {}
    provider_calls = []

    class EmptyCompanyConn:
        def select(self, table, **_params):
            return []

    async def enrich(company):
        provider_calls.append(company.domain)
        return None

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)

    def load_checkpoint(key):
        return checkpoints.get(key)

    def save_checkpoint(key, status, payload):
        checkpoints[key] = {"status": status, "payload": payload}
        return True

    first = asyncio.run(discover._enrich_all(
        EmptyCompanyConn(), [candidate], "US", None,
        checkpoint_load=load_checkpoint, checkpoint_save=save_checkpoint,
    ))
    second = asyncio.run(discover._enrich_all(
        EmptyCompanyConn(), [candidate], "US", None,
        checkpoint_load=load_checkpoint, checkpoint_save=save_checkpoint,
    ))

    assert provider_calls == ["acme.example"]
    assert first[0][0][1].domain == second[0][0][1].domain == "acme.example"


def test_discovery_manifest_restores_candidates_without_provider_search():
    candidate = CompanyCandidate(
        domain="acme.example",
        name="Acme",
        source="gemini_grounded_search",
        source_url="https://example.test/source",
        discovered_at=datetime.now(timezone.utc),
    )
    checkpoint = {
        "status": "done",
        "payload": {
            "provider_name": "gemini_grounded_search",
            "candidates": [candidate.model_dump(mode="json")],
        },
    }

    restored = discover._restore_discovery_manifest(checkpoint)

    assert restored is not None
    provider_name, candidates = restored
    assert provider_name == "gemini_grounded_search"
    assert candidates == [candidate]


def test_recovery_pipeline_skips_domains_already_scored(monkeypatch):
    class Config:
        raw = {"profile": "test"}
        http = {
            "user_agent": "test",
            "per_host_delay_seconds": 0,
            "timeout_seconds": 1,
            "cache_ttl_hours": 1,
            "max_concurrency": 1,
        }

    class Conn:
        def __init__(self):
            self.upserts = []

        def select(self, table, **_params):
            if table == "scores":
                return [{"domain": "acme.example"}]
            return []

        def commit(self):
            pass

    class Fetcher:
        stats = {"hits": 0, "misses": 0}

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    conn = Conn()
    processed = []
    monkeypatch.setattr(pipeline.db, "load_suppression", lambda _conn: set())
    monkeypatch.setattr(pipeline.db, "start_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.db, "upsert_company", lambda _conn, company: conn.upserts.append(company.domain))
    monkeypatch.setattr(pipeline.db, "finish_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "Fetcher", Fetcher)

    async def process(*_args, **_kwargs):
        processed.append(True)

    monkeypatch.setattr(pipeline, "_process_one", process)
    company = Company(domain="acme.example")

    asyncio.run(pipeline.run(Config(), [company], conn, "run-1", skip_scored=True))

    assert processed == []
    assert conn.upserts == []


def test_interrupted_enrichment_is_not_replayed_after_worker_restart(monkeypatch):
    candidate = CompanyCandidate(
        domain="acme.example",
        name="Acme",
        source="test",
        discovered_at=datetime.now(timezone.utc),
    )
    saved = {"status": "started", "payload": {}}
    provider_calls = []

    class EmptyCompanyConn:
        def select(self, table, **_params):
            return []

    async def enrich(company):
        provider_calls.append(company.domain)
        return None

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)

    result = asyncio.run(discover._enrich_all(
        EmptyCompanyConn(), [candidate], "US", None,
        checkpoint_load=lambda _key: saved,
        checkpoint_save=lambda _key, status, payload: saved.update(status=status, payload=payload) is None,
    ))

    assert provider_calls == []
    assert result[2] == {"interrupted": 1}
    assert saved["status"] == "failed"


def test_worker_health_and_readiness_are_available():
    health = worker.healthz()
    assert health["status"] == "ok"
    assert health["worker_id"]
    with pytest.raises(Exception) as exc_info:
        worker.readyz()
    assert getattr(exc_info.value, "status_code", None) == 503


def test_disabled_worker_does_not_claim_jobs(monkeypatch):
    scans = []
    monkeypatch.setattr(worker, "WORKER_ENABLED", False)
    monkeypatch.setattr(worker, "_recoverable_rows", lambda: scans.append(True) or [])

    asyncio.run(worker._worker_loop())

    assert scans == []
    monkeypatch.setattr(worker, "_STARTED", True)
    assert worker.readyz() == {"status": "paused"}
    assert worker.healthz()["worker_enabled"] is False


def test_worker_shutdown_during_enrichment_finishes_active_job_only(monkeypatch):
    row = _pending_discovery_job()
    conn = SharedJobConn(row)
    enrichment_started = threading.Event()
    finish_enrichment = threading.Event()
    dispatches = []

    monkeypatch.setattr(worker, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "_recoverable_rows", lambda: [dict(row), dict(row)])
    monkeypatch.setattr(api.db, "connect", lambda: conn)
    monkeypatch.setattr(api, "_JOB_HEARTBEAT_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(api.jobs, "worker_id", lambda: "worker-shutdown-test")
    monkeypatch.setattr(api, "load_config", lambda: object())
    original_dispatch = worker._dispatch

    def record_dispatch(job_row):
        dispatches.append(job_row["job_id"])
        original_dispatch(job_row)

    monkeypatch.setattr(worker, "_dispatch", record_dispatch)

    async def run_discovery(*_args, **_kwargs):
        enrichment_started.set()
        assert finish_enrichment.wait(timeout=2)
        return SimpleNamespace(run_id="disc-stable-1", as_dict=lambda: {"scored": 1})

    monkeypatch.setattr(api.discover_mod, "run_discovery", run_discovery)

    async def exercise_shutdown():
        worker._STOP.clear()
        context = worker._lifespan(worker.app)
        await context.__aenter__()
        assert await asyncio.to_thread(enrichment_started.wait, 2)
        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        finish_enrichment.set()
        await asyncio.wait_for(shutdown, timeout=2)

    asyncio.run(exercise_shutdown())

    assert dispatches == ["worker-job-1"]
    assert row["status"] == "done"
    assert worker._STARTED is False


def test_pipeline_aborts_before_writes_when_ownership_is_lost(monkeypatch):
    company = Company(domain="acme.example", name="Acme")
    writes = []
    ownership_checks = 0

    async def detect(*_args):
        return None

    async def discover_jobs(*_args):
        return SimpleNamespace(
            ats_provider=None,
            ats_token=None,
            ats_detected_by=None,
            careers_url=None,
            method="unknown",
            source=None,
            hiring_signal="unknown",
            notes=[],
            postings=[],
        )

    def ensure_owned():
        nonlocal ownership_checks
        ownership_checks += 1
        if ownership_checks == 2:
            raise asyncio.CancelledError("lease expired during provider call")

    monkeypatch.setattr(pipeline.ats, "detect", detect)
    monkeypatch.setattr(pipeline, "discover_jobs", discover_jobs)
    monkeypatch.setattr(pipeline.db, "upsert_company", lambda *_args: writes.append("company"))
    monkeypatch.setattr(pipeline.db, "upsert_postings", lambda *_args: writes.append("postings"))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline._process_one(
            None, object(), object(), company, pipeline.Stats(), utcnow(), "run-1", False,
            ensure_owned=ensure_owned,
        ))

    assert ownership_checks == 2
    assert writes == []


def test_discover_api_enqueues_stable_run_id_without_executing(monkeypatch):
    captured = {}

    class Config:
        def resolve_region(self, region):
            return region or "US"

    class NoExecutor:
        def run_in_executor(self, *_args):
            raise AssertionError("API attempted to execute a Research job")

    monkeypatch.setattr(api, "load_config", lambda: Config())
    monkeypatch.setattr(api.jobs, "new_run_id", lambda: "disc-stable-id")

    def create_job(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(job_id="job-stable-id")

    monkeypatch.setattr(api, "_new_job", create_job)
    monkeypatch.setattr(api, "_MAIN_LOOP", NoExecutor())

    result = api.start_discover(api.DiscoverRequest(region="US", limit=7))

    assert result == {"job_id": "job-stable-id", "region": "US"}
    assert captured["kind"] == "discover"
    assert captured["run_id"] == "disc-stable-id"
    assert captured["total"] == 7