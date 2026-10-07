-- Per-send sender attribution for Analytics.
--
-- outreach_items.sender_email is the mailbox that actually sent the item's
-- first email. It is stamped by the send pipeline at send time; follow-ups and
-- replies for the item continue from it (their conversation_messages.from_email
-- carries the same address). Additive and nullable: nothing existing reads it
-- except the new analytics endpoints.

ALTER TABLE outreach_items ADD COLUMN IF NOT EXISTS sender_email TEXT;
CREATE INDEX IF NOT EXISTS idx_items_sender ON outreach_items(sender_email);
