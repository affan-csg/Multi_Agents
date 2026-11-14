"""Dedicated long-lived worker service for durable Research jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException

from . import db, jobs
from . import api as research_api
from .diagnostics import log_event

logger = logging.getLogger("csg.worker")
POLL_INTERVAL_SECONDS = float(os.environ.get("RESEARCH_WORKER_POLL_SECONDS", "5"))
WORKER_ENABLED = os.environ.get("RESEARCH_WORKER_ENABLED", "true").lower() not in {"0", "false", "no"}
_WORKER_ID = jobs.worker_id()
_STOP = threading.Event()
_STARTED = False
_ACTIVE_JOB_ID: str | None = None
_LAST_SCAN_AT: str | None = None


def _recoverable_rows() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        return jobs.recoverable(conn)
    finally:
        conn.close()


def _dispatch(row: dict[str, Any]) -> None:
    """Run one persisted request; each runner performs the atomic claim."""
    global _ACTIVE_JOB_ID
    job_id = row["job_id"]
    payload = row.get("request_payload") or {}
    job = research_api.Job(
        job_id=job_id,
        kind=row["kind"],
        run_id=row.get("run_id"),
        status=row.get("status", "pending"),
        done=row.get("done", 0),
        total=row.get("total", 0),
        current_domain=row.get("current_domain"),
        result=row.get("result"),
        error=row.get("error"),
    )
    if row["kind"] == "discover" and not payload.get("domains") and not job.run_id:
        job.run_id = jobs.new_run_id()

    if row["kind"] == "discover" and payload.get("domains"):
        runner = research_api._run_resume_job
        args = (job, row.get("run_id") or payload["run_id"], payload["domains"])
    elif row["kind"] == "discover":
        runner = research_api._run_discover_job
        args = (job, research_api.DiscoverRequest.model_validate(payload))
    elif row["kind"] == "qualify" and "tiers" in payload:
        runner = research_api._run_qualify_job
        request = research_api.QualifyRequest.model_validate(payload)
        if row.get("status") == "running":
            request.force = False
        args = (job, row["run_id"], request)
    elif row["kind"] == "qualify":
        runner = research_api._run_qualify_retry_job
        args = (job, row["run_id"])
    elif row["kind"] == "contacts":
        runner = research_api._run_contacts_job
        request = research_api.ContactsRequest.model_validate(payload)
        if row.get("status") == "running":
            request.force = False
        args = (job, row["run_id"], request)
    else:
        log_event("worker_unsupported_job", job_id=job_id, kind=row.get("kind"), worker_id=_WORKER_ID)
        return

    _ACTIVE_JOB_ID = job_id
    log_event(
        "worker_dispatch",
        job_id=job_id,
        kind=row["kind"],
        worker_id=_WORKER_ID,
        prior_worker_id=row.get("worker_id"),
        prior_status=row.get("status"),
        prior_lease_until=row.get("lease_until"),
    )
    try:
        runner(*args)
    finally:
        _ACTIVE_JOB_ID = None


async def _worker_loop() -> None:
    global _LAST_SCAN_AT
    if not WORKER_ENABLED:
        log_event("worker_disabled", worker_id=_WORKER_ID)
        return
    log_event("worker_started", worker_id=_WORKER_ID, poll_interval_seconds=POLL_INTERVAL_SECONDS)
    while not _STOP.is_set():
        try:
            rows = await asyncio.to_thread(_recoverable_rows)
            _LAST_SCAN_AT = datetime.now(timezone.utc).isoformat()
            for row in rows:
                if _STOP.is_set():
                    break
                await asyncio.to_thread(_dispatch, row)
        except Exception:
            logger.exception("Research worker scan failed")
            log_event("worker_scan_error", worker_id=_WORKER_ID)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    log_event("worker_stopping", worker_id=_WORKER_ID, active_job_id=_ACTIVE_JOB_ID)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _STARTED
    _STOP.clear()
    _STARTED = True
    task = asyncio.create_task(_worker_loop())
    try:
        yield
    finally:
        _STOP.set()
        await task
        _STARTED = False


app = FastAPI(title="CSG Research Worker", lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "worker_enabled": WORKER_ENABLED,
        "worker_id": _WORKER_ID,
        "active_job_id": _ACTIVE_JOB_ID,
        "last_scan_at": _LAST_SCAN_AT,
    }


@app.get("/readyz")
def readyz() -> dict[str, str]:
    if not _STARTED or _STOP.is_set():
        raise HTTPException(status_code=503, detail="worker is not ready")
    return {"status": "ready" if WORKER_ENABLED else "paused"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
