"""Reply classification, and the matching that decides whose reply it is.

Bounce and auto-responder detection is deterministic and is tested as such --
no LLM is involved, because getting those two wrong is what corrupts the
funnel, and they are perfectly detectable from headers and subjects.

The LLM-backed path is tested through a stubbed `_call_llm`: what is under
test is the parsing and the degradation behavior, not a model's judgment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from outreach.config import load_config
from outreach.conversation import classifier, matcher
from outreach.conversation.inbox import InboundEmail, parse_message
from outreach.models import OutreachItem

NOW = datetime(2026, 3, 10, 12, 0, 0)


@pytest.fixture
def cfg():
    return load_config()


def _email(**kw) -> InboundEmail:
    defaults = dict(
        message_id="<r1@acme.com>", in_reply_to="<orig@csg>", references=[],
        from_email="cto@acme.com", to_email="outreach@csg.com",
        subject="Re: Hiring support", body="Sounds good.", received_at=NOW,
    )
    defaults.update(kw)
    return InboundEmail(**defaults)


# ---------------------------------------------------------------------------
# deterministic detection -- never reaches the LLM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject", [
    "Out of Office: Re: Hiring support",
    "Automatic reply: Hiring support",
    "Autoreply — away from my desk",
    "I am on vacation until Monday",
    "On leave until the 14th",
])
def test_detects_out_of_office_from_subject(cfg, subject, monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    assert classifier.classify(cfg, _email(subject=subject)) == "out_of_office"


def test_detects_out_of_office_from_headers(cfg, monkeypatch):
    """RFC 3834's Auto-Submitted header is decisive on its own, even when the
    subject looks like an ordinary reply."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    assert classifier.classify(
        cfg, _email(subject="Re: Hiring support"),
        headers={"Auto-Submitted": "auto-replied"},
    ) == "out_of_office"


def test_auto_submitted_no_is_not_an_auto_reply(cfg, monkeypatch):
    """`Auto-Submitted: no` is the explicit "a human sent this" value and must
    not be read as evidence of automation."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: "INTENT: interested")
    assert classifier.classify(
        cfg, _email(), headers={"Auto-Submitted": "no"},
    ) == "interested"


@pytest.mark.parametrize("sender,subject", [
    ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)"),
    ("postmaster@acme.com", "Undeliverable: Hiring support"),
    ("cto@acme.com", "Mail delivery failed: returning message to sender"),
    ("noreply@acme.com", "Address not found"),
])
def test_detects_bounces(cfg, sender, subject, monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    assert classifier.classify(cfg, _email(from_email=sender, subject=subject)) == "bounce"


def test_ordinary_noreply_sender_is_not_a_bounce(cfg, monkeypatch):
    """A human replying from a no-reply alias is still a human. Without a
    bounce-shaped subject, the sender alone proves nothing."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: "INTENT: interested")
    assert classifier.classify(
        cfg, _email(from_email="noreply@acme.com", subject="Re: Hiring support"),
    ) == "interested"


# ---------------------------------------------------------------------------
# scheduling-link booking confirmations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender,subject", [
    ("notifications@calendly.com", "New Event: Dana Scully - 30 Minute Meeting"),
    ("calendly.com", "Confirmed: CSG Outreach and Dana Scully"),
    ("calendar-notification@google.com", "Invitation: Call w/ CSG @ Tue Mar 10"),
])
def test_scheduling_notification_is_classified_booked(cfg, sender, subject, monkeypatch):
    """A booking made through the scheduling link usually arrives as a
    notification FROM the tool, not prose from the prospect -- this must be
    recognized as `booked` without ever reaching the LLM, since there may be
    no human-written text in the message at all."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    assert classifier.classify(cfg, _email(from_email=sender, subject=subject, body="")) == "booked"


def test_scheduling_notification_beats_bounce_and_autoreply_checks(cfg, monkeypatch):
    """Decided before, and separately from, the bounce/auto-reply checks --
    an automated Calendly sender must never be misread as one of those."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    result = classifier.classify(
        cfg, _email(from_email="notifications@calendly.com", subject="New Event: 30 Minute Meeting", body=""),
    )
    assert result == "booked"
    assert result not in ("bounce", "out_of_office")


def test_ordinary_reply_is_not_mistaken_for_a_booking_confirmation(cfg, monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: "INTENT: interested")
    assert classifier.classify(cfg, _email(from_email="cto@acme.com", subject="Re: Hiring support")) == "interested"


