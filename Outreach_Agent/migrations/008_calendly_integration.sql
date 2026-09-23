-- Migration 008 -- Calendly integration (real API sync + webhook receiver)
--
-- Run this ONCE in the Outreach project's Supabase SQL editor if that project
-- was provisioned before this migration existed. A freshly provisioned
-- project does not need this file -- schema.sql already has everything
-- below. Safe to run more than once (IF NOT EXISTS).
--
-- Purely additive: no existing table is altered, no existing column is
-- dropped, renamed, or retyped. The only write this integration ever makes
-- to a pre-existing table is outreach_items.stage, and only through
-- db.set_stage -- the same audit-trail path (an outreach_events row) every
-- other stage move already uses, with a new actor value ('calendly') so the
-- Bookings/drawer audit trail can always tell a Calendly-confirmed booking
-- apart from one the classifier inferred from reply wording. See
-- src/outreach/calendly/__init__.py for the package this schema backs.
--
-- Reversibility: calendly_events has two foreign keys pointing OUT into
-- existing tables (outreach_items, prospects), both ON DELETE SET NULL, not
-- CASCADE -- deleting a campaign/item never destroys the record that a real
-- booking happened, it just un-links it (the booking then shows as
-- unmatched on the Bookings page). No existing table has a column
-- referencing calendly_events, so `DROP TABLE calendly_events;` is always a
-- clean, isolated removal that touches nothing else.
--
-- ---------------------------------------------------------------------------
-- calendly_events: one row per (event, invitee) pair as reported by either a
-- REST API sync pull (POST /calendly/sync) or a webhook delivery
-- (POST /calendly/webhook, invitee.created/invitee.canceled) -- the raw
-- record of a real Calendly booking, independent of whether it could be
-- matched to a pipeline item by invitee email. event_uri (Calendly's own
-- resource identifier) is the idempotency key: a sync re-run or a webhook
-- re-delivery of the same event updates this one row in place (e.g. status
-- flipping active -> canceled) rather than creating a duplicate -- see
-- src/outreach/calendly/db.py's upsert_event.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendly_events (
    event_uri        TEXT PRIMARY KEY,
    event_uuid        TEXT NOT NULL,
    invitee_uri       TEXT,
    invitee_email     TEXT,
    invitee_name      TEXT,
    status            TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'canceled'
    event_type_name   TEXT,
    starts_at         TIMESTAMPTZ,
    ends_at           TIMESTAMPTZ,
    location          TEXT,   -- join URL / meeting location, when Calendly provides one
    cancel_reason     TEXT,
    source            TEXT NOT NULL DEFAULT 'sync',    -- 'sync' | 'webhook' -- which path most recently wrote this row
    item_id           TEXT REFERENCES outreach_items(item_id) ON DELETE SET NULL,
    research_domain   TEXT REFERENCES prospects(research_domain) ON DELETE SET NULL,
    raw               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendly_events_item   ON calendly_events(item_id);
CREATE INDEX IF NOT EXISTS idx_calendly_events_email  ON calendly_events(invitee_email);
CREATE INDEX IF NOT EXISTS idx_calendly_events_status ON calendly_events(status);
