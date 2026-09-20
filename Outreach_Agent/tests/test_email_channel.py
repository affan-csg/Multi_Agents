"""channels/email.py -- specifically the Message-ID bug: the function must
return the exact ID it put on the outgoing message, not a value it merely
hoped smtplib would hand back (it never does)."""

from __future__ import annotations

import re
from email.message import EmailMessage

import pytest

from outreach.channels import email as email_channel
from outreach.config import load_config


@pytest.fixture
def cfg():
    cfg = load_config()
    cfg.raw["sending"]["dry_run"] = False
    cfg.raw["sending"]["from_email"] = "affan@careersourcegroup.com"
    cfg.raw["sending"]["from_name"] = "CSG Outreach"
    return cfg


class _FakeSMTP:
    """Stands in for smtplib.SMTP -- captures the exact EmailMessage handed
    to send_message so the test can inspect precisely what would go on the
    wire, without a real network call."""

    sent: list[EmailMessage] = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def _capture_smtp(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(email_channel.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "affan@careersourcegroup.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    yield


_MSGID_RE = re.compile(r"^<[^@<>\s]+@[^@<>\s]+>$")


def test_returned_id_is_the_id_actually_on_the_sent_message(cfg):
    """The regression test for the actual bug: previously this returned a
    freshly-minted uuid4 that had never been written to the message at all,
    so nothing about it corresponded to what a reply's In-Reply-To would
    later contain."""
    returned = email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")

    assert len(_FakeSMTP.sent) == 1
    on_the_wire = _FakeSMTP.sent[0]["Message-ID"]

    assert returned == on_the_wire
    assert returned is not None


def test_message_id_is_well_formed(cfg):
    """A real RFC 5322 Message-ID -- <local-part@domain> -- not a bare uuid4
    with no angle brackets or @domain, which is what the old fallback
    produced and which cannot appear in any real email's headers."""
    returned = email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")
    assert _MSGID_RE.match(returned), f"not a well-formed Message-ID: {returned!r}"


def test_message_id_domain_matches_sender(cfg):
    """Qualified with CSG's own domain, not the sending machine's local
    hostname (make_msgid's default), so it is traceable and not
    coincidentally colliding with something else's IDs."""
    returned = email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")
    assert returned.endswith("@careersourcegroup.com>")


def test_message_id_is_unique_per_send(cfg):
    a = email_channel.send(cfg, to_email="a@example.com", subject="Hi", body="Body")
    b = email_channel.send(cfg, to_email="b@example.com", subject="Hi", body="Body")
    assert a != b


def test_reply_to_defaults_to_from_email_when_unset(cfg):
    """Previously no Reply-To was set at all when outreach.yaml's
    sending.reply_to was blank, relying on every mail client defaulting a
    reply to From -- explicit is safer."""
    cfg.raw["sending"]["reply_to"] = ""
    email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")

    assert _FakeSMTP.sent[0]["Reply-To"] == "affan@careersourcegroup.com"


def test_reply_to_honors_explicit_config(cfg):
    cfg.raw["sending"]["reply_to"] = "replies@careersourcegroup.com"
    email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")

    assert _FakeSMTP.sent[0]["Reply-To"] == "replies@careersourcegroup.com"


def test_from_and_to_headers_unchanged(cfg):
    """The fix must not disturb the headers that were already correct."""
    email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")
    msg = _FakeSMTP.sent[0]

    assert msg["From"] == "CSG Outreach <affan@careersourcegroup.com>"
    assert msg["To"] == "prospect@example.com"


def test_dry_run_never_touches_smtp(cfg):
    cfg.raw["sending"]["dry_run"] = True
    returned = email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")

    assert _FakeSMTP.sent == []
    assert returned.startswith("dry-run-")


# ---------------------------------------------------------------------------
# In-Reply-To / References -- the actual threading bug
# ---------------------------------------------------------------------------
#
# A Conversation Agent reply approved and sent went out with no In-Reply-To/
# References header at all, so Gmail (and every other client) displayed it
# as a brand-new conversation instead of threading it under the original
# outreach email the prospect had actually replied to.

def test_no_threading_headers_without_in_reply_to(cfg):
    """The cold-outreach send path (pipeline.py) never passes in_reply_to --
    there is nothing to reply to yet -- and must not gain headers it never
    asked for."""
    email_channel.send(cfg, to_email="prospect@example.com", subject="Hi", body="Body")
    msg = _FakeSMTP.sent[0]

    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_in_reply_to_sets_threading_headers(cfg):
    """The actual regression: a reply must carry In-Reply-To (and
    References) set to the Message-ID it answers, or the recipient's mail
    client cannot thread it."""
    email_channel.send(
        cfg, to_email="prospect@example.com", subject="Re: Hi", body="Body",
        in_reply_to="<orig@careersourcegroup.com>",
    )
    msg = _FakeSMTP.sent[0]

    assert msg["In-Reply-To"] == "<orig@careersourcegroup.com>"
    assert msg["References"] == "<orig@careersourcegroup.com>"