# ---------------------------------------------------------------------------
# LLM-backed classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response,expected", [
    ("INTENT: interested", "interested"),
    ("INTENT: booked", "booked"),
    ("INTENT: not_interested", "not_interested"),
    ("INTENT: unclear", "unclear"),
    ("intent: BOOKED", "booked"),
    ("booked", "booked"),                      # bare value, no label
    ("The intent here is interested.", "interested"),
])
def test_parses_intent_from_model_output(cfg, monkeypatch, response, expected):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: response)
    assert classifier.classify(cfg, _email()) == expected


def test_unparseable_response_degrades_to_unclear(cfg, monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: "I'm not sure what you mean.")
    assert classifier.classify(cfg, _email()) == "unclear"


def test_no_llm_available_degrades_to_unclear(cfg, monkeypatch):
    """Losing classification must never mean losing the reply: `unclear`
    still moves the card to Replied so a person sees it."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: None)
    assert classifier.classify(cfg, _email()) == "unclear"


def test_empty_body_is_unclear(cfg, monkeypatch):
    monkeypatch.setattr(classifier, "_call_llm", lambda p: pytest.fail("LLM must not be called"))
    assert classifier.classify(cfg, _email(body="   ")) == "unclear"


# ---------------------------------------------------------------------------
# not_interested -- the intent that routes a card to the terminal Failed
# stage (see conversation/engine.py's _INTENT_STAGE). The judgment call of
# "is this wording a rejection" is the LLM's job and cannot be exercised
# offline; what's tested here is that classify() correctly carries whatever
# the model decides through to the returned intent, for the range of
# rejection phrasings the prompt itself calls out as examples (see
# prompts/classify_reply.md) -- i.e. the parsing/routing plumbing does not
# lose or mangle a rejection call the model already made correctly.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "Not interested, thanks.",
    "We're not looking for this right now.",
    "No thanks, please don't contact us again.",
    "We don't need your services at this time.",
    "Please remove me from this list.",
    "Not a fit for us, sorry.",
    "Unsubscribe me please.",
    "We already have a vendor for this.",
])
def test_classify_returns_not_interested_for_rejection_replies(cfg, monkeypatch, body):
    """Simulates the model correctly judging each of these as a rejection --
    classify() must carry that verdict through unchanged."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: "INTENT: not_interested")
    assert classifier.classify(cfg, _email(body=body)) == "not_interested"


@pytest.mark.parametrize("body,model_verdict", [
    # A referral keeps the conversation alive with someone else -- interested,
    # not a rejection, per the prompt's own explicit instruction.
    ("Please talk to our Head of Talent, cc'd here.", "interested"),
    # Deferral, not decline -- the prompt explicitly calls this out as
    # interested, since the door is left open.
    ("Not right now, check back in six months.", "interested"),
    # A genuine open question is active engagement, not a decline.
    ("What's your pricing model look like?", "interested"),
    # A polite acknowledgement with no forward motion is unclear, not a
    # rejection -- the prompt explicitly reserves not_interested for a CLEAR
    # no.
    ("Thanks for reaching out.", "unclear"),
])
def test_ambiguous_or_positive_replies_do_not_become_not_interested(cfg, monkeypatch, body, model_verdict):
    """The flip side of the above: simulates the model correctly judging each
    of these as NOT a rejection (per prompts/classify_reply.md's own
    examples), and confirms classify() does not somehow coerce them into
    not_interested -- these must remain in the normal Replied flow, never the
    terminal Failed stage."""
    monkeypatch.setattr(classifier, "_call_llm", lambda p: f"INTENT: {model_verdict}")
    assert classifier.classify(cfg, _email(body=body)) == model_verdict
    assert classifier.classify(cfg, _email(body=body)) != "not_interested"


def test_classify_reply_prompt_reserves_not_interested_for_a_clear_no():
    """Guards the prompt itself, not just the parser: the instructions the
    model is given must keep steering ambiguous replies away from
    not_interested (this is the actual safeguard against misclassifying a
    genuine question or a neutral reply as a rejection, since the parsing
    logic alone has no way to know)."""
    template = classifier._PROMPT_PATH.read_text(encoding="utf-8")
    assert "a clear no" in template
    assert "referral" in template.lower()
    assert "check back" in template.lower()
    assert "Err toward `unclear`" in template


# ---------------------------------------------------------------------------
# quoted-text stripping
# ---------------------------------------------------------------------------

def test_strips_quoted_original_before_classifying():
    """A reply carries our whole pitch underneath it. Left in, the model sees
    more of our text than theirs and misreads short rejections as interest."""
    body = (
        "Not for us, thanks.\n\n"
        "On Mon, Mar 9, 2026 at 9:00 AM Affan Jan <affan@csg.com> wrote:\n"
        "> We help SaaS companies hire engineers faster and would love to talk\n"
        "> about your open roles and how we can help you scale the team.\n"
    )
    stripped = classifier._strip_quoted(body)
    assert stripped == "Not for us, thanks."
    assert "csg.com" not in stripped


