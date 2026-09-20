"""quoting.strip_quoted -- shared by classifier.py (LLM input) and
routes.py (drawer display), so both must agree on what "just their reply"
means."""

from __future__ import annotations

from outreach.conversation.quoting import strip_quoted


def test_strips_gmail_quote_marker():
    body = (
        "yes its live\r\n\r\n"
        "On Sat, 19 Sept 2026 at 19:31, CSG Outreach <affan@careersourcegroup.com>\r\n"
        "wrote:\r\n\r\n"
        "> MX record is live. Please reply to this email to confirm delivery\r\n"
        "> works end to end.\r\n>"
    )
    assert strip_quoted(body) == "yes its live"


def test_strips_leading_quote_markers_from_remaining_text():
    """A reply typed inside an existing quote can leave '>' on the genuine
    lines too -- these must not show up in the UI."""
    body = "> yes\n> its live"
    assert strip_quoted(body) == "yes\nits live"


def test_strips_outlook_original_message_marker():
    body = "Not for us, thanks.\n\n-----Original Message-----\nFrom: CSG Outreach\nSubject: Hi"
    assert strip_quoted(body) == "Not for us, thanks."


def test_strips_outlook_horizontal_rule():
    body = "Interested.\n\n________________________________\nFrom: CSG Outreach"
    assert strip_quoted(body) == "Interested."


def test_falls_back_to_full_text_when_stripping_would_empty_it():
    body = "On Mon, Mar 9, 2026, someone wrote:\n> the original"
    assert strip_quoted(body) != ""


def test_empty_body_returns_empty_string():
    assert strip_quoted("") == ""
    assert strip_quoted(None) == ""  # type: ignore[arg-type]


def test_plain_reply_with_no_quote_is_unchanged():
    assert strip_quoted("Sounds good, let's talk.") == "Sounds good, let's talk."


def test_classifier_alias_matches_shared_implementation():
    """classifier._strip_quoted is a thin alias -- this pins that it stays
    one, so the two call sites can never quietly diverge again."""
    from outreach.conversation import classifier

    body = "okay i like it.\n\nOn Sat, 19 Sept 2026, CSG Outreach wrote:\n> Hi Affan,\n> This is a test."
    assert classifier._strip_quoted(body) == strip_quoted(body)
