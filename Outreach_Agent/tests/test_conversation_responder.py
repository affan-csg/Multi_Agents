"""responder.compose_reply / compose_followup -- specifically that the
prompt actually sent to the LLM has the quoted original stripped out of the
reply text, and that a configured Calendly URL reaches the prompt (or a
sane fallback does when none is set)."""

from __future__ import annotations

from datetime import datetime

import pytest

from outreach.config import load_config
from outreach.conversation import responder
from outreach.models import ConversationMessage, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def item():
    return OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage="sent", to_email="cto@acme.com",
        subject="Hiring support for Acme", body="Original pitch.",
        provider_message_id="<orig@csg>", sent_at=NOW, created_at=NOW,
    )


@pytest.fixture
def prospect():
    return ImportedProspect(research_domain="acme.com", name="Acme", contact_name="Dana")


def _reply_with_quote(text: str) -> ConversationMessage:
    body = (
        f"{text}\r\n\r\n"
        "On Sat, 19 Sept 2026 at 19:31, CSG Outreach <affan@careersourcegroup.com>\r\n"
        "wrote:\r\n\r\n"
        "> This is our original pitch, quoted beneath the reply.\r\n"
        "> It should never reach the model as if the prospect wrote it.\r\n"
    )
    return ConversationMessage(
        message_id="in-1", item_id="item-1", direction="inbound",
        provider_message_id="<reply@acme.com>", subject="Re: Hiring support for Acme",
        body=body, from_email="cto@acme.com", received_at=NOW, created_at=NOW,
    )


def test_prompt_never_contains_the_quoted_original(cfg, item, prospect, monkeypatch):
    """The regression test for the actual bug: a raw reply body was being
    handed to the LLM (and shown in the UI) with the entire quoted pitch
    still attached."""
    captured = {}

    def fake_call_llm(prompt):
        captured["prompt"] = prompt
        return "SUBJECT: Re: Hiring support for Acme\nBODY: Sure, happy to chat.\n\nAffan"

    monkeypatch.setattr("outreach.conversation.responder.personalize._call_llm", fake_call_llm)

    latest = _reply_with_quote("Sounds good, tell me more")
    responder.compose_reply(cfg, item, prospect, [latest], latest, "interested")

    prompt = captured["prompt"]
    assert "Sounds good, tell me more" in prompt
    assert "quoted beneath the reply" not in prompt
    assert "our original pitch" not in prompt


def test_prior_thread_turns_are_also_stripped(cfg, item, prospect, monkeypatch):
    """Not just the latest reply -- every inbound turn walked into {{thread}}
    carries the same quoted-original problem."""
    captured = {}

    def fake_call_llm(prompt):
        captured["prompt"] = prompt
        return "SUBJECT: Re: x\nBODY: Sure.\n\nAffan"

    monkeypatch.setattr("outreach.conversation.responder.personalize._call_llm", fake_call_llm)

    first_turn = _reply_with_quote("First question")
    latest = _reply_with_quote("Second question")
    responder.compose_reply(cfg, item, prospect, [first_turn, latest], latest, "interested")

    prompt = captured["prompt"]
    assert "First question" in prompt
    assert "Second question" in prompt
    assert "quoted beneath the reply" not in prompt


def test_configured_calendly_url_reaches_the_prompt(cfg, item, prospect, monkeypatch):
    monkeypatch.setattr(type(cfg), "calendly_url", property(lambda self: "https://calendly.com/csg/intro"))
    captured = {}
    monkeypatch.setattr(
        "outreach.conversation.responder.personalize._call_llm",
        lambda p: captured.setdefault("prompt", p) and "SUBJECT: Re: x\nBODY: Sure.\n\nAffan",
    )

    latest = _reply_with_quote("Sounds good")
    responder.compose_reply(cfg, item, prospect, [latest], latest, "interested")

    assert "https://calendly.com/csg/intro" in captured["prompt"]


def test_no_calendly_url_falls_back_to_asking_for_times(cfg, item, prospect, monkeypatch):
    monkeypatch.setattr(type(cfg), "calendly_url", property(lambda self: ""))
    captured = {}
    monkeypatch.setattr(
        "outreach.conversation.responder.personalize._call_llm",
        lambda p: captured.setdefault("prompt", p) and "SUBJECT: Re: x\nBODY: Sure.\n\nAffan",
    )

    latest = _reply_with_quote("Sounds good")
    responder.compose_reply(cfg, item, prospect, [latest], latest, "interested")

    assert "ask for specific times instead" in captured["prompt"]


def test_compose_reply_returns_none_when_llm_unavailable(cfg, item, prospect, monkeypatch):
    monkeypatch.setattr("outreach.conversation.responder.personalize._call_llm", lambda p: None)

    latest = _reply_with_quote("Sounds good")
    assert responder.compose_reply(cfg, item, prospect, [latest], latest, "interested") is None