@pytest.mark.parametrize("marker", [
    "\n-----Original Message-----\n",
    "\n_____________\n",
    "\nFrom: Affan Jan\n",
])
def test_strips_other_quote_styles(marker):
    body = f"Interested.{marker}Our original pitch text here."
    assert classifier._strip_quoted(body).strip() == "Interested."


def test_keeps_body_when_stripping_would_empty_it():
    """An oddly top-quoted reply must not be reduced to nothing -- better to
    classify the full text than an empty string."""
    body = "On Mon, Mar 9, 2026, someone wrote:\n> the original"
    assert classifier._strip_quoted(body).strip() != ""


# ---------------------------------------------------------------------------
# matching a reply to its item
# ---------------------------------------------------------------------------

def _item(item_id="item-1", to_email="cto@acme.com", provider_message_id="<orig@csg>",
          stage="sent", sent_at=NOW):
    return OutreachItem(
        item_id=item_id, campaign_id="camp-1", research_domain="acme.com",
        stage=stage, to_email=to_email, provider_message_id=provider_message_id,
        sent_at=sent_at, created_at=sent_at,
    )


def test_matches_on_in_reply_to():
    items = [_item()]
    assert matcher.match_to_item(_email(in_reply_to="<orig@csg>"), items).item_id == "item-1"


def test_matches_ignoring_angle_brackets():
    """Providers are inconsistent about storing the brackets; `<a@b>` and
    `a@b` must compare equal or threading silently fails."""
    items = [_item(provider_message_id="orig@csg")]
    assert matcher.match_to_item(_email(in_reply_to="<orig@csg>"), items) is not None


def test_matches_on_references_when_in_reply_to_is_absent():
    items = [_item()]
    email_in = _email(in_reply_to=None, references=["<other@x>", "<orig@csg>"])
    assert matcher.match_to_item(email_in, items).item_id == "item-1"


def test_falls_back_to_sender_address():
    """For clients that drop the threading headers entirely."""
    items = [_item(provider_message_id=None)]
    email_in = _email(in_reply_to=None, references=[])
    assert matcher.match_to_item(email_in, items).item_id == "item-1"


def test_sender_match_is_case_insensitive():
    items = [_item(to_email="CTO@Acme.com")]
    email_in = _email(in_reply_to=None, references=[], from_email="cto@acme.com")
    assert matcher.match_to_item(email_in, items) is not None


def test_ambiguous_sender_resolves_to_most_recent_send():
    """The same person in two campaigns: they are answering the latest one."""
    old = _item(item_id="old", provider_message_id=None, sent_at=NOW - timedelta(days=30))
    new = _item(item_id="new", provider_message_id=None, sent_at=NOW - timedelta(days=1))
    email_in = _email(in_reply_to=None, references=[])
    assert matcher.match_to_item(email_in, [old, new]).item_id == "new"


def test_unmatched_email_returns_none():
    """Ordinary mail in the same mailbox must not be attached to a card."""
    items = [_item()]
    stranger = _email(in_reply_to=None, references=[], from_email="someone@elsewhere.com")
    assert matcher.match_to_item(stranger, items) is None


def test_matches_reply_to_a_followup_via_outbound_index():
    """A reply to our follow-up threads to the follow-up's Message-ID, not the
    original outreach -- strategy 2."""
    items = [_item(provider_message_id="<orig@csg>")]
    index = {"followup-1@csg": "item-1"}
    email_in = _email(in_reply_to="<followup-1@csg>", from_email="someone@elsewhere.com")
    assert matcher.match_to_item(email_in, items, index).item_id == "item-1"


# ---------------------------------------------------------------------------
# strategy 4: Reply-To -- a scheduling tool's booking notification
# ---------------------------------------------------------------------------
#
# Regression coverage for a real live booking: a Calendly "New Event"
# notification is FROM notifications@calendly.com and TO the account owner
# -- neither header names the prospect, so strategies 1-3 all miss it. The
# actual invitee's address is in Reply-To specifically for this reason.

def test_matches_a_calendly_style_notification_via_reply_to():
    items = [_item(to_email="cto@acme.com", provider_message_id=None)]
    notification = _email(
        in_reply_to=None, references=[],
        from_email="notifications@calendly.com", to_email="affan@careersourcegroup.com",
        subject="New Event: 30 Minute Meeting", reply_to="cto@acme.com",
    )
    matched = matcher.match_to_item(notification, items)
    assert matched is not None
    assert matched.item_id == "item-1"


