"""Compose (and, only if allowed, send) the next message in a conversation.

The central rule of this module: composing and sending are two separate
steps, and only the first happens by default.

With `auto_reply` off -- the default, even when the agent is switched on --
`handle_reply` writes a fully drafted reply into conversation_messages with
draft_pending=True and stops. Nothing leaves the building. A person reads it
in the item drawer and clicks send, which is what actually mails it. With
`auto_reply` on, the same draft is sent immediately.

Either way the draft is stored before any send is attempted, so a send that
fails leaves a recoverable draft rather than losing the composed reply.

Drafting reuses personalize.py's LLM plumbing (_call_llm, _parse_llm_output,
_sender_signature) rather than reimplementing it, so this agent inherits the
same backend order, the same signature block, and the same parse contract as
every other message the system writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .. import db, personalize
from ..channels import SendError, send as send_via_channel
from ..config import PROJECT_ROOT, Config
from ..db import SupabaseConn, utcnow
from ..models import (
    ConversationMessage,
    ConversationSettings,
    ImportedProspect,
    OutreachItem,
    ReplyIntent,
)
from .quoting import strip_quoted

_REPLY_PROMPT_PATH = PROJECT_ROOT / "prompts" / "conversation_reply.md"
_FOLLOWUP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "conversation_followup.md"
_REVISE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "conversation_reply_revise.md"

# Intents worth answering at all. A rejection gets no automated reply -- the
# respectful response to "not interested" is silence, and an auto-reply to it
# is the fastest way to earn a spam complaint. out_of_office and bounce are
# not human replies in the first place.
_REPLYABLE: tuple[ReplyIntent, ...] = ("interested", "booked", "unclear")


@dataclass
class ComposedReply:
    subject: str
    body: str
    # True when this was written but deliberately not sent, because
    # auto_reply is off. The UI surfaces these for approval.
    draft_pending: bool
    message_id: str
    provider_message_id: Optional[str] = None
    error: Optional[str] = None


def should_reply(intent: ReplyIntent) -> bool:
    return intent in _REPLYABLE


def _research_context(prospect: Optional[ImportedProspect]) -> str:
    """Grounding facts for the prompt, drawn only from cached research.

    Deliberately terse and factual: everything here is something the model is
    permitted to state, so anything uncertain is left out entirely rather
    than hedged."""
    if prospect is None:
        return "not available"

    lines: list[str] = []
    if prospect.name:
        lines.append(f"Company: {prospect.name}")
    if prospect.industry:
        lines.append(f"Industry: {prospect.industry}")
    if prospect.region:
        lines.append(f"Region: {prospect.region}")
    if prospect.description:
        lines.append(f"About: {prospect.description}")
    if prospect.pain_summary:
        lines.append(f"Likely pain: {prospect.pain_summary}")
    if prospect.recommended_angle:
        lines.append(f"Recommended angle: {prospect.recommended_angle}")

    titles = [r.get("title") for r in (prospect.open_reqs or []) if r.get("title")][:3]
    if titles:
        lines.append(f"Open roles: {', '.join(titles)}")

    return "\n".join(lines) if lines else "not available"


def _render_thread(messages: list[ConversationMessage], limit: int = 6) -> str:
    """The conversation so far, oldest first, as plain labelled turns.

    Trimmed to the most recent `limit` messages: a long thread would crowd
    out the latest reply, which is the part that actually matters."""
    if not messages:
        return "(no prior messages recorded)"

    recent = messages[-limit:]
    parts = []
    for message in recent:
        who = "Them" if message.direction == "inbound" else "Us"
        # Every inbound turn carries its own quoted history the same way the
        # latest reply does -- stripped here too, or the model re-reads our
        # own prior emails N times over as it walks the thread.
        raw = message.body or ""
        body = (strip_quoted(raw) if message.direction == "inbound" else raw).strip()
        parts.append(f"{who}: {body[:1200]}")
    return "\n\n".join(parts)


def _thread_subject(original: Optional[str]) -> str:
    """Keep the thread's subject, adding Re: exactly once."""
    subject = (original or "").strip() or "Following up"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def compose_reply(
    cfg: Config,
    item: OutreachItem,
    prospect: Optional[ImportedProspect],
    thread: list[ConversationMessage],
    latest_reply: ConversationMessage,
    intent: ReplyIntent,
) -> Optional[tuple[str, str]]:
    """Draft the reply text. Returns (subject, body), or None if no LLM is
    available or the response could not be parsed.

    None is not an error the caller should swallow silently -- it means a
    reply genuinely could not be written, and the engine records that on the
    item so a person knows to write one by hand."""
    signature = personalize._sender_signature(cfg)
    template = _REPLY_PROMPT_PATH.read_text(encoding="utf-8")

    # Strip the quoted original out of their latest reply before it reaches
    # the model, same as classifier.py already does before classifying it.
    # Left in, the model is handed our own prior email as if it were part of
    # what the prospect just said, which both pads the "answer what they
    # asked" instruction with our own words and risks the reply echoing
    # fragments of the quoted pitch back as if it were new.
    latest_text = strip_quoted(latest_reply.body or "")[:4000]

    calendly_url = cfg.calendly_url

    prompt = (
        template
        .replace("{{company_name}}", (prospect.name if prospect else None) or item.research_domain)
        .replace("{{domain}}", item.research_domain)
        .replace("{{contact_name}}", (prospect.contact_name if prospect else None) or "there")
        .replace("{{contact_position}}", (prospect.contact_position if prospect else None) or "")
        .replace("{{research_context}}", _research_context(prospect))
        .replace("{{calendly_url}}", calendly_url or "(none configured -- ask for specific times instead)")
        .replace("{{thread}}", _render_thread(thread))
        .replace("{{latest_reply}}", latest_text)
        .replace("{{intent}}", intent)
        .replace("{{signature}}", signature)
    )

    text = personalize._call_llm(prompt)
    if not text:
        return None

    parsed = personalize._parse_llm_output(text)
    if not parsed:
        return None

    # The model is asked to keep the thread's subject (see
    # conversation_reply.md's SUBJECT instruction), but an instruction is not
    # a guarantee: an LLM can paraphrase or invent a subject line even when
    # told to preserve one verbatim -- observed directly (a reply to "Quick
    # question about your hiring pipeline" came back subjected "Re: Tech and
    # AI talent delivery"). Gmail and most clients thread primarily on
    # Subject + In-Reply-To/References together, so a drifted subject here
    # sends the reply into a brand-new conversation in the recipient's inbox
    # even though matcher.py still correctly resolves it as the same item
    # server-side. The parsed `subject` is therefore discarded outright for a
    # cold-reply/follow-up send -- _thread_subject always derives from the
    # real prior subject, never from what the model returned.
    _, body = parsed
    return _thread_subject(latest_reply.subject or item.subject), body


