-- Migration 001 -- Conversation Agent (Agent 3)
--
-- Run this ONCE in the Outreach project's Supabase SQL editor if that
-- project was provisioned from schema.sql BEFORE the Conversation Agent
-- existed. A freshly-provisioned project does not need this file at all --
-- schema.sql already contains everything below.
--
-- Safe to run more than once: every statement is IF NOT EXISTS /
-- ADD COLUMN IF NOT EXISTS, and none of them touch or re-key an existing
-- table. No data is read, rewritten, or deleted by this migration.

CREATE TABLE IF NOT EXISTS conversation_settings (
    campaign_id         TEXT PRIMARY KEY REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
    enabled             BOOLEAN NOT NULL DEFAULT false,
    auto_reply          BOOLEAN NOT NULL DEFAULT false,
    followup_after_days INT NOT NULL DEFAULT 3,
    max_followups       INT NOT NULL DEFAULT 2,
    updated_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id          TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES outreach_items(item_id) ON DELETE CASCADE,
    direction           TEXT NOT NULL,
    provider_message_id TEXT,
    in_reply_to         TEXT,
    subject             TEXT,
    body                TEXT,
    from_email          TEXT,
    to_email            TEXT,
    intent              TEXT,
    draft_pending       BOOLEAN NOT NULL DEFAULT false,
    sent_at             TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    UNIQUE (item_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_msgs_item      ON conversation_messages(item_id);
CREATE INDEX IF NOT EXISTS idx_conv_msgs_direction ON conversation_messages(direction);

ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS followup_count         INT NOT NULL DEFAULT 0;
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS last_inbound_at        TIMESTAMPTZ;
ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS conversation_locked_at TIMESTAMPTZ;
