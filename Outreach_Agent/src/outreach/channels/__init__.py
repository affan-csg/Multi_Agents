"""Send channels. One module per supported channel, each exposing a `send()`
function with the same shape: (cfg, to_email, subject, body, in_reply_to) ->
message_id.

Only `email` is implemented. The Outreach Agent's job stops at delivery --
no channel here does reply detection or stage advancement past `sent`.
"""

from __future__ import annotations

from typing import Optional

from ..config import Config


class SendError(RuntimeError):
    pass


# Imported after SendError on purpose: channels.email's EmailSendError
# subclasses it, so every channel failure is catchable as SendError.
from . import email as email_channel  # noqa: E402


def send(
    cfg: Config, channel: str, *, to_email: str, subject: str, body: str,
    in_reply_to: Optional[str] = None, sender_email: Optional[str] = None,
) -> str:
    """Dispatch to the configured channel. Returns a provider message ID.

    `in_reply_to` -- the Message-ID this send answers -- is set by the
    Conversation Agent's replies (see responder.py) so a sent reply actually
    threads in the recipient's mail client; the cold-outreach send path
    never passes it, since there is nothing to reply to yet.

    `sender_email` selects the sending account; callers resolve it with
    `sender_identity` first so the address they store is the one used."""
    if channel == "email":
        kwargs = {"sender_email": sender_email} if sender_email else {}
        return email_channel.send(
            cfg, to_email=to_email, subject=subject, body=body, in_reply_to=in_reply_to, **kwargs,
        )
    raise SendError(f"unsupported channel: {channel!r}")


def sender_identity(cfg: Config, sender_email: Optional[str] = None) -> Optional[str]:
    """The sender address a send with `sender_email` will actually use (the
    default account's when None or unknown), or None if nothing is
    configured. This is what gets stored as outreach_items.sender_email."""
    return cfg.account_for(sender_email).identity or None
