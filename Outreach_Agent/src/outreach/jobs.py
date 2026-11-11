"""Durable job state and conditional worker claims for Supabase-backed jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import uuid
from typing import Any, Optional

from .db import SupabaseConn, utcnow

TABLE = "agent_jobs"
LEASE_SECONDS = 120


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def create(
    conn: SupabaseConn,
    *,
    kind: str,
    payload: dict[str, Any],
    campaign_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = utcnow()
    row = {
        "job_id": job_id, "kind": kind, "campaign_id": campaign_id,
        "status": "pending", "done": 0, "total": 0,
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
    insert = getattr(conn, "insert", None)
    if callable(insert):
        result = conn.insert(TABLE, [row])
        if result:
            result[0]["_created"] = True
            return result[0]
    row["_created"] = True
    return row


def get(conn: SupabaseConn, job_id: str) -> Optional[dict[str, Any]]:
    rows = conn.select(TABLE, job_id=f"eq.{job_id}", limit=1)
    return rows[0] if rows else None


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


def update(conn: SupabaseConn, job_id: str, **fields: Any) -> None:
    fields["updated_at"] = _iso(utcnow())
    if fields.get("status") in ("done", "error"):
        fields["finished_at"] = _iso(utcnow())
        fields["lease_until"] = None
    conn.update(TABLE, fields, job_id=f"eq.{job_id}")


def worker_id() -> str:
    return f"{os.getpid()}:{uuid.uuid4()}"
