"""Send pipeline: personalize + send every pending item in a campaign.

This module owns the only stage transitions this agent ever makes:
pending -> sent (success) or pending -> failed (send error). It never sets
waiting/replied/booked -- those belong to a person or a future reply-tracking
agent (see config/outreach.yaml stages.manual_or_future_agent).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import db
from .channels import SendError, send as send_via_channel, sender_identity
from .config import Config
from .db import SupabaseConn
from .personalize import draft_message

ProgressFn = Callable[[int, int, str], None]


@dataclass
class SendStats:
    total: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"total": self.total, "sent": self.sent, "failed": self.failed,
                "skipped": self.skipped, "errors": self.errors}


def send_campaign(
    conn: SupabaseConn, cfg: Config, campaign_id: str, *,
    on_progress: Optional[ProgressFn] = None, sender_email: Optional[str] = None,
) -> SendStats:
    """Send every `pending` item in a campaign: draft a message, send it via
    the item's channel, and record the outcome. Rate-limited per
    outreach.yaml sending.rate_limit_per_minute.

    Idempotent: only touches items currently in `pending`, so re-running a
    partially-failed campaign never re-sends an already-sent item.
    """
    items = [i for i in db.list_items(conn, campaign_id=campaign_id) if i.stage == "pending"]
    stats = SendStats(total=len(items))

    rate_per_min = int(cfg.sending.get("rate_limit_per_minute", 20))
    min_interval = 60.0 / rate_per_min if rate_per_min > 0 else 0.0

    for i, item in enumerate(items):
        if on_progress:
            on_progress(i, len(items), item.research_domain)

        prospect = db.get_prospect(conn, item.research_domain)
        if prospect is None or not prospect.has_contact:
            db.mark_failed(conn, item.item_id, "no cached prospect or contact email")
            stats.skipped += 1
            stats.errors[item.research_domain] = "no cached prospect or contact email"
            continue

        # The message was already drafted once, at campaign-creation time
        # (see ingest.create_campaign_from_prospects), so what gets sent here
        # is exactly what the user reviewed in the drawer -- never
        # regenerated at send time. Only draft here as a fallback for items
        # created before this existed.
        if item.subject and item.body:
            subject, body = item.subject, item.body
        else:
            draft = draft_message(cfg, prospect)
            subject, body = draft.subject, draft.body
            db.update_item_fields(
                conn, item.item_id,
                subject=draft.subject, body=draft.body,
                personalized_from=draft.personalized_from, to_email=prospect.contact_email,
            )

        # Resolve once and use the same value for the send and the stored
        # sender, so analytics attribute the email to the mailbox that
        # actually sent it (never to a campaign-level setting).
        actual_sender = sender_identity(cfg, sender_email)
        try:
            message_id = send_via_channel(
                cfg, item.channel, to_email=prospect.contact_email,
                subject=subject, body=body, sender_email=actual_sender,
            )
            db.mark_sent(conn, item.item_id, provider_message_id=message_id, sender_email=actual_sender)
            stats.sent += 1
        except SendError as exc:
            db.mark_failed(conn, item.item_id, str(exc), sender_email=actual_sender)
            stats.failed += 1
            stats.errors[item.research_domain] = str(exc)

        if min_interval and i < len(items) - 1:
            time.sleep(min_interval)

    if on_progress:
        on_progress(len(items), len(items), "")
    return stats
