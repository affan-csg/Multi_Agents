"""Stripping the quoted original out of a reply's body.

Used in two places that must never disagree: classifier.py strips before
handing a reply to the LLM (so it judges what the prospect actually wrote,
not a wall of our own pitch quoted beneath it), and routes.py exposes the
same stripped text to the Dashboard drawer, so what a person reads matches
what the model judged. Kept here, standalone, rather than duplicated in one
of those two modules, precisely so they cannot drift apart.

Only ever used for DISPLAY and for classification input -- never for
storage. conversation_messages.body always keeps the full original text;
stripping is applied at read time, not write time, so nothing about the
audit trail is lossy.
"""

from __future__ import annotations

import re

# Ordinary mail clients (Gmail, Apple Mail, Outlook, mobile signatures) all
# mark the start of a quoted block one of these few ways. Whichever marker
# appears earliest in the body wins -- a reply can only be quoting one prior
# message, so the first marker is the real boundary.
#
# The Gmail/Apple Mail marker ("On <date>, <name> wrote:") routinely wraps
# onto a second line for a long name/address -- "On Sat, 19 Sept 2026 at
# 19:31, CSG Outreach <affan@...>\nwrote:" is completely typical, not a rare
# case, so the gap between "On " and "wrote:" is allowed to include up to
# one newline (not `.` with DOTALL/unbounded, which would let "wrote:"
# anywhere later in a long, multi-paragraph reply wrongly count as the
# marker and swallow real reply text as if it were quoted).
_QUOTE_MARKERS = [
    r"\nOn [^\n]{0,160}\n?[^\n]{0,40}wrote:",   # Gmail/Apple Mail -- see note above
    r"\n-{2,}\s*Original Message",              # Outlook
    r"\n_{5,}",                                 # Outlook's horizontal rule
    r"\nFrom:\s",                                # forwarded/replied header block
    r"\nSent from my ",                          # mobile signature, usually above the quote
]

# Gmail/most clients prefix every line of the quoted block with one or more
# '>' once the marker above is stripped down to just the reply text -- but a
# reply typed *inside* an existing quote (rare, but real) can leave leading
# '>' on the genuine reply lines too. Left in, "> yes its live" reads oddly
# in the UI, so quote markers are stripped line-by-line as a second pass.
_QUOTE_PREFIX = re.compile(r"^\s*>+\s?", re.MULTILINE)


def strip_quoted(body: str) -> str:
    """Return just what the prospect wrote, with the quoted original (and
    any leading '>' quote markers) removed.

    Falls back to the full body, quote-prefix-stripped, if cutting at a
    marker would leave nothing -- an oddly top-quoted reply should still
    show something rather than an empty message."""
    if not body:
        return ""

    # Real mail (fetched over IMAP in particular) is \r\n-terminated; the
    # markers below are written against a plain \n convention. Normalizing
    # up front means every marker -- and re.DOTALL, which is what actually
    # lets a marker span a wrapped line -- work the same regardless of which
    # line ending the source used.
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")

    cut = len(normalized)
    for marker in _QUOTE_MARKERS:
        match = re.search(marker, normalized, re.IGNORECASE)
        if match and match.start() < cut:
            cut = match.start()

    trimmed = _QUOTE_PREFIX.sub("", normalized[:cut]).strip()
    if trimmed:
        return trimmed

    # Stripping emptied it -- better to show the quote-prefix-cleaned full
    # text than nothing at all.
    return _QUOTE_PREFIX.sub("", normalized).strip()
