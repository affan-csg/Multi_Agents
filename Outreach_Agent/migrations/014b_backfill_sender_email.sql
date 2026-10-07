-- ONE-TIME BACKFILL -- run by hand, after 014_sender_email.sql.
--
-- Before sender tracking every email went out through the single configured
-- mailbox. Only run this once you have confirmed that is true for ALL history
-- (check that no other mailbox/relay/OUTREACH_FROM_EMAIL value was ever used).
-- Replace the address below with the mailbox that sent everything.
--
-- Rows that already have a sender are never touched; unsent rows stay NULL.
-- Analytics show NULL as "Unknown / legacy" rather than guessing.

UPDATE outreach_items
SET sender_email = 'REPLACE_WITH_CURRENT_FROM_EMAIL'
WHERE sent_at IS NOT NULL
  AND sender_email IS NULL;
