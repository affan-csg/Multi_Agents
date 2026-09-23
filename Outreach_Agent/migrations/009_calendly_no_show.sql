-- Migration 009 -- Calendly no-show tracking (Phase 1 dashboard management
-- expansion)
--
-- Run this ONCE in the Outreach project's Supabase SQL editor if that project
-- was provisioned before this migration existed. A freshly provisioned
-- project does not need this file -- schema.sql already has everything
-- below. Safe to run more than once (columns are only added if missing).
--
-- Purely additive: two new nullable/defaulted columns on the existing
-- calendly_events table, no data backfill needed -- existing rows default to
-- no_show=false with a null no-show-uri, which is exactly correct for events
-- synced before this migration (Calendly's own invitee_no_shows flag
-- couldn't have been captured before this integration knew to store it).
-- New/re-synced events populate these going forward.
--
-- (This migration originally also added invitee_reschedule_url -- see
-- migration 010, which drops it now that the reschedule-link feature has
-- been removed entirely.)

ALTER TABLE calendly_events
    ADD COLUMN IF NOT EXISTS no_show     BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS no_show_uri TEXT;
