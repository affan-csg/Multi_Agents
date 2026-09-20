"""Decide what an inbound reply actually means.

Two layers, deliberately in this order:

  1. Deterministic detection of bounces and auto-responders, from the headers
     and a few unambiguous body markers. These are the two cases where mail
     arrived but no human replied, and getting them wrong corrupts the funnel
     in the most damaging way available -- an out-of-office moved to Replied
     looks exactly like genuine interest on the board, and a bounce looks
     like a prospect who answered. They are also perfectly detectable without
     a model, so no model is asked.

  2. An LLM for everything that is a real human reply, using the same backend
     convention as personalize.py and the Research Agent's qualify step:
     Gemini first if GEMINI_API_KEY is set, else Anthropic.

With no LLM key configured, or on any API failure, classification degrades to
`unclear` rather than raising. `unclear` is a real human reply that could not
be categorized -- it still moves the card to Replied so a person sees it, it
just never triggers an automatic action. Losing the ability to classify must
never mean losing the reply.

Nothing here writes to the database or moves a card; engine.py owns that.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ..config import PROJECT_ROOT, Config
from ..models import ReplyIntent
from .inbox import InboundEmail
from .quoting import strip_quoted

_PROMPT_PATH = PROJECT_ROOT / "prompts" / "classify_reply.md"

# Headers that mark automated mail. RFC 3834 (Auto-Submitted) and the
# de-facto Microsoft/Exchange conventions. Their mere presence is decisive --
# no legitimate human reply carries them.
_AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "x-auto-response-suppress")

# Subject markers for out-of-office replies, checked only when the headers
# are absent or unavailable (some providers strip them in transit).
_OOO_SUBJECT_MARKERS = (
    "out of office", "out of the office", "auto-reply", "autoreply",
    "automatic reply", "away from my desk", "on vacation", "on leave",
    "annual leave", "maternity leave", "paternity leave", "parental leave",
)

# Bounce markers. Delivery Status Notifications are near-universally sent
# from one of these addresses and/or carry these subjects.
_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "no-reply@", "noreply@")
_BOUNCE_SUBJECT_MARKERS = (
    "undeliverable", "delivery status notification", "delivery failure",
    "returned mail", "mail delivery failed", "address not found",
    "recipient rejected", "message not delivered", "delivery incomplete",
)

_VALID_INTENTS: tuple[ReplyIntent, ...] = (
    "interested", "booked", "not_interested", "out_of_office", "bounce", "unclear",
)

# A booking made through a scheduling link (Calendly, and equivalents) often
# arrives as a notification FROM THE SCHEDULING TOOL, not as prose from the
# prospect -- "New Event: <name> - 30 Minute Meeting", sent by
# notifications@calendly.com. That is exactly the shape `_looks_like_bounce`
# is designed to catch as an automated sender, and exactly what
# `_looks_like_auto_reply` would risk misreading if it ever checked sender
# domains instead of just OOO-shaped subjects -- so this is decided
# separately and BEFORE either of those, as its own positive signal, rather
# than trying to carve a bounce/auto-reply exception for it after the fact.
_SCHEDULING_SENDERS = ("calendly.com", "calendar-notification@google.com", "@calendar.google.com")
_SCHEDULING_SUBJECT_MARKERS = (
    "new event:", "confirmed:", "invitation:", "meeting confirmed",
    "event confirmation", "you're scheduled", "you are scheduled",
)


def _looks_like_booking_confirmation(email_in: InboundEmail) -> bool:
    sender = (email_in.from_email or "").lower()
    subject = (email_in.subject or "").lower()

    if any(marker in sender for marker in _SCHEDULING_SENDERS):
        return True
    return any(marker in subject for marker in _SCHEDULING_SUBJECT_MARKERS)


def _looks_like_bounce(email_in: InboundEmail) -> bool:
    sender = (email_in.from_email or "").lower()
    subject = (email_in.subject or "").lower()

    if any(marker in sender for marker in _BOUNCE_SENDERS):
        # A no-reply sender alone is not proof (plenty of humans reply from
        # monitored no-reply aliases); it needs a bounce-shaped subject too.
        if any(marker in subject for marker in _BOUNCE_SUBJECT_MARKERS):
            return True
        if "mailer-daemon" in sender or "postmaster" in sender:
            return True

    return any(marker in subject for marker in _BOUNCE_SUBJECT_MARKERS)


def _looks_like_auto_reply(email_in: InboundEmail, headers: Optional[dict[str, str]] = None) -> bool:
    if headers:
        lowered = {k.lower(): (v or "").lower() for k, v in headers.items()}
        for header in _AUTO_HEADERS:
            value = lowered.get(header)
            # Auto-Submitted: no is the explicit "this IS human" value, so it
            # must not be read as evidence of automation.
            if value and value != "no":
                return True

    subject = (email_in.subject or "").lower()
    return any(marker in subject for marker in _OOO_SUBJECT_MARKERS)


def _strip_quoted(body: str) -> str:
    """Drop the quoted original beneath a reply -- see quoting.strip_quoted.

    A reply typically carries our entire outreach email underneath it. Left
    in, the model sees far more of our own pitch than of their answer, and
    reliably misreads a two-word rejection as interest. Kept as a thin alias
    here so existing call sites/tests in this module don't need updating;
    the real implementation is shared with routes.py's drawer display so the
    two can never disagree about what "just their reply" means."""
    return strip_quoted(body)


def _render_prompt(email_in: InboundEmail, reply_text: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{from_email}}", email_in.from_email or "unknown")
        .replace("{{subject}}", email_in.subject or "(no subject)")
        .replace("{{reply_body}}", reply_text[:4000])
    )


def _call_llm(prompt: str) -> Optional[str]:
    """Same backend order as personalize._call_llm: Gemini if its key is set,
    else Anthropic, and None on any failure so the caller can degrade."""
    try:
        if os.environ.get("GEMINI_API_KEY"):
            from google import genai
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            resp = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
            return resp.text
        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            resp = client.messages.create(
                model="claude-opus-5", max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(block.text for block in resp.content if hasattr(block, "text"))
    except Exception:
        return None
    return None


def _parse_intent(text: str) -> Optional[ReplyIntent]:
    """Pull the intent out of the model's answer.

    Matches on a labelled INTENT: line first, then falls back to scanning for
    a bare intent word, since a model occasionally answers with the value
    alone. Anything unrecognized returns None and becomes `unclear`."""
    if not text:
        return None

    match = re.search(r"INTENT:\s*([a-z_]+)", text, re.IGNORECASE)
    candidate = match.group(1).lower().strip() if match else text.strip().lower()

    if candidate in _VALID_INTENTS:
        return candidate  # type: ignore[return-value]

    for intent in _VALID_INTENTS:
        if re.search(rf"\b{intent}\b", candidate):
            return intent
    return None


def classify(
    cfg: Config,
    email_in: InboundEmail,
    *,
    headers: Optional[dict[str, str]] = None,
) -> ReplyIntent:
    """Classify one inbound reply. Never raises.

    Bounces, auto-responders, and scheduling-tool booking confirmations are
    all decided deterministically and never reach the LLM. Everything else
    is a real human reply, and degrades to `unclear` -- not to silence -- if
    no model is available."""
    # Checked first, and separately from the other two: a Calendly
    # notification is automated mail (it would otherwise partially match
    # `_looks_like_auto_reply`'s shape), but it is the single clearest
    # `booked` signal that exists -- the prospect finished scheduling
    # through the exact link the agent offered them. Treating it as the
    # positive case up front means it can never be shadowed by the
    # bounce/auto-reply checks below.
    if _looks_like_booking_confirmation(email_in):
        return "booked"
    if _looks_like_bounce(email_in):
        return "bounce"
    if _looks_like_auto_reply(email_in, headers):
        return "out_of_office"

    reply_text = _strip_quoted(email_in.body or "")
    if not reply_text.strip():
        return "unclear"

    text = _call_llm(_render_prompt(email_in, reply_text))
    if not text:
        return "unclear"

    return _parse_intent(text) or "unclear"
