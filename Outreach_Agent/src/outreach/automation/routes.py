"""Automation endpoints: read the polling status, change the interval, or run
a cycle now. Included into the Outreach app by api.py; mounted by Server/app.py
under /outreach, so these are /outreach/automation/*."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import load_config
from ..db import utcnow
from . import runner

router = APIRouter()


def status_payload(conn: Any, now: Optional[Any] = None) -> dict[str, Any]:
    now = now or utcnow()
    state = runner.ensure_state(conn)
    locked = state.locked_until is not None and runner._naive(state.locked_until) > now
    payload = state.model_dump(mode="json")
    payload.pop("locked_by", None)
    payload.update({
        "scheduler_enabled": runner.scheduler_enabled(),
        "running": locked,
        "due": runner.is_due(state, now),
        "min_interval_minutes": runner.MIN_INTERVAL_MINUTES,
        "max_interval_minutes": runner.MAX_INTERVAL_MINUTES,
        "now": now.isoformat(),
    })
    return payload


class AutomationSettingsRequest(BaseModel):
    interval_minutes: Optional[int] = None
    enabled: Optional[bool] = None


@router.get("/automation/status")
def automation_status() -> dict[str, Any]:
    conn = db.connect()
    try:
        return status_payload(conn)
    finally:
        conn.close()


@router.patch("/automation/settings")
def automation_settings(req: AutomationSettingsRequest) -> dict[str, Any]:
    conn = db.connect()
    try:
        if req.interval_minutes is not None:
            if not runner.MIN_INTERVAL_MINUTES <= req.interval_minutes <= runner.MAX_INTERVAL_MINUTES:
                raise HTTPException(
                    400,
                    f"interval_minutes must be between {runner.MIN_INTERVAL_MINUTES} "
                    f"and {runner.MAX_INTERVAL_MINUTES}",
                )
            state = runner.ensure_state(conn)
            fields: dict[str, Any] = {"interval_minutes": req.interval_minutes}
            # Re-anchor the next run to the new interval, measured from the
            # last start, so shortening the interval takes effect promptly.
            if state.last_started_at is not None:
                started = runner._naive(state.last_started_at)
                fields["next_run_at"] = started + timedelta(minutes=req.interval_minutes)
            db.update_automation_state(conn, runner.JOB, **fields)
        if req.enabled is not None:
            state = runner.ensure_state(conn)
            fields = {"enabled": req.enabled}
            if req.enabled and not state.enabled:
                fields["next_run_at"] = utcnow()  # resume: first cycle is due now
            db.update_automation_state(conn, runner.JOB, **fields)
        return status_payload(conn)
    finally:
        conn.close()


@router.post("/automation/run")
def automation_run() -> dict[str, Any]:
    """Run one cycle now. Synchronous, like the existing manual buttons; goes
    through the same lease, so it cannot overlap a scheduled cycle."""
    conn = db.connect()
    try:
        result = runner.run_cycle(conn, load_config())
        return {"result": result.as_dict(), "status": status_payload(conn)}
    finally:
        conn.close()
