"""Tests for calendly/webhook.py's signature verification -- the Calendly-
Webhook-Signature HMAC-SHA256 scheme (t=<ts>,v1=<hex hmac> over
f"{t}.{raw_body}") and payload normalization for invitee.created/canceled."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from outreach.calendly.webhook import (
    WebhookVerificationError,
    parse_webhook_payload,
    verify_signature,
)

SIGNING_KEY = "test-signing-key"


def _sign(body: bytes, ts: int, key: str = SIGNING_KEY) -> str:
    signed_payload = f"{ts}.{body.decode('utf-8')}".encode("utf-8")
    sig = hmac.new(key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_valid_signature_passes():
    body = b'{"event": "invitee.created"}'
    ts = int(time.time())
    header = _sign(body, ts)
    verify_signature(body, header, SIGNING_KEY)  # must not raise


def test_missing_header_rejected():
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_signature(b"{}", None, SIGNING_KEY)


def test_missing_signing_key_rejected():
    body = b"{}"
    header = _sign(body, int(time.time()))
    with pytest.raises(WebhookVerificationError, match="CALENDLY_WEBHOOK_SIGNING_KEY"):
        verify_signature(body, header, "")


def test_wrong_key_rejected():
    body = b'{"event": "invitee.created"}'
    ts = int(time.time())
    header = _sign(body, ts, key="wrong-key")
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_signature(body, header, SIGNING_KEY)


def test_tampered_body_rejected():
    body = b'{"event": "invitee.created"}'
    ts = int(time.time())
    header = _sign(body, ts)
    tampered = b'{"event": "invitee.canceled"}'
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_signature(tampered, header, SIGNING_KEY)


def test_stale_timestamp_rejected():
    body = b"{}"
    old_ts = int(time.time()) - 3600  # 1 hour old, past the 5-minute tolerance
    header = _sign(body, old_ts)
    with pytest.raises(WebhookVerificationError, match="stale"):
        verify_signature(body, header, SIGNING_KEY)


def test_malformed_header_rejected():
    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_signature(b"{}", "not-a-valid-header", SIGNING_KEY)


def test_malformed_timestamp_rejected():
    with pytest.raises(WebhookVerificationError, match="malformed timestamp"):
        verify_signature(b"{}", "t=notanumber,v1=abc", SIGNING_KEY)


# --- parse_webhook_payload ----------------------------------------------------

def test_parse_invitee_created_payload():
    payload = {
        "event": "invitee.created",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
            "email": "prospect@acme.com",
            "name": "Dana Prospect",
            "scheduled_event": {
                "uri": "https://api.calendly.com/scheduled_events/EV1",
                "name": "30 Minute Meeting",
                "start_time": "2026-04-01T15:00:00.000000Z",
                "end_time": "2026-04-01T15:30:00.000000Z",
                "location": {"join_url": "https://zoom.us/j/123"},
            },
        },
    }
    fields = parse_webhook_payload(payload)
    assert fields["webhook_event_type"] == "invitee.created"
    assert fields["event_uri"] == "https://api.calendly.com/scheduled_events/EV1"
    assert fields["invitee_email"] == "prospect@acme.com"
    assert fields["invitee_name"] == "Dana Prospect"
    assert fields["status"] == "active"
    assert fields["location"] == "https://zoom.us/j/123"


def test_parse_invitee_canceled_payload():
    payload = {
        "event": "invitee.canceled",
        "payload": {
            "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1",
            "email": "prospect@acme.com",
            "scheduled_event": {"uri": "https://api.calendly.com/scheduled_events/EV1"},
            "cancellation": {"reason": "Can't make it"},
        },
    }
    fields = parse_webhook_payload(payload)
    assert fields["status"] == "canceled"
    assert fields["cancel_reason"] == "Can't make it"


def test_parse_payload_missing_location_is_none():
    payload = {"event": "invitee.created", "payload": {"scheduled_event": {"uri": "u1"}}}
    fields = parse_webhook_payload(payload)
    assert fields["location"] is None
