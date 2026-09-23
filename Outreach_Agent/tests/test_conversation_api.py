"""The Conversation Agent's HTTP surface.

Two things are under test: that the routes are actually mounted on the
Outreach Agent's existing app (a wiring mistake here would be invisible to
every other test in the suite, since they all call the engine directly), and
that the endpoints enforce the same rules the engine does.

`db.connect` is patched to hand back the in-memory fake, so these run offline
like everything else.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from outreach import api, db
from outreach.models import Campaign, ImportedProspect, OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def client(conn, monkeypatch):
    """A TestClient whose every db.connect() returns the same fake."""
    monkeypatch.setattr(db, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(api.db, "connect", lambda *a, **kw: conn)
    from outreach.conversation import routes as conv_routes
    monkeypatch.setattr(conv_routes.db, "connect", lambda *a, **kw: conn)
    return TestClient(api.app)


@pytest.fixture
def seeded(conn):
    db.create_campaign(conn, Campaign(campaign_id="camp-1", name="Test", created_at=NOW))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", name="Acme", contact_email="cto@acme.com",
    ))
    db.create_item(conn, OutreachItem(
        item_id="item-1", campaign_id="camp-1", research_domain="acme.com",
        stage="sent", to_email="cto@acme.com", sent_at=NOW,
        created_at=NOW, stage_updated_at=NOW,
    ))
    return conn


# ---------------------------------------------------------------------------
# mounting
# ---------------------------------------------------------------------------

def test_conversation_routes_are_mounted():
    """The router is wired into the Outreach Agent's app. Every other
    conversation test calls the engine directly, so only this would catch a
    missing include_router."""
    paths = TestClient(api.app).get("/openapi.json").json()["paths"]
    for expected in (
        "/campaigns/{campaign_id}/conversation/settings",
        "/campaigns/{campaign_id}/conversation/tick",
        "/items/{item_id}/conversation",
        "/conversation/messages/{message_id}/send",
        "/conversation/messages/{message_id}",
    ):
        assert expected in paths, f"{expected} is not mounted"


def test_existing_outreach_routes_still_mounted():
    """Adding the router must not disturb the Outreach Agent's own API."""
    paths = TestClient(api.app).get("/openapi.json").json()["paths"]
    for expected in ("/campaigns", "/prospects", "/import", "/items/{item_id}/stage"):
        assert expected in paths


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_unconfigured_campaign_reads_as_disabled(client, seeded):
    """No settings row is not an error -- the UI can render the switch
    before one exists."""
    r = client.get("/campaigns/camp-1/conversation/settings")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["auto_reply"] is False


def test_settings_for_unknown_campaign_is_404(client, seeded):
    assert client.get("/campaigns/nope/conversation/settings").status_code == 404


def test_toggle_on_and_off(client, seeded):
    on = client.post("/campaigns/camp-1/conversation/settings", json={"enabled": True})
    assert on.json()["enabled"] is True
    # Switching on must not, by itself, authorize sending.
    assert on.json()["auto_reply"] is False

    off = client.post("/campaigns/camp-1/conversation/settings", json={"enabled": False})
    assert off.json()["enabled"] is False


def test_partial_update_preserves_other_fields(client, seeded):
    """The UI's toggle sends only `enabled` and must not clobber the rest."""
    client.post("/campaigns/camp-1/conversation/settings", json={
        "enabled": True, "auto_reply": True, "followup_after_days": 7, "max_followups": 4,
    })
    r = client.post("/campaigns/camp-1/conversation/settings", json={"enabled": False})

    body = r.json()
    assert body["enabled"] is False
    assert body["auto_reply"] is True
    assert body["followup_after_days"] == 7
    assert body["max_followups"] == 4


@pytest.mark.parametrize("payload", [
    {"followup_after_days": 0},
    {"followup_after_days": -1},
    {"max_followups": -1},
])
def test_rejects_nonsensical_settings(client, seeded, payload):
    assert client.post("/campaigns/camp-1/conversation/settings", json=payload).status_code == 400


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------

