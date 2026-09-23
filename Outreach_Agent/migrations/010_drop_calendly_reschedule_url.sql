-- Migration 010 -- drop the Calendly reschedule-link column
--
-- The "Copy reschedule link" feature (invitee_reschedule_url on
-- calendly_events, added by migration 009) has been removed from the
-- product entirely -- Calendly's API has no reschedule endpoint, so this
-- was just a hosted-page link, and it's been dropped from the UI. This
-- migration removes the now-unused column.
--
-- Run this ONCE in the Outreach project's Supabase SQL editor. Safe to run
-- more than once.

ALTER TABLE calendly_events
    DROP COLUMN IF EXISTS invitee_reschedule_url;
