"""Rule-based, observable-only diffing between an AI draft and its edited
version. No LLM calls, no DB access -- pure text analysis, run synchronously
inside a request that is about to return to the user (see the revise/redraft
hooks in api.py and conversation/routes.py), so it must stay fast and free.

Every flag here is an OBSERVABLE measurement (length, sentence count, simple
keyword/substring checks) -- never an inferred psychological judgment. See
rl_prompt.md section 2: "Do NOT try to infer arbitrary psychological
conclusions. Instead store observable differences."
"""

from __future__ import annotations

import re
from typing import Any, Optional

DIFF_VERSION = 1

# A body's final lines are always the fixed signature block (see
# personalize._DEFAULT_SENDER_SIGNATURE / cfg.personalization.sender_signature
# and every prompts/outreach_email*.md template's "SIGNATURE RULE"). The CTA
# is the closing ask BEFORE that block, not the literal last line -- without
# stripping the signature, "cta_changed" would almost always read True simply
# because the signature block itself varies in whether a trailing blank line
# preceded it, which is noise, not an edit.
_SIGNATURE_LINE_HINTS = ("career source group", "careersourcegroup.com")

# Below this many words of absolute change, a length delta is treated as
# noise (e.g. a single word swapped) rather than a deliberate shorten/lengthen.
_LENGTH_NOISE_WORDS = 3

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BULLET_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)


def _sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


_SIGNOFF_PREFIXES = ("best,", "best regards", "thanks,", "thank you,", "regards,")


def _strip_signature(body: str) -> str:
    """Drop the trailing signature block (and the 'Best,'-style sign-off line
    immediately before it, if present) so CTA detection looks at the actual
    closing ask, not the fixed sender block appended to every draft.

    Only strips once a genuine anchor is found (a known sender/company hint,
    or a "Best,"-style sign-off line) -- a short final line on its own (e.g.
    a one-line body with no signature at all) is never mistaken for a
    signature block just because it's short."""
    lines = (body or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    anchor = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        line = lines[i].strip()
        if not line:
            continue
        if any(hint in line.lower() for hint in _SIGNATURE_LINE_HINTS) or line.lower().startswith(_SIGNOFF_PREFIXES):
            anchor = i
            break
    if anchor is None:
        return "\n".join(lines).strip()

    # From the anchor, keep walking upward through short lines that are
    # plausibly still part of the same signature block (name, company,
    # address, URL -- all short, all consecutive, no blank line between).
    cut = anchor
    for i in range(anchor - 1, -1, -1):
        line = lines[i].strip()
        if not line or len(line) > 40:
            break
        cut = i
    return "\n".join(lines[:cut]).strip()


def _closing_ask(body: str) -> str:
    stripped = _strip_signature(body)
    sentences = _sentences(stripped)
    return sentences[-1] if sentences else ""


def _word_count(text: str) -> int:
    return len((text or "").split())


def _personalization_tokens(prospect: Optional[dict[str, Any]]) -> list[str]:
    if not prospect:
        return []
    tokens = []
    for key in ("contact_name", "name", "research_domain"):
        value = prospect.get(key)
        if value and isinstance(value, str) and len(value.strip()) >= 2:
            tokens.append(value.strip().lower())
    return tokens


def _count_tokens(text: str, tokens: list[str]) -> int:
    text_lower = (text or "").lower()
    return sum(text_lower.count(t) for t in tokens)


def _has_bullets_or_breaks(text: str) -> tuple[bool, int]:
    text = text or ""
    has_bullets = bool(_BULLET_RE.search(text))
    line_breaks = text.count("\n")
    return has_bullets, line_breaks


def compute_diff(
    before_subject: Optional[str],
    before_body: Optional[str],
    after_subject: Optional[str],
    after_body: Optional[str],
    prospect: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute the structured diff-flag columns stored on a DraftVersion row.

    Handles None/empty before or after gracefully (e.g. an item that had no
    prior draft) -- every flag defaults to a safe, non-crashing value rather
    than raising."""
    before_subject = before_subject or ""
    before_body = before_body or ""
    after_subject = after_subject or ""
    after_body = after_body or ""

    subject_changed = before_subject.strip() != after_subject.strip()

    before_sentences = _sentences(_strip_signature(before_body))
    after_sentences = _sentences(_strip_signature(after_body))

    opening_changed = (before_sentences[:1] or [None]) != (after_sentences[:1] or [None])

    before_cta = _closing_ask(before_body)
    after_cta = _closing_ask(after_body)
    cta_changed = before_cta.strip() != after_cta.strip()
    cta_shortened = cta_changed and bool(before_cta) and bool(after_cta) and _word_count(after_cta) < _word_count(before_cta)

    before_words = _word_count(_strip_signature(before_body))
    after_words = _word_count(_strip_signature(after_body))
    length_delta_words = after_words - before_words
    length_delta_chars = len(_strip_signature(after_body)) - len(_strip_signature(before_body))
    shortened = length_delta_words <= -_LENGTH_NOISE_WORDS
    lengthened = length_delta_words >= _LENGTH_NOISE_WORDS

    sentences_removed_count = max(0, len(before_sentences) - len(after_sentences))

    tokens = _personalization_tokens(prospect)
    personalization_delta = _count_tokens(after_body, tokens) - _count_tokens(before_body, tokens)

    before_bullets, before_breaks = _has_bullets_or_breaks(before_body)
    after_bullets, after_breaks = _has_bullets_or_breaks(after_body)
    formatting_changed = before_bullets != after_bullets or before_breaks != after_breaks

    return {
        "diff_version": DIFF_VERSION,
        "subject_changed": subject_changed,
        "opening_changed": opening_changed,
        "cta_changed": cta_changed,
        "cta_shortened": cta_shortened,
        "length_delta_chars": length_delta_chars,
        "length_delta_words": length_delta_words,
        "shortened": shortened,
        "lengthened": lengthened,
        "sentences_removed_count": sentences_removed_count,
        "personalization_delta": personalization_delta,
        "formatting_changed": formatting_changed,
    }
