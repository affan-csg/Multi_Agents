"""The in-process scheduler: a coroutine that wakes every minute, asks the
database whether a cycle is due, and runs one if so.

It holds no schedule of its own. `automation_state.next_run_at` is the
schedule, so a restart or redeploy picks up exactly where the last process
left off instead of restarting a 30-minute countdown (or, worse, firing
immediately on every boot). The in-process asyncio.Lock only avoids queuing a
second cycle behind a running one in THIS process; the DB lease in
runner.run_cycle is what prevents overlap across processes and replicas.

Opt-in: Server/app.py starts this only when AUTO_SYNC_ENABLED=true.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .. import db
from ..config import load_config
from ..db import utcnow
from . import runner

logger = logging.getLogger("outreach.automation")

CHECK_EVERY_SECONDS = 60


def _run_if_due() -> Optional[runner.CycleResult]:
    """Blocking: runs in the executor, never on the event loop."""
    conn = db.connect()
    try:
        state = runner.ensure_state(conn)
        if not state.enabled or not runner.is_due(state, utcnow()):
            return None
        return runner.run_cycle(conn, load_config())
    finally:
        conn.close()


async def run_forever(
    stop: asyncio.Event, *, check_every: float = CHECK_EVERY_SECONDS,
) -> None:
    lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    logger.info("automation scheduler started (checks every %ss)", check_every)
    while not stop.is_set():
        try:
            if not lock.locked():
                async with lock:
                    await loop.run_in_executor(None, _run_if_due)
        except Exception:  # the scheduler itself must never die
            logger.exception("automation scheduler iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_every)
        except asyncio.TimeoutError:
            pass
    logger.info("automation scheduler stopped")