def test_tick_on_a_disabled_campaign_is_a_no_op_not_an_error(client, seeded):
    """The UI needs to explain why nothing happened, so this is a 200 with
    enabled=false rather than a 4xx."""
    r = client.post("/campaigns/camp-1/conversation/tick")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert db.get_item(seeded, "item-1").stage == "sent"


def test_tick_reports_missing_imap_configuration(client, seeded, monkeypatch):
    """A missing mailbox config is surfaced clearly, not swallowed."""
    monkeypatch.delenv("IMAP_HOST", raising=False)
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    client.post("/campaigns/camp-1/conversation/settings", json={"enabled": True})

    r = client.post("/campaigns/camp-1/conversation/tick")

    assert r.status_code == 200
    assert "IMAP" in (r.json()["message"] or "")


def test_tick_for_unknown_campaign_is_404(client, seeded):
    assert client.post("/campaigns/nope/conversation/tick").status_code == 404


# ---------------------------------------------------------------------------
# thread + drafts
# ---------------------------------------------------------------------------

def test_item_conversation_is_empty_before_any_reply(client, seeded):
    r = client.get("/items/item-1/conversation")
    assert r.status_code == 200
    assert r.json()["messages"] == []
    assert r.json()["pending_drafts"] == []
    assert r.json()["stage"] == "sent"


def test_item_conversation_for_unknown_item_is_404(client, seeded):
    assert client.get("/items/nope/conversation").status_code == 404


def test_sending_an_unknown_draft_is_404(client, seeded):
    assert client.post("/conversation/messages/nope/send").status_code == 404


def test_cannot_discard_an_already_sent_message(client, seeded, conn):
    """Sent mail is history and stays on the record -- only unsent drafts can
    be discarded."""
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-1", item_id="item-1", direction="outbound",
        provider_message_id="<sent@csg>", subject="Hi", body="Body",
        draft_pending=False, sent_at=NOW, created_at=NOW,
    ))

    assert client.delete("/conversation/messages/m-1").status_code == 400


def test_discarding_a_pending_draft_removes_it(client, seeded, conn):
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-2", item_id="item-1", direction="outbound",
        subject="Draft", body="Body", draft_pending=True, created_at=NOW,
    ))

    assert client.delete("/conversation/messages/m-2").status_code == 200
    assert db.get_conversation_message(conn, "m-2") is None


def test_approving_a_draft_sends_it_even_while_the_agent_is_off(client, seeded, conn, monkeypatch):
    """A person clicking send IS the authorization -- this is how you work a
    conversation by hand with the agent switched off."""
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-3", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Body", to_email="cto@acme.com",
        draft_pending=True, created_at=NOW,
    ))

    sent = []
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel",
        lambda *a, **kw: sent.append(kw) or "<sent@csg>",
    )

    r = client.post("/conversation/messages/m-3/send")

    assert r.status_code == 200
    assert len(sent) == 1
    assert db.get_conversation_message(conn, "m-3").draft_pending is False


def test_cannot_send_the_same_draft_twice(client, seeded, conn, monkeypatch):
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-4", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Body", to_email="cto@acme.com",
        draft_pending=True, created_at=NOW,
    ))
    monkeypatch.setattr(
        "outreach.conversation.responder.send_via_channel", lambda *a, **kw: "<sent@csg>",
    )

    assert client.post("/conversation/messages/m-4/send").status_code == 200
    assert client.post("/conversation/messages/m-4/send").status_code == 400


# ---------------------------------------------------------------------------
# clean_body / on-demand drafting
# ---------------------------------------------------------------------------

def test_item_conversation_includes_clean_body(client, seeded, conn):
    """Regression test: the drawer used to render the raw body, quoted
    original and all -- clean_body must be present and actually stripped."""
    from outreach.models import ConversationMessage
    body = (
        "yes its live\r\n\r\n"
        "On Sat, 19 Sept 2026, CSG Outreach <affan@careersourcegroup.com>\r\n"
        "wrote:\r\n\r\n> Original pitch text.\r\n"
    )
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-5", item_id="item-1", direction="inbound",
        provider_message_id="<r@acme.com>", subject="Re: Hi", body=body,
        from_email="cto@acme.com", received_at=NOW, created_at=NOW,
    ))

    r = client.get("/items/item-1/conversation")
    message = r.json()["messages"][0]

    assert message["body"] == body               # untouched, full original
    assert message["clean_body"] == "yes its live"  # stripped for display


