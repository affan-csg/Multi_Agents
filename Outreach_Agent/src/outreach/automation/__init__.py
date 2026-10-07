"""Automatic polling: inbox + Calendly sync on an interval, then the
Conversation Agent's follow-up schedule.

    runner.py  -- run_cycle(): one polling cycle, guarded by a DB-backed lease
    loop.py    -- run_forever(): the in-process scheduler (opt-in, see
                  AUTO_SYNC_ENABLED) that calls run_cycle when it is due
    routes.py  -- status / interval / run-now endpoints

The scheduler is just a trigger. Everything it needs to survive a restart --
the next run time, the last successful syncs, the lease, the follow-up ledger
-- lives in the database (automation_state, followup_attempts), so any trigger
(this loop, `outreach automation run-once` from cron, the API button) is safe
to run at any time, on any number of replicas.
"""
