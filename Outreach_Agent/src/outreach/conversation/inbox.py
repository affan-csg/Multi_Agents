"""IMAP reply fetching -- reads the mailbox the outreach was sent from.

Provider-agnostic in the same spirit as channels/email.py: whatever
IMAP_HOST points at (Gmail, Outlook/365, Fastmail, a self-hosted Dovecot)
works, so switching providers is a .env change rather than a code change.
For Gmail specifically the password is an App Password, exactly as for SMTP.

This module is deliberately dumb: it fetches and parses, and knows nothing
about outreach items, stages or campaigns. Matching an email to an item is
matcher.py's job; deciding what it means is classifier.py's. That separation
is what lets the whole reply path be tested offline with hand-built
`InboundEmail` objects and no IMAP server anywhere.

On read-state: the fetch does NOT rely on IMAP's \\Seen flag or on UIDs to
decide what is new. Flags can be reset by another mail client, a mailbox can
be re-synced, and a tick can die halfway through. Instead it re-reads a
recent window every time and lets the database reject anything already
stored (see db.add_conversation_message). Re-reading is cheap; missing a
reply because a flag was clobbered is not. Messages are fetched with
BODY.PEEK so this never marks the user's mail as read behind their back.
"""

from __future__ import annotations

import email
import imaplib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Optional

# How far back a fetch looks. Generous on purpose: the cost of re-reading an
# already-stored reply is one rejected insert, while the cost of missing one
# is a prospect who answered and never heard back.
_DEFAULT_LOOKBACK_DAYS = 14


class InboxError(RuntimeError):
    pass


@dataclass
class InboundEmail:
    """One fetched reply, provider-agnostic and fully detached from IMAP.

    Everything downstream (matcher, classifier, engine) consumes this shape,
    so the entire reply pipeline can be exercised in tests by constructing
    these directly."""

    message_id: str            # RFC 5322 Message-ID -- the dedupe key
    in_reply_to: Optional[str]  # RFC 5322 In-Reply-To -- the primary thread link
    references: list[str]      # RFC 5322 References -- the fallback thread link
    from_email: str
    to_email: str
    subject: str
    body: str
    received_at: Optional[datetime]
    # RFC 5322 Reply-To. Normally unset, or the sender's own address again --
    # but a scheduling tool's booking notification (Calendly confirmed) puts
    # the actual invitee's address here even though From/To are both the
    # tool's own addresses, which is what lets matcher.py's strategy 4
    # resolve "someone booked a call" back to the right prospect when no
    # other signal (threading headers, sender address) is available at all.
    reply_to: Optional[str] = None