def test_outbound_message_clean_body_equals_body(client, seeded, conn):
    """An outbound message never carries a quoted original -- clean_body
    must not alter it."""
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-6", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Sure, happy to help.",
        draft_pending=True, created_at=NOW,
    ))

    r = client.get("/items/item-1/conversation")
    message = r.json()["messages"][0]
    assert message["clean_body"] == message["body"] == "Sure, happy to help."


def test_draft_reply_on_demand(client, seeded, conn, monkeypatch):
    """The drawer's "Draft a reply" button: composes and stores a draft for
    the item's latest inbound message, without needing a fresh tick."""
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-7", item_id="item-1", direction="inbound",
        provider_message_id="<r@acme.com>", subject="Re: Hi",
        body="Sounds good, tell me more.", from_email="cto@acme.com",
        intent="interested", received_at=NOW, created_at=NOW,
    ))
    from outreach.conversation import responder
    monkeypatch.setattr(
        responder, "compose_reply",
        lambda *a, **kw: ("Re: Hi", "Happy to help -- here's a link: https://calendly.com/x"),
    )

    r = client.post("/items/item-1/conversation/draft")

    assert r.status_code == 200
    assert r.json()["draft_pending"] is True
    drafts = db.list_pending_drafts(conn, "item-1")
    assert len(drafts) == 1
    assert "calendly.com" in drafts[0].body


def test_draft_reply_requires_an_inbound_message(client, seeded):
    assert client.post("/items/item-1/conversation/draft").status_code == 400


def test_draft_reply_refuses_when_one_already_pending(client, seeded, conn):
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-8", item_id="item-1", direction="inbound",
        provider_message_id="<r@acme.com>", body="Hi", from_email="cto@acme.com",
        received_at=NOW, created_at=NOW,
    ))
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-9", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Draft body", draft_pending=True, created_at=NOW,
    ))

    r = client.post("/items/item-1/conversation/draft")
    assert r.status_code == 400
    assert "already pending" in r.json()["detail"]


def test_draft_reply_surfaces_llm_failure(client, seeded, conn, monkeypatch):
    """compose_reply returning None (no LLM available, or an unparseable
    response) must be a clear, actionable error, not a silent no-op."""
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="m-10", item_id="item-1", direction="inbound",
        provider_message_id="<r@acme.com>", body="Hi", from_email="cto@acme.com",
        received_at=NOW, created_at=NOW,
    ))
    from outreach.conversation import responder
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: None)

    r = client.post("/items/item-1/conversation/draft")
    assert r.status_code == 502


def test_draft_reply_for_unknown_item_is_404(client, seeded):
    assert client.post("/items/nope/conversation/draft").status_code == 404


# ---------------------------------------------------------------------------
# revise / redraft a pending reply draft
# ---------------------------------------------------------------------------

def _seed_pending_draft(conn, *, in_reply_to: str = "<r@acme.com>") -> None:
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="in-1", item_id="item-1", direction="inbound",
        provider_message_id="<r@acme.com>", subject="Re: Hi",
        body="Sounds good, tell me more.", from_email="cto@acme.com",
        intent="interested", received_at=NOW, created_at=NOW,
    ))
    db.add_conversation_message(conn, ConversationMessage(
        message_id="draft-1", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Happy to help.", in_reply_to=in_reply_to,
        draft_pending=True, created_at=NOW,
    ))


def test_revise_draft_edits_in_place(client, seeded, conn, monkeypatch):
    _seed_pending_draft(conn)
    from outreach.conversation import responder
    monkeypatch.setattr(
        responder, "revise_reply",
        lambda *a, **kw: ("Re: Hi", "Happy to help -- shorter version."),
    )

    r = client.post("/conversation/messages/draft-1/revise", json={"instruction": "make it shorter"})

    assert r.status_code == 200
    assert r.json()["body"] == "Happy to help -- shorter version."
    stored = db.get_conversation_message(conn, "draft-1")
    assert stored.body == "Happy to help -- shorter version."
    assert stored.draft_pending is True  # revising must not send it