def revise_reply(
    cfg: Config,
    item: OutreachItem,
    prospect: Optional[ImportedProspect],
    *,
    latest_reply: ConversationMessage,
    current_subject: str,
    current_body: str,
    instruction: str,
) -> tuple[str, str]:
    """Edit an already-drafted reply per a free-text instruction, instead of
    regenerating it from scratch -- mirrors personalize.revise_message's
    contract exactly (same prompt discipline: smallest edit that satisfies
    the instruction, everything else preserved). Raises RuntimeError if no
    LLM is available or the response can't be parsed; there is no sensible
    non-LLM fallback for an arbitrary edit instruction, same as the cold-
    outreach version."""
    signature = personalize._sender_signature(cfg)
    template = _REVISE_PROMPT_PATH.read_text(encoding="utf-8")

    prompt = (
        template
        .replace("{{company_name}}", (prospect.name if prospect else None) or item.research_domain)
        .replace("{{domain}}", item.research_domain)
        .replace("{{contact_name}}", (prospect.contact_name if prospect else None) or "there")
        .replace("{{contact_position}}", (prospect.contact_position if prospect else None) or "")
        .replace("{{latest_reply}}", strip_quoted(latest_reply.body or "")[:4000])
        .replace("{{current_subject}}", current_subject)
        .replace("{{current_body}}", current_body)
        .replace("{{instruction}}", instruction)
        .replace("{{signature}}", signature)
    )

    text = personalize._call_llm(prompt)
    if not text:
        raise RuntimeError("no LLM available to revise this reply (GEMINI_API_KEY / ANTHROPIC_API_KEY not set, or the call failed)")

    parsed = personalize._parse_llm_output(text)
    if not parsed:
        raise RuntimeError("LLM response could not be parsed into SUBJECT/BODY")

    subject, body = parsed
    return _thread_subject(subject or current_subject), body


def compose_followup(
    cfg: Config,
    item: OutreachItem,
    prospect: Optional[ImportedProspect],
    *,
    followup_number: int,
    max_followups: int,
) -> Optional[tuple[str, str]]:
    """Draft a follow-up to an unanswered outreach email."""
    signature = personalize._sender_signature(cfg)
    template = _FOLLOWUP_PROMPT_PATH.read_text(encoding="utf-8")

    original = f"Subject: {item.subject or ''}\n\n{item.body or ''}".strip()

    prompt = (
        template
        .replace("{{company_name}}", (prospect.name if prospect else None) or item.research_domain)
        .replace("{{domain}}", item.research_domain)
        .replace("{{contact_name}}", (prospect.contact_name if prospect else None) or "there")
        .replace("{{contact_position}}", (prospect.contact_position if prospect else None) or "")
        .replace("{{research_context}}", _research_context(prospect))
        .replace("{{calendly_url}}", cfg.calendly_url or "(none configured -- ask for specific times instead)")
        .replace("{{original_message}}", original[:4000])
        .replace("{{followup_number}}", str(followup_number))
        .replace("{{max_followups}}", str(max_followups))
        .replace("{{signature}}", signature)
    )

    text = personalize._call_llm(prompt)
    if not text:
        return None

    parsed = personalize._parse_llm_output(text)
    if not parsed:
        return None

    # Same reasoning as compose_reply above: never trust the model's own
    # SUBJECT for a follow-up either -- always thread against the original
    # outreach item's real subject.
    _, body = parsed
    return _thread_subject(item.subject), body


def store_and_maybe_send(
    conn: SupabaseConn,
    cfg: Config,
    item: OutreachItem,
    settings: ConversationSettings,
    *,
    subject: str,
    body: str,
    to_email: str,
    in_reply_to: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ComposedReply:
    """Record an outbound message, then send it only if auto_reply is on.

    The order matters: the draft is written to conversation_messages BEFORE
    any send is attempted, so a network failure mid-send leaves a recoverable
    draft rather than losing the composed text.

    `settings.enabled` is re-read by the engine immediately before calling
    this, and `auto_reply` is checked here -- so both switches are consulted
    as late as possible, and flipping either off between the start of a tick
    and this moment prevents the send.

    `now` is the tick's clock, and must be honored rather than re-read from
    the wall clock: the timestamp written here is what the follow-up spacing
    rule later measures against, so a message stamped with a different clock
    than the one that scheduled it makes that rule incoherent."""
    message_id = f"conv-{uuid.uuid4()}"
    now = now or utcnow()

    if not settings.auto_reply:
        # Composed, stored, and deliberately not sent. A person approves it.
        db.add_conversation_message(conn, ConversationMessage(
            message_id=message_id,
            item_id=item.item_id,
            direction="outbound",
            provider_message_id=None,
            in_reply_to=in_reply_to,
            subject=subject,
            body=body,
            from_email=cfg.from_email,
            to_email=to_email,
            draft_pending=True,
            created_at=now,
        ))
        return ComposedReply(
            subject=subject, body=body, draft_pending=True, message_id=message_id,
        )

    try:
        provider_message_id = send_via_channel(
            cfg, item.channel, to_email=to_email, subject=subject, body=body,
            in_reply_to=in_reply_to,
        )
    except SendError as exc:
        # Keep the draft so the work is not lost and a person can send it.
        db.add_conversation_message(conn, ConversationMessage(
            message_id=message_id,
            item_id=item.item_id,
            direction="outbound",
            in_reply_to=in_reply_to,
            subject=subject,
            body=body,
            from_email=cfg.from_email,
            to_email=to_email,
            draft_pending=True,
            created_at=now,
        ))
        return ComposedReply(
            subject=subject, body=body, draft_pending=True,
            message_id=message_id, error=str(exc),
        )

    db.add_conversation_message(conn, ConversationMessage(
        message_id=message_id,
        item_id=item.item_id,
        direction="outbound",
        provider_message_id=provider_message_id,
        in_reply_to=in_reply_to,
        subject=subject,
        body=body,
        from_email=cfg.from_email,
        to_email=to_email,
        draft_pending=False,
        sent_at=now,
        created_at=now,
    ))
    return ComposedReply(
        subject=subject, body=body, draft_pending=False,
        message_id=message_id, provider_message_id=provider_message_id,
    )


def send_pending_draft(
    conn: SupabaseConn, cfg: Config, item: OutreachItem, message: ConversationMessage,
) -> ComposedReply:
    """Send a draft a person has approved in the UI.

    This is the one send path that does NOT consult auto_reply: a human
    clicking send IS the authorization, and it stays available while the
    agent is switched off -- that is precisely how you work a conversation by
    hand with the agent stopped."""
    if not message.draft_pending:
        raise ValueError("this message has already been sent")

    to_email = message.to_email or item.to_email
    if not to_email:
        raise ValueError("no recipient address for this message")

    provider_message_id = send_via_channel(
        cfg, item.channel,
        to_email=to_email,
        subject=message.subject or "",
        body=message.body or "",
        # The draft's own in_reply_to, stored when it was composed (see
        # store_and_maybe_send) -- previously dropped here entirely, so a
        # draft approved and sent from the UI went out as a brand-new
        # thread instead of a reply, exactly the bug this fixes.
        in_reply_to=message.in_reply_to,
    )
    now = utcnow()
    db.update_conversation_message(
        conn, message.message_id,
        draft_pending=False,
        provider_message_id=provider_message_id,
        sent_at=now.isoformat(),
    )
    return ComposedReply(
        subject=message.subject or "", body=message.body or "",
        draft_pending=False, message_id=message.message_id,
        provider_message_id=provider_message_id,
    )
