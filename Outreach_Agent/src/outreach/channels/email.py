"""Email channel: sends via SMTP (works with SendGrid/Postmark/SES/Gmail
relay/etc -- whatever SMTP_HOST points at). Provider-agnostic on purpose so
switching providers is a .env change, not a code change.

`cfg.dry_run` (outreach.yaml sending.dry_run) short-circuits an actual send
and returns a fake message id -- used for testing the pipeline end to end
without spending real send quota, same spirit as the Research Agent's
explicit-cost-gated steps (qualify/contacts).
"""

from __future__ import annotations

import os
import smtplib
import uuid
from email.message import EmailMessage

from ..config import Config


class EmailSendError(RuntimeError):
    pass


def send(cfg: Config, *, to_email: str, subject: str, body: str) -> str:
    if not to_email:
        raise EmailSendError("to_email is required")

    if cfg.dry_run:
        return f"dry-run-{uuid.uuid4()}"

    host = os.environ.get("SMTP_HOST")
    if not host:
        raise EmailSendError("SMTP_HOST is not set in .env -- required to send email (or set sending.dry_run: true)")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    msg["To"] = to_email
    reply_to = cfg.sending.get("reply_to")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(str(exc)) from exc

    return msg.get("Message-Id") or str(uuid.uuid4())