def test_revise_draft_requires_pending(client, seeded, conn):
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="sent-1", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Already sent.", draft_pending=False,
        sent_at=NOW, created_at=NOW,
    ))
    r = client.post("/conversation/messages/sent-1/revise", json={"instruction": "shorter"})
    assert r.status_code == 400


def test_revise_draft_surfaces_llm_failure(client, seeded, conn, monkeypatch):
    _seed_pending_draft(conn)
    from outreach.conversation import responder
    monkeypatch.setattr(responder, "revise_reply", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no LLM available")))

    r = client.post("/conversation/messages/draft-1/revise", json={"instruction": "shorter"})
    assert r.status_code == 502


def test_redraft_draft_regenerates(client, seeded, conn, monkeypatch):
    _seed_pending_draft(conn)
    from outreach.conversation import responder
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: ("Re: Hi", "A completely different draft."))

    r = client.post("/conversation/messages/draft-1/redraft")

    assert r.status_code == 200
    assert r.json()["body"] == "A completely different draft."
    stored = db.get_conversation_message(conn, "draft-1")
    assert stored.body == "A completely different draft."
    assert stored.draft_pending is True


def test_redraft_requires_pending(client, seeded, conn):
    from outreach.models import ConversationMessage
    db.add_conversation_message(conn, ConversationMessage(
        message_id="sent-2", item_id="item-1", direction="outbound",
        subject="Re: Hi", body="Already sent.", draft_pending=False,
        sent_at=NOW, created_at=NOW,
    ))
    assert client.post("/conversation/messages/sent-2/redraft").status_code == 400


def test_redraft_surfaces_llm_failure(client, seeded, conn, monkeypatch):
    _seed_pending_draft(conn)
    from outreach.conversation import responder
    monkeypatch.setattr(responder, "compose_reply", lambda *a, **kw: None)

    r = client.post("/conversation/messages/draft-1/redraft")
    assert r.status_code == 502


def test_revise_unknown_message_is_404(client, seeded):
    assert client.post("/conversation/messages/nope/revise", json={"instruction": "x"}).status_code == 404


def test_redraft_unknown_message_is_404(client, seeded):
    assert client.post("/conversation/messages/nope/redraft").status_code == 404


# ---------------------------------------------------------------------------
# the Outreach Agent's own routes are unchanged
# ---------------------------------------------------------------------------

def test_manual_stage_route_still_works(client, seeded):
    """The pre-existing manual stage route is untouched and still available
    whether or not the Conversation Agent is on."""
    assert client.post("/items/item-1/stage", json={"stage": "replied"}).status_code == 200
    assert db.get_item(seeded, "item-1").stage == "replied"


def test_manual_stage_route_still_rejects_agent_owned_stages(client, seeded):
    assert client.post("/items/item-1/stage", json={"stage": "pending"}).status_code == 400


# ---------------------------------------------------------------------------
# not_interested ("Failed") via the manual stage route
# ---------------------------------------------------------------------------

def test_manual_stage_route_accepts_not_interested(client, seeded):
    """A person can manually mark a card Failed -- e.g. a rejection that
    arrived by phone rather than email."""
    r = client.post("/items/item-1/stage", json={"stage": "not_interested"})
    assert r.status_code == 200
    assert db.get_item(seeded, "item-1").stage == "not_interested"


def test_manual_stage_route_can_reopen_a_failed_item(client, seeded, conn):
    """The explicit manual escape hatch: not_interested is terminal for the
    Conversation Agent's own automated moves, but a person can always
    reclassify a wrongly-marked rejection back to an open stage through this
    same route -- it is not bound by CONVERSATION_TRANSITIONS."""
    db.set_stage(conn, "item-1", "not_interested", actor="conversation_agent")
    assert db.get_item(conn, "item-1").stage == "not_interested"

    r = client.post("/items/item-1/stage", json={"stage": "replied"})

    assert r.status_code == 200
    assert db.get_item(seeded, "item-1").stage == "replied"
