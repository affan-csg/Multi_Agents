"""The Conversation Agent's ON/OFF toggle, per campaign.

Two independent switches, deliberately not collapsed into one:

    enabled     -- the master switch. False means the agent does nothing at
                   all for this campaign: no IMAP fetch, no classification,
                   no stage moves, no sends.
    auto_reply  -- whether the agent may actually SEND a reply it composed.
                   False (the default) means it still detects replies,
                   classifies them, moves cards and writes a draft -- a
                   person just has to approve the send.

"Watch my inbox and keep the board current" and "talk to my prospects
unattended" are genuinely different levels of trust, so they are two
different decisions. Turning the agent on never, by itself, causes an email
to be sent to a prospect.

Absence of a settings row is not an error -- it reads as disabled. The agent
is strictly opt-in, so a campaign nobody configured behaves exactly like one
explicitly switched off. This is what makes deploying Agent 3 a no-op for
every campaign that already exists.
"""

from __future__ import annotations

from typing import Optional

from .. import db
from ..config import Config, load_config
from ..db import SupabaseConn, utcnow
from ..models import ConversationSettings


def _defaults(cfg: Optional[Config]) -> dict:
    """Seed values for a campaign with no settings row, from
    config/outreach.yaml's `conversation` section.

    Falls back to hard-coded safe values if the config cannot be read at all,
    because "is the agent on?" must always be answerable -- and the safe
    answer is no."""
    try:
        return (cfg or load_config()).conversation_defaults
    except Exception:
        return {"enabled": False, "auto_reply": False, "followup_after_days": 1, "max_followups": 2}


def get_settings(
    conn: SupabaseConn, campaign_id: str, cfg: Optional[Config] = None,
) -> ConversationSettings:
    """This campaign's settings, defaulting to disabled.

    Never returns None and never raises for an unconfigured campaign -- the
    caller can always ask "is this on?" and get a safe answer. The default is
    constructed rather than persisted, so merely reading the toggle does not
    create a row."""
    stored = db.get_conversation_settings(conn, campaign_id)
    if stored is not None:
        return stored
    return ConversationSettings(campaign_id=campaign_id, **_defaults(cfg))


def is_enabled(conn: SupabaseConn, campaign_id: str) -> bool:
    """The master switch, read fresh from the database.

    The engine calls this immediately before every send rather than caching
    it for the duration of a tick: switching the agent off is meant to stop
    it, and a tick that started a minute ago should not keep emailing
    prospects because it read `enabled` once at the top."""
    return get_settings(conn, campaign_id).enabled


def set_settings(
    conn: SupabaseConn,
    campaign_id: str,
    *,
    enabled: bool | None = None,
    auto_reply: bool | None = None,
    followup_after_days: int | None = None,
    max_followups: int | None = None,
) -> ConversationSettings:
    """Update some or all of a campaign's settings, leaving the rest as they
    were. Every parameter is optional so the UI's toggle can flip `enabled`
    without having to know or resend the follow-up configuration.

    Writes only to conversation_settings. Turning the agent off therefore
    touches exactly one boolean -- no item is re-staged, no message is
    deleted, no history is reset. Turning it back on resumes from whatever
    the recorded state is."""
    current = get_settings(conn, campaign_id)

    updated = ConversationSettings(
        campaign_id=campaign_id,
        enabled=current.enabled if enabled is None else enabled,
        auto_reply=current.auto_reply if auto_reply is None else auto_reply,
        followup_after_days=(
            current.followup_after_days if followup_after_days is None else followup_after_days
        ),
        max_followups=current.max_followups if max_followups is None else max_followups,
        updated_at=utcnow(),
    )
    db.upsert_conversation_settings(conn, updated)
    return updated
