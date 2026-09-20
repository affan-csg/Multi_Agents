"""Match an inbound email back to the outreach item it is answering.

Four strategies, strongest evidence first. Each is a strict narrowing of the
candidate set -- never a guess across campaigns:

  1. In-Reply-To / References vs. the item's stored provider_message_id.
     This is the RFC 5322 threading chain and is definitive: the prospect's
     mail client is telling us precisely which of our messages it answers.

  2. In-Reply-To / References vs. a previously stored outbound message in
     conversation_messages. Same certainty as (1), but resolves replies to a
     follow-up or to an earlier reply rather than to the original outreach.

  3. Sender address vs. the item's to_email. The fallback for mail clients
     that drop the threading headers (some mobile clients, and most
     "forwarded to a colleague" cases). Restricted to items already in a
     conversation stage, so it can never attach a reply to work the Outreach
     Agent still owns.

  4. Reply-To address vs. the item's to_email. A scheduling tool's booking
     notification (Calendly confirmed, observed directly in this project's
     own testing) is FROM the tool itself and TO the account that owns the
     calendar -- neither header names the prospect at all, so strategy 3
     cannot see them. But the tool puts the actual invitee's address in
     Reply-To specifically so a reply to the notification reaches the
     person who booked, and that is exactly the signal this strategy reads.
     Without it, a real "they booked a call" confirmation is structurally
     invisible to every earlier strategy and the card would never move to
     Booked on its own.

If more than one item matches on address alone -- the same person emailed in
two campaigns -- the most recently sent one wins, because that is the message
they are overwhelmingly likely to be replying to. When nothing matches at
all the email is left alone: an unmatched message is almost always ordinary
mail that happens to be in the same mailbox, and inventing a match would
move a card for a conversation that never happened.
"""

from __future__ import annotations

from typing import Optional

from ..models import OutreachItem
from .inbox import InboundEmail


def _thread_ids(email_in: InboundEmail) -> list[str]:
    """Every message id this reply claims to be answering, In-Reply-To first
    (the direct parent) then References (the wider chain)."""
    ids: list[str] = []
    if email_in.in_reply_to:
        ids.append(email_in.in_reply_to.strip())
    ids.extend(ref.strip() for ref in email_in.references if ref.strip())
    return ids


def _normalize(message_id: Optional[str]) -> str:
    """Strip the angle brackets around a Message-ID.

    Providers are inconsistent about whether they store or return them, so
    `<abc@x>` and `abc@x` must compare equal or threading silently fails."""
    if not message_id:
        return ""
    return message_id.strip().strip("<>").strip().lower()


def match_to_item(
    email_in: InboundEmail,
    items: list[OutreachItem],
    outbound_index: Optional[dict[str, str]] = None,
) -> Optional[OutreachItem]:
    """Find the item this email replies to, or None.

    `items` must already be restricted to conversation-stage items of
    campaigns with the agent enabled -- this function does not re-check
    stages, it trusts its caller's query (see db.list_conversation_items).

    `outbound_index` maps a normalized provider_message_id to an item_id for
    every outbound message already recorded, which is what lets a reply to a
    follow-up resolve correctly (strategy 2)."""
    if not items:
        return None

    by_id = {item.item_id: item for item in items}
    thread_ids = [_normalize(tid) for tid in _thread_ids(email_in)]

    # 1. Threading headers against the original outreach send.
    if thread_ids:
        by_provider_id = {
            _normalize(item.provider_message_id): item
            for item in items
            if item.provider_message_id
        }
        for tid in thread_ids:
            if tid and tid in by_provider_id:
                return by_provider_id[tid]

        # 2. Threading headers against any previously recorded outbound
        #    message -- a reply to a follow-up rather than to the original.
        if outbound_index:
            for tid in thread_ids:
                item_id = outbound_index.get(tid)
                if item_id and item_id in by_id:
                    return by_id[item_id]

    # 3. Sender address, for clients that dropped the threading headers.
    sender = (email_in.from_email or "").strip().lower()
    if sender:
        found = _match_by_address(items, sender)
        if found:
            return found

    # 4. Reply-To address, for a scheduling tool's booking notification --
    #    see the module docstring. Only reached once strategy 3 has already
    #    failed, since a genuine reply's Reply-To (when a client even sets
    #    one) is normally identical to its From and adds nothing beyond it.
    reply_to = (email_in.reply_to or "").strip().lower()
    if reply_to and reply_to != sender:
        found = _match_by_address(items, reply_to)
        if found:
            return found

    return None


def _match_by_address(items: list[OutreachItem], address: str) -> Optional[OutreachItem]:
    """The item(s) whose to_email matches `address`, resolved to one.

    Shared by strategies 3 and 4 -- both are "an email address identifies
    the prospect" matches, they just read that address off a different
    header, so the same-person-multiple-campaigns tiebreak applies
    identically to either."""
    candidates = [item for item in items if (item.to_email or "").strip().lower() == address]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Same person across several campaigns: the most recent send is the one
    # they are answering. Items with no sent_at sort last rather than
    # crashing the comparison.
    return max(
        candidates,
        key=lambda i: (i.sent_at is not None, i.sent_at or i.created_at),
    )


def build_outbound_index(messages_by_item: dict[str, list]) -> dict[str, str]:
    """Build the normalized {provider_message_id: item_id} lookup used by
    strategy 2, from already-loaded conversation messages."""
    index: dict[str, str] = {}
    for item_id, messages in messages_by_item.items():
        for message in messages:
            if message.direction == "outbound" and message.provider_message_id:
                index[_normalize(message.provider_message_id)] = item_id
    return index
