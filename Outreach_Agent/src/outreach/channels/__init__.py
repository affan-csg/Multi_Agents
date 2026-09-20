"""Send channels. One module per supported channel, each exposing a `send()`
function with the same shape: (cfg, to_email, subject, body, in_reply_to) ->
message_id.

Only `email` is implemented. The Outreach Agent's job stops at delivery --
no channel here does reply detection or stage advancement past `sent`.
"""

from __future__ import annotations

from typing import Optional

from ..config import Config
from . import email as email_channel


class SendError(RuntimeError):
    pass


def send(
    cfg: Config, channel: str, *, to_email: str, subject: str, body: str,
    in_reply_to: Optional[str] = None,
) -> str:
    """Dispatch to the configured channel. Returns a provider message ID.

    `in_reply_to` -- the Message-ID this send answers -- is set by the
    Conversation Agent's replies (see responder.py) so a sent reply actually
    threads in the recipient's mail client; the cold-outreach send path
    never passes it, since there is nothing to reply to yet."""
    if channel == "email":
        return email_channel.send(cfg, to_email=to_email, subject=subject, body=body, in_reply_to=in_reply_to)
    raise SendError(f"unsupported channel: {channel!r}")
