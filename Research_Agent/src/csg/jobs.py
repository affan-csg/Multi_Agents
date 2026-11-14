"""Durable job state and conditional worker claims for Supabase-backed jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import uuid
from typing import Any, Optional

from .db import SupabaseConn, utcnow

TABLE = "agent_jobs"
CHECKPOINT_TABLE = "agent_job_checkpoints"
LEASE_SECONDS = 600


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def create(
    conn: SupabaseConn,
    *,
    kind: str,
    payload: dict[str, Any],
    run_id: Optional[str] = None,
    total: int = 0,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = utcnow()
    row = {
        "job_id": job_id, "kind": kind, "run_id": run_id,
        "status": "pending", "done": 0, "total": total,
        "request_payload": payload, "idempotency_key": idempotency_key,
        "created_at": _iso(now), "updated_at": _iso(now),
    }
    if idempotency_key:
        existing = getattr(conn, "select", None)
        if callable(existing):
            rows = conn.select(TABLE, idempotency_key=f"eq.{idempotency_key}", limit=1)
            if rows:
                rows[0]["_created"] = False
                return rows[0]
    insert = getattr(conn, "insert_if_absent", None)
    if callable(insert):
        result = conn.insert_if_absent(
            TABLE, [row], "idempotency_key" if idempotency_key else "job_id",
        )
        if result:
            result[0]["_created"] = True
            return result[0]
        if idempotency_key:
            rows = conn.select(TABLE, idempotency_key=f"eq.{idempotency_key}", limit=1)
            if rows:
                rows[0]["_created"] = False
                return rows[0]
    row["_created"] = True
    return row


def get(conn: SupabaseConn, job_id: str) -> Optional[dict[str, Any]]:
    rows = conn.select(TABLE, job_id=f"eq.{job_id}", limit=1)
    return rows[0] if rows else None


def owns(conn: SupabaseConn, job_id: str, worker_id: str) -> bool:
    row = get(conn, job_id)
    if not row or row.get("status") != "running" or row.get("worker_id") != worker_id:
        return False
    lease_until = row.get("lease_until")
    if not lease_until:
        return False
    if isinstance(lease_until, str):
        lease_until = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
    return lease_until > utcnow()


def recoverable(conn: SupabaseConn, limit: int = 50) -> list[dict[str, Any]]:
    now = utcnow()
    pending = conn.select(
        TABLE, status="eq.pending", updated_at=f"lt.{_iso(now)}",
        order="created_at.asc", limit=limit,
    )
    stale = conn.select(
        TABLE, status="eq.running", lease_until=f"lt.{_iso(now)}",
        order="lease_until.asc", limit=limit,
    )
    return [*pending, *stale]


def claim(conn: SupabaseConn, job_id: str, worker_id: str) -> bool:
    now = utcnow()
    payload = {
        "status": "running",
        "worker_id": worker_id,
        "started_at": _iso(now),
        "lease_until": _iso(now + timedelta(seconds=LEASE_SECONDS)),
        "updated_at": _iso(now),
    }
    rows = conn.update_returning(
        TABLE,
        payload,
        job_id=f"eq.{job_id}", status="eq.pending",
    )
    if rows:
        return True
    rows = conn.update_returning(
        TABLE,
        payload,
        job_id=f"eq.{job_id}", status="eq.running", lease_until=f"lt.{_iso(now)}",
    )
    return bool(rows)


def cancel(conn: SupabaseConn, job_id: str) -> bool:
    """Atomically cancel a job that has not already reached a terminal state."""
    now = utcnow()
    rows = conn.update_returning(
        TABLE,
        {
            "status": "cancelled",
            "error": "Cancelled by user.",
            "finished_at": _iso(now),
            "lease_until": None,
            "updated_at": _iso(now),
        },
        job_id=f"eq.{job_id}",
        status="in.(pending,running)",
    )
    return bool(rows)


def requeue(conn: SupabaseConn, job_id: str, worker_id: str, *, delay_seconds: float) -> bool:
    """Return a live owned job to pending after a retryable downstream failure."""
    now = utcnow()
    rows = conn.update_returning(
        TABLE,
        {
            "status": "pending",
            "worker_id": None,
            "lease_until": None,
            "error": None,
            "updated_at": _iso(now + timedelta(seconds=max(0.0, delay_seconds))),
        },
        job_id=f"eq.{job_id}",
        status="eq.running",
        worker_id=f"eq.{worker_id}",
        lease_until=f"gt.{_iso(now)}",
    )
    return bool(rows)


def update(conn: SupabaseConn, job_id: str, worker_id: str, **fields: Any) -> bool:
    now = utcnow()
    fields["updated_at"] = _iso(now)
    if fields.get("status") in ("done", "error"):
        fields["finished_at"] = _iso(now)
        fields["lease_until"] = None
    elif fields.get("status") == "running":
        fields["lease_until"] = _iso(now + timedelta(seconds=LEASE_SECONDS))
    rows = conn.update_returning(
        TABLE, fields, job_id=f"eq.{job_id}", worker_id=f"eq.{worker_id}",
        status="eq.running", lease_until=f"gt.{_iso(now)}",
    )
    return bool(rows)


def get_checkpoint(conn: SupabaseConn, job_id: str, checkpoint_key: str) -> Optional[dict[str, Any]]:
    rows = conn.select(
        CHECKPOINT_TABLE,
        job_id=f"eq.{job_id}",
        checkpoint_key=f"eq.{checkpoint_key}",
        limit=1,
    )
    return rows[0] if rows else None


def list_checkpoints(conn: SupabaseConn, job_id: str) -> list[dict[str, Any]]:
    return conn.select(CHECKPOINT_TABLE, job_id=f"eq.{job_id}", order="checkpoint_key.asc")


def save_checkpoint(
    conn: SupabaseConn,
    job_id: str,
    worker_id: str,
    checkpoint_key: str,
    *,
    status: str,
    payload: Optional[dict[str, Any]] = None,
) -> bool:
    """Persist a checkpoint only while this worker still owns a live lease."""
    result = conn.rpc("save_agent_job_checkpoint", {
        "p_job_id": job_id,
        "p_checkpoint_key": checkpoint_key,
        "p_worker_id": worker_id,
        "p_status": status,
        "p_payload": payload or {},
    })
    return result is True


def worker_id() -> str:
    return f"{os.getpid()}:{uuid.uuid4()}"


def new_run_id() -> str:
    return f"disc-{uuid.uuid4().hex}"
