"""Rule-based, observable-only extraction of an item's "strategy" attributes
from its stored subject/body/personalized_from. No LLM calls, no DB access --
pure text analysis, safe to run synchronously and repeatedly (every
recompute), same design principle as ../learning/diffing.py: every attribute
here is a measurement (word count, punctuation, presence of a token), never
an inferred psychological judgment.

This is the one genuinely new signal source campaign-level optimization
needs that the Learning Agent didn't produce: the Learning Agent only ever
diffs a human edit against its prior version, so it has nothing to say about
the very first draft, or about a campaign that was never edited at all.
Deriving strategy from the stored subject/body directly means every sent
item can be classified and compared, not just the ones a human later revised.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .models import StrategyProfile

# Word-count thresholds for length_bucket. Chosen to split typical cold-email
# lengths into three roughly meaningful bands rather than an arbitrary
# median split -- kept as named constants for easy tuning, same convention
# as ../learning/engine.py's confidence thresholds.
_SHORT_MAX_WORDS = 80
_MEDIUM_MAX_WORDS = 160

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SIGNATURE_LINE_HINTS = ("career source group", "careersourcegroup.com")
_SIGNOFF_PREFIXES = ("best,", "best regards", "thanks,", "thank you,", "regards,")

# A CTA counts as "direct" when it asks for a concrete, schedulable action;
# "soft" when it asks an open-ended, no-commitment question instead. Neither
# list claims to be exhaustive -- unmatched CTAs fall back to whichever is
# closer by presence of a question mark (see _cta_style).
_DIRECT_CTA_HINTS = (
    "book a", "book 15", "grab a slot", "schedule a", "here's my calendar",
    "calendly", "pick a time", "15 minutes on my calendar", "reply with a time",
)
_SOFT_CTA_HINTS = (
    "open to", "worth a quick chat", "would it make sense", "interested in learning more",
    "mind if i", "let me know if", "happy to share more",
)


def _word_count(text: str) -> int:
    return len((text or "").split())


def _sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _strip_signature(body: str) -> str:
    """Same signature-stripping heuristic as ../learning/diffing.py's
    _strip_signature, duplicated rather than imported: this module must stay
    independently usable (and independently failure-isolated) from the
    Learning Agent's own package, per this package's read-only-sibling
    contract -- see __init__.py invariant 1."""
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


def _length_bucket(word_count: int) -> str:
    if word_count <= _SHORT_MAX_WORDS:
        return "short"
    if word_count <= _MEDIUM_MAX_WORDS:
        return "medium"
    return "long"


def _subject_style(subject: str, prospect_tokens: list[str]) -> str:
    subject = (subject or "").strip()
    if "?" in subject:
        return "question"
    if prospect_tokens and any(t in subject.lower() for t in prospect_tokens):
        return "personalized"
    return "statement"


def _opening_style(body: str, prospect_tokens: list[str]) -> str:
    sentences = _sentences(_strip_signature(body))
    opening = sentences[0] if sentences else ""
    if "?" in opening:
        return "question"
    if prospect_tokens and any(t in opening.lower() for t in prospect_tokens):
        return "personalized_fact"
    return "generic"


def _cta_style(body: str) -> str:
    cta = _closing_ask(body).strip()
    if not cta:
        return "none"
    lowered = cta.lower()
    if any(hint in lowered for hint in _DIRECT_CTA_HINTS):
        return "direct"
    if any(hint in lowered for hint in _SOFT_CTA_HINTS):
        return "soft"
    # Fall back on the presence of a question mark: an open question with no
    # recognized direct-ask phrase reads as a soft ask by default.
    return "soft" if "?" in cta else "direct"


def _personalization_level(personalized_from: Optional[str], body: str, prospect_tokens: list[str]) -> str:
    if personalized_from in ("pain_summary", "recommended_angle"):
        return "deep"
    if prospect_tokens and any(t in (body or "").lower() for t in prospect_tokens):
        return "light"
    return "none"


def _prospect_tokens(prospect: Optional[dict[str, Any]]) -> list[str]:
    if not prospect:
        return []
    tokens = []
    for key in ("contact_name", "name", "research_domain"):
        value = prospect.get(key)
        if value and isinstance(value, str) and len(value.strip()) >= 2:
            tokens.append(value.strip().lower())
    return tokens


def compute_strategy(
    item_id: str,
    campaign_id: str,
    *,
    subject: Optional[str],
    body: Optional[str],
    personalized_from: Optional[str],
    prospect: Optional[dict[str, Any]] = None,
) -> StrategyProfile:
    """Compute the observable strategy attributes for one sent item.

    Handles None/empty subject or body gracefully (e.g. an item that never
    got a real draft) -- every attribute defaults to a safe, non-crashing
    value rather than raising, same as ../learning/diffing.compute_diff."""
    subject = subject or ""
    body = body or ""
    tokens = _prospect_tokens(prospect)

    word_count = _word_count(_strip_signature(body))
    email_style = "template_only" if personalized_from == "template_only" else "research_backed"

    return StrategyProfile(
        item_id=item_id,
        campaign_id=campaign_id,
        length_bucket=_length_bucket(word_count),
        subject_style=_subject_style(subject, tokens),
        opening_style=_opening_style(body, tokens),
        cta_style=_cta_style(body),
        personalization_level=_personalization_level(personalized_from, body, tokens),
        email_style=email_style,
        word_count=word_count,
    )