def _decode(value: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header (=?utf-8?B?...?=) to plain text.
    Subjects from non-English senders arrive encoded, and an undecoded one
    would reach the classifier as gibberish."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _address_of(value: Optional[str]) -> str:
    """The bare address out of a From/To header -- `A B <a@b.com>` -> `a@b.com`.
    Lowercased, because matching is case-insensitive while mail headers are
    not consistently cased."""
    if not value:
        return ""
    parsed = email.utils.parseaddr(value)[1]
    return (parsed or "").strip().lower()


def _body_text(msg: Message) -> str:
    """Flatten a message to plain text, preferring text/plain.

    Attachments and inline images are skipped. An HTML-only reply falls back
    to its HTML source rather than returning nothing -- the classifier can
    still read the words through the tags, and an empty body would look like
    a reply with no content."""
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        if payload is None:
            return str(msg.get_payload() or "")
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")

    html_fallback = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition", "").startswith("attachment"):
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")

        if part.get_content_type() == "text/plain":
            return text
        if part.get_content_type() == "text/html" and not html_fallback:
            html_fallback = text

    return html_fallback


def _references_of(msg: Message) -> list[str]:
    raw = msg.get("References") or ""
    return [ref.strip() for ref in raw.split() if ref.strip()]


def parse_message(raw: bytes) -> Optional[InboundEmail]:
    """Turn a raw RFC 822 message into an InboundEmail.

    Returns None for a message with no Message-ID: without one there is no
    dedupe key, so ingesting it could duplicate the reply on every tick.
    Dropping it is the safer failure -- every real mail agent sets one."""
    msg = email.message_from_bytes(raw)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        return None

    received_at: Optional[datetime] = None
    if msg.get("Date"):
        try:
            parsed = parsedate_to_datetime(msg["Date"])
            # Stored naive-UTC, matching db.utcnow's convention.
            received_at = (
                parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
            )
        except (TypeError, ValueError):
            received_at = None

    return InboundEmail(
        message_id=message_id,
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=_references_of(msg),
        from_email=_address_of(msg.get("From")),
        to_email=_address_of(msg.get("To")),
        subject=_decode(msg.get("Subject")),
        body=_body_text(msg),
        received_at=received_at,
        reply_to=_address_of(msg.get("Reply-To")) or None,
    )


def imap_username() -> str:
    """The mailbox to read. Falls back to SMTP_USERNAME, because replies land
    in the mailbox the outreach was sent from -- that is the same account by
    definition, so making it a second setting only creates a way for the two
    to drift apart."""
    return (os.environ.get("IMAP_USERNAME") or os.environ.get("SMTP_USERNAME") or "").strip()


def imap_password() -> str:
    """The mailbox password, falling back to SMTP_PASSWORD.

    For Gmail the same App Password authorizes both protocols, so copying it
    into a second variable buys nothing and quietly breaks reply detection
    the day the password is rotated in only one place. Setting
    IMAP_PASSWORD explicitly still wins, for a provider that genuinely needs
    separate credentials."""
    return os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or ""


def imap_configured() -> bool:
    """Whether IMAP credentials exist. Checked before a tick so a missing
    configuration is reported as a clear, actionable message rather than a
    connection error from deep inside imaplib."""
    return bool(os.environ.get("IMAP_HOST") and imap_username())


def fetch_replies(*, lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> list[InboundEmail]:
    """Fetch recent messages from the configured mailbox.

    Returns every message in the window, not just unread ones -- read-state
    lives in the database, not in IMAP flags (see the module docstring).
    Uses BODY.PEEK so fetching never marks the user's own mail as read.

    Raises InboxError for anything the operator needs to fix (missing config,
    bad credentials, unreachable host); an individual message that fails to
    parse is skipped rather than failing the whole batch, so one malformed
    mail cannot block every other reply in the mailbox."""
    host = os.environ.get("IMAP_HOST")
    username = imap_username()
    password = imap_password()

    if not host or not username:
        raise InboxError(
            "IMAP_HOST must be set in .env to check for replies (see .env.example). "
            "IMAP_USERNAME/IMAP_PASSWORD fall back to the SMTP ones, since replies "
            "land in the mailbox the outreach was sent from."
        )

    port = int(os.environ.get("IMAP_PORT", "993"))
    folder = os.environ.get("IMAP_FOLDER", "INBOX")
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")

    try:
        conn = imaplib.IMAP4_SSL(host, port)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise InboxError(f"could not connect to IMAP host {host}:{port} -- {exc}") from exc

    try:
        try:
            conn.login(username, password or "")
        except imaplib.IMAP4.error as exc:
            raise InboxError(
                f"IMAP login failed for {username} -- {exc}. "
                "For Gmail this usually means an App Password is required."
            ) from exc

        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise InboxError(f"could not open IMAP folder {folder!r}")

        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            raise InboxError("IMAP search failed")

        ids = (data[0] or b"").split()
        out: list[InboundEmail] = []
        for msg_id in ids:
            try:
                status, payload = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                parsed = parse_message(payload[0][1])
                if parsed:
                    out.append(parsed)
            except Exception:
                # One unreadable message must not cost us every other reply.
                continue
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass
