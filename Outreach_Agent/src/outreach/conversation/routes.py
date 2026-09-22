"""HTTP routes for the Conversation Agent.

Kept in its own APIRouter, mounted onto the Outreach Agent's existing app by
one `include_router` line in api.py. Same thin-plumbing convention as the
rest of that file: no business logic here, just request shapes and calls into
the engine.

Nothing in this module changes an existing route's behavior. Removing the
mount line would remove the Conversation Agent from the API entirely and
leave the Outreach Agent exactly as it was.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..channels import SendError
from ..config import load_config
from ..models import ConversationMessage
from . import engine
from .quoting import strip_quoted
from .settings import get_settings, set_settings

router = APIRouter(tags=["conversation"])


class ReviseDraftRequest(BaseModel):
    instruction: str  # free-text description of what to change, e.g. "make it shorter"


class ConversationSettingsRequest(BaseModel):
    """Every field optional so the UI's toggle can flip `enabled` alone,
    without having to know or resend the follow-up configuration."""

    enabled: Optional[bool] = None
    auto_reply: Optional[bool] = None
    followup_after_days: Optional[int] = None
    max_followups: Optional[int] = None


@router.get("/campaigns/{campaign_id}/conversation/settings")
def read_settings(campaign_id: str) -> dict[str, Any]:
    """This campaign's toggle state. An unconfigured campaign reads as
    disabled rather than 404-ing, so the UI can render the switch without
    a settings row having to exist first."""
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        return get_settings(conn, campaign_id).model_dump(mode="json")
    finally:
        conn.close()


@router.post("/campaigns/{campaign_id}/conversation/settings")
def write_settings(campaign_id: str, req: ConversationSettingsRequest) -> dict[str, Any]:
    """Update the toggle and/or the follow-up knobs.

    Writes only to conversation_settings: switching the agent off never
    re-stages an item, deletes a message, or resets a counter, so switching
    it back on resumes from the recorded state instead of replaying it."""
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")

        if req.followup_after_days is not None and req.followup_after_days < 1:
            raise HTTPException(400, "followup_after_days must be at least 1")
        if req.max_followups is not None and req.max_followups < 0:
            raise HTTPException(400, "max_followups cannot be negative")

        updated = set_settings(
            conn, campaign_id,
            enabled=req.enabled, auto_reply=req.auto_reply,
            followup_after_days=req.followup_after_days,
            max_followups=req.max_followups,
        )
        return updated.model_dump(mode="json")
    finally:
        conn.close()


@router.post("/campaigns/{campaign_id}/conversation/tick")
def run_tick(campaign_id: str) -> dict[str, Any]:
    """Run one conversation cycle now -- the "Check replies" button.

    Synchronous by design: a tick is a short IMAP fetch plus a handful of
    per-item decisions, and the button is more useful when it reports what
    actually happened than when it hands back a job id. Returns a no-op
    result (rather than an error) when the agent is off, so the UI can
    explain why nothing happened."""
    cfg = load_config()
    conn = db.connect()
    try:
        if not db.get_campaign(conn, campaign_id):
            raise HTTPException(404, f"{campaign_id} not found")
        return engine.tick(conn, cfg, campaign_id).as_dict()
    finally:
        conn.close()


def _message_payload(m: ConversationMessage) -> dict[str, Any]:
    """A message's stored fields plus `clean_body` for display.

    `clean_body` is display-only, computed at read time -- the stored `body`
    is never touched, so the full original (quoted history and all) is
    always there if a person wants it. Only inbound messages carry a quoted
    original worth stripping; an outbound message's body is exactly what was
    or will be sent, so it is passed through unchanged."""
    payload = m.model_dump(mode="json")
    payload["clean_body"] = strip_quoted(m.body or "") if m.direction == "inbound" else (m.body or "")
    return payload


@router.get("/items/{item_id}/conversation")
def item_conversation(item_id: str) -> dict[str, Any]:
    """The full message thread for one item, for the drawer's conversation
    view: every inbound reply and outbound message, oldest first, plus any
    drafts still awaiting approval. Each message includes `clean_body` --
    the reply with the quoted original stripped -- alongside the untouched
    `body`, so the drawer can show the readable version by default while the
    full original stays one click away."""
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")

        messages = db.list_conversation_messages(conn, item_id)
        settings = get_settings(conn, item.campaign_id)
        return {
            "item_id": item_id,
            "stage": item.stage,
            "followup_count": item.followup_count,
            "last_inbound_at": item.last_inbound_at.isoformat() if item.last_inbound_at else None,
            "enabled": settings.enabled,
            "auto_reply": settings.auto_reply,
            "messages": [_message_payload(m) for m in messages],
            "pending_drafts": [_message_payload(m) for m in messages if m.draft_pending],
        }
    finally:
        conn.close()


@router.post("/items/{item_id}/conversation/draft")
def draft_reply(item_id: str) -> dict[str, Any]:
    """Compose a reply to this item's latest inbound message right now, on
    demand -- the drawer's "Draft a reply" button.

    Exists as a manual escape hatch alongside the automatic drafting a tick
    already does for a brand-new reply: a tick only ever drafts once per
    inbound message (see engine._process_inbound, which runs on `new_inbound`
    messages only), so if that first attempt failed -- an LLM call errored,
    timed out, or the provider was briefly down -- there was previously no
    way to try again short of a fresh reply arriving. This calls the exact
    same responder.compose_reply used by the tick, against the same stored
    thread, so what it produces is not a different code path, just a retry
    of the same one.

    Requires the item to actually have an inbound message to answer, and
    refuses to draft a second one on top of a pending draft that already
    exists -- discard that one first (DELETE /conversation/messages/{id})
    if you want a different draft."""
    cfg = load_config()
    conn = db.connect()
    try:
        item = db.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, f"{item_id} not found")

        messages = db.list_conversation_messages(conn, item_id)
        inbound = [m for m in messages if m.direction == "inbound"]
        if not inbound:
            raise HTTPException(400, "no inbound reply on this item to draft an answer to")

        if any(m.draft_pending for m in messages):
            raise HTTPException(400, "a draft is already pending for this item -- discard it first")

        latest = inbound[-1]
        to_email = item.to_email or latest.from_email
        if not to_email:
            raise HTTPException(400, "no recipient address to reply to")

        from . import responder
        prospect = db.get_prospect(conn, item.research_domain)
        composed = responder.compose_reply(
            cfg, item, prospect, messages, latest, latest.intent or "unclear",
        )
        if composed is None:
            raise HTTPException(
                502,
                "could not draft a reply (no LLM available, or the call failed/returned "
                "an unparseable response) -- try again in a moment",
            )
        subject, body = composed

        settings = get_settings(conn, item.campaign_id)
        outcome = responder.store_and_maybe_send(
            conn, cfg, item, settings,
            subject=subject, body=body, to_email=to_email,
            in_reply_to=latest.provider_message_id,
        )
        if outcome.error:
            raise HTTPException(502, outcome.error)

        return {
            "status": "ok",
            "message_id": outcome.message_id,
            "draft_pending": outcome.draft_pending,
            "subject": subject,
            "body": body,
        }
    finally:
        conn.close()


def _inbound_this_draft_answers(
    messages: list[ConversationMessage], draft: ConversationMessage,
) -> Optional[ConversationMessage]:
    """The inbound message a pending draft is replying to, by its
    in_reply_to -- falling back to the latest inbound message on the item if
    that can't be resolved (e.g. an older draft predating in_reply_to being
    stored). Used by both revise and redraft so an edit/regenerate is always
    grounded in the same reply the original draft answered, not whatever is
    newest by the time someone clicks the button."""
    if draft.in_reply_to:
        for m in messages:
            if m.direction == "inbound" and m.provider_message_id == draft.in_reply_to:
                return m
    inbound = [m for m in messages if m.direction == "inbound"]
    return inbound[-1] if inbound else None


@router.post("/conversation/messages/{message_id}/revise")
def revise_draft(message_id: str, req: ReviseDraftRequest) -> dict[str, Any]:
    """Edit an already-drafted reply per a free-text instruction, in place --
    same contract as /items/{id}/revise for a cold-outreach draft: the
    existing wording is edited, not regenerated from scratch. Only ever acts
    on an unsent draft; a message already sent is history."""
    cfg = load_config()
    conn = db.connect()
    try:
        message = db.get_conversation_message(conn, message_id)
        if not message:
            raise HTTPException(404, f"{message_id} not found")
        if not message.draft_pending:
            raise HTTPException(400, "only an unsent draft can be revised")
        if not message.subject or not message.body:
            raise HTTPException(400, "this draft has no content yet to revise")

        item = db.get_item(conn, message.item_id)
        if not item:
            raise HTTPException(404, f"item {message.item_id} not found")

        from . import responder
        thread = db.list_conversation_messages(conn, item.item_id)
        latest_inbound = _inbound_this_draft_answers(thread, message)
        if not latest_inbound:
            raise HTTPException(400, "no inbound reply on this item for context")

        prospect = db.get_prospect(conn, item.research_domain)
        learning_ctx = None
        try:
            from ..learning.context import get_learning_context_summary
            learning_ctx = get_learning_context_summary(conn)
        except Exception:
            pass  # learning unavailable -- draft proceeds unmodified, see rl_prompt.md section 12
        before_subject, before_body = message.subject, message.body
        try:
            subject, body = responder.revise_reply(
                cfg, item, prospect,
                latest_reply=latest_inbound,
                current_subject=message.subject, current_body=message.body,
                instruction=req.instruction, learning_context=learning_ctx,
            )
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))

        db.update_conversation_message(conn, message_id, subject=subject, body=body)
        try:
            from ..learning.db import record_draft_edit
            record_draft_edit(
                conn, item_id=item.item_id, campaign_id=item.campaign_id, source="conversation_revise",
                before_subject=before_subject, before_body=before_body,
                after_subject=subject, after_body=body,
                instruction=req.instruction, message_id=message_id,
                prospect=prospect.model_dump(mode="json") if prospect else None,
            )
        except Exception:
            pass  # learning write failure must never break the revise response
        return {"status": "ok", "message_id": message_id, "subject": subject, "body": body}
    finally:
        conn.close()


@router.post("/conversation/messages/{message_id}/redraft")
def redraft_draft(message_id: str) -> dict[str, Any]:
    """Fully regenerate a pending draft from scratch -- unlike /revise, this
    throws away the current wording rather than editing it. Same
    'try again with a fresh attempt' idea as the cold-outreach redraft
    button, applied to a conversation reply."""
    cfg = load_config()
    conn = db.connect()
    try:
        message = db.get_conversation_message(conn, message_id)
        if not message:
            raise HTTPException(404, f"{message_id} not found")
        if not message.draft_pending:
            raise HTTPException(400, "only an unsent draft can be redrafted")

        item = db.get_item(conn, message.item_id)
        if not item:
            raise HTTPException(404, f"item {message.item_id} not found")

        from . import responder
        thread = db.list_conversation_messages(conn, item.item_id)
        latest_inbound = _inbound_this_draft_answers(thread, message)
        if not latest_inbound:
            raise HTTPException(400, "no inbound reply on this item to draft an answer to")

        prospect = db.get_prospect(conn, item.research_domain)
        learning_ctx = None
        try:
            from ..learning.context import get_learning_context_summary
            learning_ctx = get_learning_context_summary(conn)
        except Exception:
            pass  # learning unavailable -- draft proceeds unmodified, see rl_prompt.md section 12
        before_subject, before_body = message.subject, message.body
        composed = responder.compose_reply(
            cfg, item, prospect, thread, latest_inbound, latest_inbound.intent or "unclear",
            learning_context=learning_ctx,
        )
        if composed is None:
            raise HTTPException(
                502,
                "could not draft a reply (no LLM available, or the call failed/returned "
                "an unparseable response) -- try again in a moment",
            )
        subject, body = composed
        db.update_conversation_message(conn, message_id, subject=subject, body=body)
        try:
            from ..learning.db import record_draft_edit
            record_draft_edit(
                conn, item_id=item.item_id, campaign_id=item.campaign_id, source="conversation_redraft",
                before_subject=before_subject, before_body=before_body,
                after_subject=subject, after_body=body, message_id=message_id,
                prospect=prospect.model_dump(mode="json") if prospect else None,
            )
        except Exception:
            pass  # learning write failure must never break the redraft response
        return {"status": "ok", "message_id": message_id, "subject": subject, "body": body}
    finally:
        conn.close()


@router.post("/conversation/messages/{message_id}/send")
def send_draft(message_id: str) -> dict[str, Any]:
    """Send a drafted reply a person has approved.

    Deliberately NOT gated on `enabled` or `auto_reply`: a human clicking
    send is the authorization. This is what lets you work a conversation by
    hand while the agent is switched off -- the drafts it already wrote stay
    sendable."""
    cfg = load_config()
    conn = db.connect()
    try:
        message = db.get_conversation_message(conn, message_id)
        if not message:
            raise HTTPException(404, f"{message_id} not found")
        if not message.draft_pending:
            raise HTTPException(400, "this message has already been sent")

        item = db.get_item(conn, message.item_id)
        if not item:
            raise HTTPException(404, f"item {message.item_id} not found")

        from . import responder
        try:
            outcome = responder.send_pending_draft(conn, cfg, item, message)
        except (SendError, ValueError) as exc:
            raise HTTPException(502, str(exc))

        return {
            "status": "ok",
            "message_id": outcome.message_id,
            "provider_message_id": outcome.provider_message_id,
        }
    finally:
        conn.close()


@router.delete("/conversation/messages/{message_id}")
def discard_draft(message_id: str) -> dict[str, str]:
    """Discard a drafted reply a person does not want to send. Only ever
    removes an unsent draft -- a message that was actually sent is history
    and stays on the record."""
    conn = db.connect()
    try:
        message = db.get_conversation_message(conn, message_id)
        if not message:
            raise HTTPException(404, f"{message_id} not found")
        if not message.draft_pending:
            raise HTTPException(400, "only unsent drafts can be discarded")
        conn.delete("conversation_messages", message_id=f"eq.{message_id}")
        return {"status": "ok"}
    finally:
        conn.close()