def test_reply_to_only_tried_after_sender_address_fails():
    """Strategy 3 (sender) is tried first; Reply-To is a fallback, not a
    replacement -- an ordinary reply where From already matches must not
    even consult Reply-To."""
    items = [_item(to_email="cto@acme.com", provider_message_id=None)]
    email_in = _email(
        in_reply_to=None, references=[],
        from_email="cto@acme.com", reply_to="someone-else@elsewhere.com",
    )
    matched = matcher.match_to_item(email_in, items)
    assert matched is not None
    assert matched.item_id == "item-1"


def test_reply_to_identical_to_sender_does_not_double_match_oddly():
    """A normal mail client that just echoes From into Reply-To must not
    behave any differently than if Reply-To were absent."""
    items = [_item(to_email="cto@acme.com", provider_message_id=None)]
    email_in = _email(
        in_reply_to=None, references=[],
        from_email="cto@acme.com", reply_to="cto@acme.com",
    )
    assert matcher.match_to_item(email_in, items).item_id == "item-1"


def test_unrelated_reply_to_does_not_create_a_false_match():
    """Reply-To pointing at yet another unrelated address must not match
    anything just because SOME address was present."""
    items = [_item(to_email="cto@acme.com", provider_message_id=None)]
    stranger = _email(
        in_reply_to=None, references=[],
        from_email="someone@elsewhere.com", reply_to="another-stranger@elsewhere.com",
    )
    assert matcher.match_to_item(stranger, items) is None


def test_ambiguous_reply_to_resolves_to_most_recent_send():
    old = _item(item_id="old", provider_message_id=None, sent_at=NOW - timedelta(days=30))
    new = _item(item_id="new", provider_message_id=None, sent_at=NOW - timedelta(days=1))
    notification = _email(
        in_reply_to=None, references=[],
        from_email="notifications@calendly.com", reply_to="cto@acme.com",
    )
    assert matcher.match_to_item(notification, [old, new]).item_id == "new"


# ---------------------------------------------------------------------------
# raw message parsing
# ---------------------------------------------------------------------------

def test_parses_a_raw_message():
    raw = (
        b"Message-ID: <r1@acme.com>\r\n"
        b"In-Reply-To: <orig@csg>\r\n"
        b"From: Dana Scully <cto@acme.com>\r\n"
        b"To: outreach@csg.com\r\n"
        b"Subject: Re: Hiring support\r\n"
        b"Date: Tue, 10 Mar 2026 09:00:00 +0000\r\n"
        b"\r\n"
        b"Sounds interesting.\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.message_id == "<r1@acme.com>"
    assert parsed.from_email == "cto@acme.com"   # address extracted from the display name
    assert parsed.subject == "Re: Hiring support"
    assert "Sounds interesting." in parsed.body
    assert parsed.received_at.tzinfo is None     # stored naive-UTC, per db.utcnow


def test_parses_reply_to_header():
    """The actual signal a Calendly booking notification is matched on --
    see matcher.py strategy 4."""
    raw = (
        b"Message-ID: <notif1@calendly.com>\r\n"
        b"From: Calendly <notifications@calendly.com>\r\n"
        b"To: affan@careersourcegroup.com\r\n"
        b"Reply-To: cto@acme.com\r\n"
        b"Subject: New Event: 30 Minute Meeting\r\n"
        b"Date: Tue, 10 Mar 2026 09:00:00 +0000\r\n"
        b"\r\n"
        b"body\r\n"
    )
    parsed = parse_message(raw)
    assert parsed.reply_to == "cto@acme.com"


def test_reply_to_absent_when_no_header():
    raw = b"Message-ID: <r3@acme.com>\r\nFrom: a@b.com\r\nSubject: hi\r\n\r\nbody\r\n"
    assert parse_message(raw).reply_to is None


def test_message_without_an_id_is_dropped():
    """No Message-ID means no dedupe key, which would re-ingest the mail on
    every tick. Dropping it is the safer failure."""
    assert parse_message(b"From: a@b.com\r\nSubject: hi\r\n\r\nbody\r\n") is None


def test_decodes_an_encoded_subject():
    raw = (
        b"Message-ID: <r2@acme.com>\r\n"
        b"From: cto@acme.com\r\n"
        b"Subject: =?utf-8?B?UmU6IEhpcmluZyBzdXBwb3J0?=\r\n"
        b"\r\nbody\r\n"
    )
    assert parse_message(raw).subject == "Re: Hiring support"
