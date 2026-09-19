"""Send channels. One module per supported channel, each exposing a `send()`
function with the same shape: (cfg, to_email, subject, body) -> message_id.

Only `email` is implemented. The Outreach Agent's job stops at delivery --
no channel here does reply detection or stage advancement past `sent`.
"""

from __future__ import annotations

from ..config import Config
from . import email as email_channel


class SendError(RuntimeError):
    pass


def send(cfg: Config, channel: str, *, to_email: str, subject: str, body: str) -> str:
    """Dispatch to the configured channel. Returns a provider message ID."""
    if channel == "email":
        return email_channel.send(cfg, to_email=to_email, subject=subject, body=body)
    raise SendError(f"unsupported channel: {channel!r}")
