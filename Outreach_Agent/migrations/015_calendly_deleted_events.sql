-- ---------------------------------------------------------------------------
-- Migration 015 -- persistent tombstones for deleted Calendly bookings
--
-- Deleting a booking from the Dashboard removes its calendly_events row, but
-- Calendly still returns that event on every later sync (canceled events are
-- listed too) and via webhooks, so it was recreated. A row here means "the
-- user deleted this event: never store it again". Run ONCE in the Supabase
-- SQL editor; safe to re-run. Also appended to schema.sql.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendly_deleted_events (
    event_uri  TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL
);
