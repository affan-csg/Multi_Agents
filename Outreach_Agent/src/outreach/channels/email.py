"""Email channel: sends via SMTP (works with SendGrid/Postmark/SES/Gmail
relay/etc -- whatever SMTP_HOST points at). Provider-agnostic on purpose so
switching providers is a .env change, not a code change.

`cfg.dry_run` (outreach.yaml sending.dry_run) short-circuits an actual send
and returns a fake message id -- used for testing the pipeline end to end
without spending real send quota, same spirit as the Research Agent's
explicit-cost-gated steps (qualify/contacts).

Message-ID: generated and set on the outgoing message BEFORE it is handed to
smtplib, and that exact value -- not whatever the relay decides to do with it
-- is what this function returns and what gets stored as
outreach_items.provider_message_id / conversation_messages.provider_message_id.

This matters because a relay (Gmail's included) is free to overwrite or
discard a Message-ID header it does not like and assign its own. If this
function instead tried to read the header back off `msg` after sending, it
would still see the value it set here, but that is NOT necessarily what the
relay actually put on the wire -- the only way to guarantee the stored ID is
the one a reply will thread against is to generate an ID good enough that no
relay has a reason to replace it (via email.utils.make_msgid, the same
stdlib generator every real MTA uses) and treat it as authoritative
ourselves. A reply's In-Reply-To/References headers echo back whatever
Message-ID the recipient's mail client actually saw, which will be either
this one or the relay's replacement -- see the module docstring in
../conversation/matcher.py for how a sender-address fallback still resolves
a reply even if a relay did swap the ID.

In-Reply-To / References: `in_reply_to`, when given, is the Message-ID of
the message this one answers -- set as both the In-Reply-To header and (per
RFC 5322 5.3) the sole entry of the References header when the conversation
has no earlier reference chain of its own. This is what makes a sent reply
actually thread in the recipient's mail client instead of landing as a new,
unrelated conversation -- the cold-outreach send path (pipeline.py) never
passes this (there is nothing to reply to yet), while the Conversation
Agent's reply path (responder.py) always does.
"""

from __future__ import annotations

import smtplib
import uuid
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional

from ..config import Config
from . import SendError


class EmailSendError(SendError):
    pass


def send(
    cfg: Config, *, to_email: str, subject: str, body: str,
    in_reply_to: Optional[str] = None, sender_email: Optional[str] = None,
) -> str:
    """`sender_email` picks the sending account (see Config.accounts); None or
    an unknown address uses the default account, exactly as before."""
    if not to_email:
        raise EmailSendError("to_email is required")

    if cfg.dry_run:
        return f"dry-run-{uuid.uuid4()}"

    account = cfg.account_for(sender_email)
    host = account.smtp_host
    if not host:
        raise EmailSendError("SMTP_HOST is not set in .env -- required to send email (or set sending.dry_run: true)")
    port = account.smtp_port
    username = account.smtp_username
    password = account.smtp_password
    from_email = account.from_email
    from_name = account.from_name

    # The domain half of a generated Message-ID should be ours, not the
    # relay's -- make_msgid's own hostname guess is whatever this machine's
    # local hostname resolves to, which means nothing to a recipient and
    # nothing to us later. Derived from from_email so it is at least
    # traceable back to CSG's own domain.
    id_domain = (from_email or "").split("@")[-1] or "csg-outreach.local"
    message_id = make_msgid(domain=id_domain)

    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to_email
    # Reply-To is set explicitly to from_email even when outreach.yaml's
    # sending.reply_to is blank. Leaving the header off entirely relies on
    # every mail client defaulting a reply to the From address, which is the
    # common case but not a guarantee (list-manager and some transactional
    # senders rewrite From to a no-reply-looking value precisely so clients
    # must consult Reply-To) -- setting it ourselves removes the ambiguity
    # and is what the Conversation Agent's own reply drafts (responder.py)
    # rely on being consistent.
    # sending.reply_to applies to the default account only: a shared Reply-To
    # on a second account would route its replies into the wrong mailbox.
    reply_to = cfg.sending.get("reply_to") if account.is_default else None
    msg["Reply-To"] = reply_to or from_email
    if in_reply_to:
        # RFC 5322 5.3: References SHOULD list every ancestor message, but a
        # single entry (the immediate parent) is valid and is what every
        # mainstream mail client actually needs to fold this into the same
        # conversation -- the Conversation Agent's own matcher.py already
        # walks a fuller thread server-side; the recipient's client only
        # needs enough here to thread the display correctly.
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(str(exc)) from exc

    return message_id