# ---------------------------------------------------------------------------
# subject drift -- the reply-threading bug
# ---------------------------------------------------------------------------
#
# The reply prompt asks the model to keep the thread's existing subject, but
# that is only an instruction, not a guarantee: a reply to "Quick question
# about your hiring pipeline" came back subjected "Re: Tech and AI talent
# delivery" -- a fluent but invented subject line. Gmail (and most clients)
# thread primarily on Subject + In-Reply-To/References together, so sending
# with that drifted subject landed the reply as a brand-new conversation in
# the recipient's inbox, even though matcher.py still resolves it as the same
# item server-side via the Message-ID headers alone. compose_reply and
# compose_followup must never let the model's own SUBJECT field reach the
# actual send -- the thread's real subject is the only source of truth.

def test_compose_reply_ignores_a_hallucinated_subject(cfg, item, prospect, monkeypatch):
    monkeypatch.setattr(
        "outreach.conversation.responder.personalize._call_llm",
        lambda p: "SUBJECT: Tech and AI talent delivery\nBODY: Sure, happy to chat.\n\nAffan",
    )

    latest = _reply_with_quote("Sounds good, tell me more")
    subject, _ = responder.compose_reply(cfg, item, prospect, [latest], latest, "interested")

    # item.subject is "Hiring support for Acme" -- the model's invented
    # "Tech and AI talent delivery" must never win.
    assert subject == "Re: Hiring support for Acme"


def test_compose_reply_prefers_the_latest_replys_subject_over_the_items(cfg, item, prospect, monkeypatch):
    """latest_reply.subject (their actual mail client's Re: line) is a more
    faithful thread anchor than item.subject when the two differ -- e.g. a
    prospect's client already normalized it -- and still wins regardless of
    whatever the model returned."""
    monkeypatch.setattr(
        "outreach.conversation.responder.personalize._call_llm",
        lambda p: "SUBJECT: Something else entirely\nBODY: Sure.\n\nAffan",
    )

    latest = _reply_with_quote("Sounds good")
    assert latest.subject == "Re: Hiring support for Acme"
    subject, _ = responder.compose_reply(cfg, item, prospect, [latest], latest, "interested")

    assert subject == "Re: Hiring support for Acme"


def test_compose_followup_ignores_a_hallucinated_subject(cfg, item, prospect, monkeypatch):
    monkeypatch.setattr(
        "outreach.conversation.responder.personalize._call_llm",
        lambda p: "SUBJECT: A totally different subject\nBODY: Just checking in.\n\nAffan",
    )

    subject, _ = responder.compose_followup(cfg, item, prospect, followup_number=1, max_followups=3)

    assert subject == "Re: Hiring support for Acme"


# ---------------------------------------------------------------------------
# in_reply_to actually reaching the send call -- the threading bug
# ---------------------------------------------------------------------------
#
# A reply approved and sent from the UI went out with no In-Reply-To header
# at all: it landed in the recipient's inbox as a brand-new conversation
# instead of threading under the message they had actually replied to.

def test_store_and_maybe_send_forwards_in_reply_to(cfg, item, conn, monkeypatch):
    from outreach.models import ConversationSettings

    captured = {}
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: captured.update(kw) or "<sent@csg>",
    )

    settings = ConversationSettings(campaign_id="camp-1", enabled=True, auto_reply=True)
    responder.store_and_maybe_send(
        conn, cfg, item, settings,
        subject="Re: Hi", body="Sure.", to_email="cto@acme.com",
        in_reply_to="<their-reply@acme.com>",
    )

    assert captured["in_reply_to"] == "<their-reply@acme.com>"


def test_send_pending_draft_forwards_the_drafts_own_in_reply_to(cfg, item, conn, monkeypatch):
    """The actual regression: a draft's in_reply_to was stored on the row
    when it was composed, but send_pending_draft (the "Approve & send"
    button's path) never read it back out before calling send_via_channel."""
    captured = {}
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: captured.update(kw) or "<sent@csg>",
    )

    draft = ConversationMessage(
        message_id="draft-1", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Sure.", to_email="cto@acme.com",
        in_reply_to="<their-reply@acme.com>", draft_pending=True, created_at=NOW,
    )

    responder.send_pending_draft(conn, cfg, item, draft)

    assert captured["in_reply_to"] == "<their-reply@acme.com>"


# ---------------------------------------------------------------------------
# should_reply -- the gate that keeps a rejection from ever getting an
# automated answer, regardless of auto_reply
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent,expected", [
    ("interested", True),
    ("booked", True),
    ("unclear", True),
    ("not_interested", False),
    ("out_of_office", False),
    ("bounce", False),
])
def test_should_reply(intent, expected):
    assert responder.should_reply(intent) is expected
