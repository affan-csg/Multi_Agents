"""Calendly webhook signature verification + payload normalization.

Calendly signs every webhook POST with an HMAC-SHA256 signature in the
`Calendly-Webhook-Signature` header: `t=<unix ts>,v1=<hex hmac>`, computed
over `f"{t}.{raw_body}"` using the signing key returned once when the
subscription was created (client.create_webhook_subscription's response
`signing_key`, saved as CALENDLY_WEBHOOK_SIGNING_KEY -- see .env.example).

This is the first webhook receiver in this codebase, so there is no existing
in-repo signature pattern to match -- this follows Calendly's own documented
scheme (https://developer.calendly.com/api-docs -- webhook signatures)
directly: constant-time comparison via hmac.compare_digest, and a bounded
tolerance window on the timestamp to reject replayed deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional

# How much clock skew/delivery delay to tolerate before treating a
# structurally-valid, correctly-signed payload as a stale replay. Calendly
# retries failed deliveries for a while, but a signature this old signals a
# captured-and-replayed request far more likely than a slow retry.
_TOLERANCE_SECONDS = 5 * 60


class WebhookVerificationError(RuntimeError):
    pass


def verify_signature(raw_body: bytes, signature_header: Optional[str], signing_key: str) -> None:
    """Raise WebhookVerificationError unless `signature_header` is a valid,
    fresh HMAC-SHA256 signature of `raw_body` under `signing_key`. Never
    returns a bool -- callers should not be able to accidentally ignore a
    failed check, so this fails loudly instead."""
    if not signing_key:
        raise WebhookVerificationError(
            "CALENDLY_WEBHOOK_SIGNING_KEY is not set -- see .env.example"
        )
    if not signature_header:
        raise WebhookVerificationError("missing Calendly-Webhook-Signature header")

    parts: dict[str, str] = {}
    for chunk in signature_header.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip()] = value.strip()

    timestamp = parts.get("t")
    provided_sig = parts.get("v1")
    if not timestamp or not provided_sig:
        raise WebhookVerificationError("malformed Calendly-Webhook-Signature header")

    try:
        ts_int = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("malformed timestamp in Calendly-Webhook-Signature header") from exc

    if abs(time.time() - ts_int) > _TOLERANCE_SECONDS:
        raise WebhookVerificationError("webhook signature timestamp is stale -- possible replay")

    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected_sig = hmac.new(signing_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise WebhookVerificationError("signature mismatch")


def parse_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull out the fields sync.py's normalize_event needs, from either an
    `invitee.created` or `invitee.canceled` webhook body. Calendly's webhook
    payload nests the invitee resource under `payload`, with the parent event
    under `payload.scheduled_event` -- shaped differently from the plain
    /scheduled_events + /invitees REST responses sync.py also normalizes, so
    this is kept as its own small adapter rather than forcing one shape to
    fit both.
    """
    event_type = payload.get("event")  # 'invitee.created' | 'invitee.canceled'
    body = payload.get("payload") or {}
    scheduled_event = body.get("scheduled_event") or {}
    cancellation = body.get("cancellation") or {}

    return {
        "webhook_event_type": event_type,
        "event_uri": scheduled_event.get("uri"),
        "event_type_name": scheduled_event.get("name"),
        "starts_at": scheduled_event.get("start_time"),
        "ends_at": scheduled_event.get("end_time"),
        "location": _extract_location(scheduled_event.get("location")),
        "invitee_uri": body.get("uri"),
        "invitee_email": body.get("email"),
        "invitee_name": body.get("name"),
        "status": "canceled" if event_type == "invitee.canceled" else "active",
        "cancel_reason": cancellation.get("reason"),
    }


def _extract_location(location: Any) -> Optional[str]:
    if not isinstance(location, dict):
        return None
    return location.get("join_url") or location.get("location") or None
