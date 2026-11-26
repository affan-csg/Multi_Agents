"""Real company enrichment using Gemini AI for firmographic research.

Uses Google's Gemini API with grounded search to look up real company
data: employee count, revenue, funding, industry, and description.
All data is returned with strict provenance tracking.

Tavily/Groq enrichment fallback code remains in this module for reference
only and is disabled; Gemini is the sole external AI/research provider.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import threading
import time as _time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .gemini_keys import (
    GeminiKeyPool, is_connection_error, is_daily_quota_exhausted,
    make_ipv4_async_httpx_client,
)
from ..gemini_queue import GeminiQuotaBlocked, GeminiRetryLater, generate_content
from ..models import Company, ProvenanceValue
from ..diagnostics import log_event
from ..usage_tracker import track

_GEMINI_MODEL = "gemini-3.8-flash"

# A connection error (dropped TLS handshake, connect timeout, ...) can be a
# one-off blip even with IPv4 pinning -- worth one quick retry on the same
# key before burning the whole key pool and falling through to Tavily+Groq.
_CONNECTION_ERROR_RETRIES = 1
_CONNECTION_ERROR_RETRY_DELAY = 2.0
_GEMINI_RATE_LIMIT_RETRIES = 1
_GEMINI_RATE_LIMIT_RETRY_DELAY = 2.0
_ENRICH_RETRY_DELAY_CAP_SECONDS = 30.0

_FIELD_LEVEL_ENRICHMENT_REASONS = frozenset({
    "complete_with_gaps", "incomplete_result", "ungrounded_response",
    "partial", "needs_review",
})
_OPERATIONAL_ENRICHMENT_REASONS = frozenset({
    "no_api_key", "quota_exhausted", "rate_limited", "connection_error",
    "timeout", "parse_error", "request_error", "interrupted",
})
_TERMINAL_ENRICHMENT_STATUSES = frozenset({
    "complete", "complete_with_gaps", "partial", "needs_review",
})
_VALIDATION_REASON_MARKERS = (
    "supporting url", "conflicting claims", "grounding",
    "could not be verified", "not present in provider", "excerpt",
)
# Legacy runs stored field-level outcomes as failure reasons and kept no
# per-company summary at all. Those reasons are research status, not provider
# outages, so they are folded into the neutral count the Dashboard shows
# instead of being repeated as raw codes in a run summary.
_PRESENTED_FAILURE_COUNT_FIELD = {
    "incomplete_result": "enrich_partial",
    "partial": "enrich_partial",
    "needs_review": "enrich_needs_review",
    "ungrounded_response": "enrich_needs_review",
    "complete_with_gaps": "enrich_complete_with_gaps",
    "empty_response": "enrich_research_incomplete",
}

_FIELD_CRITERIA = {
    "official_website": "automatic_website",
    "headquarters": "automatic_headquarters",
    "employee_range": "automatic_company_size",
    "company_classification": "automatic_company_type",
    # What the company actually does (Cybersecurity, SaaS, AI/ML, ...). Kept
    # separate from company_classification, which is ownership/maturity.
    "business_category": "automatic_business_category",
    "leadership": "company_leadership",
    "services": "company_services",
    "hiring_service_fit": "company_hiring_fit",
    "revenue": "company_revenue",
    "funding": "company_funding",
    "industry": "company_industry",
    "description": "company_description",
}
# Outcome field -> the Company attribute holding its provenance envelope. Used
# to report the source tier of the value that was actually stored, not only of
# evidence items.
_PROVENANCE_OUTCOME_FIELDS = {
    "official_website": "website",
    "headquarters": "headquarters",
    "company_classification": "company_type",
    "business_category": "business_category",
    "employee_range": "employee_count",
    "revenue": "revenue",
    "funding": "funding",
}
# Every researched field can carry its own evidence item: the claim the model
# returned, the source it cited, the excerpt and the tier. One set keeps the
# synthesis and the validation loop in step -- a field cannot be stored
# without a chance to persist, and be checked against, its evidence.
_EVIDENCE_FIELDS = frozenset(_FIELD_CRITERIA)
# Fields whose competing claims are compared for contradiction. Multi-valued
# fields (leadership, services) legitimately carry several claims, and the
# value-shape fields (headquarters envelope, website URL) are not comparable
# claim-for-claim, so they are excluded exactly as before.
_EVIDENCE_CONFLICT_FIELDS = frozenset({
    "employee_range", "services", "hiring_service_fit", "industry",
    "description", "leadership",
})
# Fields whose value is stored only through validated evidence. Every other
# field is accepted by an authoritative value check (save_fact, industry,
# description), and evidence for it is provenance -- never a second route to
# acceptance.
_EVIDENCE_ONLY_FIELDS = frozenset({"services", "hiring_service_fit", "leadership"})
# Value fields validated after the evidence loop; their evidence is collected
# before the authoritative decision and kept in step by that block.
_DEFERRED_VALUE_EVIDENCE_FIELDS = frozenset({"industry", "description"})
# Fields whose excerpt may be returned under a sibling response key, matching
# the value/source fallbacks in field_sources below.
_EVIDENCE_EXCERPT_ALIASES = {
    "employee_range": ("employee_count",),
    "company_classification": ("company_type",),
}
_REQUIRED_RESPONSE_FIELDS = (
    "official_website", "headquarters", "company_type", "business_category",
    "employee_count", "revenue", "funding", "industry", "description",
    "employee_range", "services", "leadership", "hiring_service_fit",
    "classification_evidence", "research_evidence",
)
_REQUIRED_RESPONSE_KEYS = frozenset({
    *_REQUIRED_RESPONSE_FIELDS,
    "official_website_source_url", "official_website_excerpt",
    "headquarters_source_url", "headquarters_excerpt",
    "company_type_source_url", "company_type_excerpt",
    "business_category_source_url", "business_category_excerpt",
    "employee_count_source_url", "employee_count_excerpt",
    "revenue_source_url", "revenue_excerpt", "funding_source_url", "funding_excerpt",
    "industry_source_url", "industry_excerpt", "description_source_url", "description_excerpt",
    "employee_range_source_url", "employee_range_excerpt", "services_source_url", "services_excerpt",
    "hiring_service_fit_source_url", "hiring_service_fit_excerpt",
})
_FALLBACK_QUERIES = (
    ("official_website", "{name} {domain} official company website about"),
    ("headquarters", "{name} {domain} headquarters city country official"),
    ("employee_range", "{name} {domain} employee count company size range"),
    ("company_classification", "{name} {domain} company type public private startup systems integrator"),
    ("business_category", "{name} {domain} what the company does products industry category"),
    ("leadership", "{name} {domain} leadership CEO CTO talent acquisition executives"),
    ("services", "{name} {domain} services products what the company offers"),
    ("hiring_service_fit", "{name} {domain} careers open engineering data jobs contractors staffing services"),
    ("revenue", "{name} {domain} reported revenue annual revenue financial results"),
    ("funding", "{name} {domain} funding investment financing"),
    ("industry", "{name} {domain} industry products market"),
    ("description", "{name} {domain} company overview what the company does"),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_source_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    # "&" and "and" are the same assertion, and sources mix them freely:
    # a claim of "Co-Founder & CEO" must not fail against "co-founder and CEO".
    normalized = re.sub(r"\s*&\s*", " and ", normalized)
    return " ".join(normalized.split()).casefold()


def _has_source_excerpt(source_url: str, excerpt: Any, source_content: dict[str, str]) -> bool:
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    content = source_content.get(source_url, "")
    return bool(content) and _normalized_source_text(excerpt) in _normalized_source_text(content)


# --- Text encoding guard ------------------------------------------------------
# Provider/search output occasionally arrives with a byte sequence decoded as
# the wrong charset ("S\ufffdo Paulo", "S\u00c3\u00a3o Paulo"). Those are not
# facts about the company and must never be stored as verified data. Detection
# is read-only: a malformed value is rejected, never silently repaired, because
# a guessed repair can change the fact itself.
_REPLACEMENT_CHAR = "\ufffd"
_DISALLOWED_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text_is_malformed(value: str) -> bool:
    """True for replacement characters, control bytes, or Latin-1/UTF-8 mojibake."""
    if not isinstance(value, str) or not value:
        return False
    if _REPLACEMENT_CHAR in value or _DISALLOWED_TEXT_CONTROLS.search(value):
        return True
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False
    # A clean Latin-1/Unicode string is unchanged by the round trip. Only a
    # string whose bytes happen to form valid UTF-8 (the mojibake signature)
    # changes, so this does not reject accented text such as "S\u00e3o Paulo".
    return repaired != value


def _has_malformed_text(value: Any) -> bool:
    """True when any string inside a claim/envelope is malformed."""
    if isinstance(value, str):
        return _text_is_malformed(value)
    if isinstance(value, dict):
        return any(
            _has_malformed_text(key) or _has_malformed_text(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_malformed_text(item) for item in value)
    return False


# A grounding segment must be a real sentence, not a bare token, before it can
# support a claim.
_MIN_SUPPORT_TEXT_CHARS = 12
_NEGATION_CUE = re.compile(
    r"\b(?:not|no|never|neither|nor|without|isn't|aren't|wasn't|weren't|doesn't|don't)\b"
)
_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d),(?=\d)")
_UNIT_WORDS = {"k": "thousand", "m": "million", "b": "billion"}
_CLAIM_ALIASES = {
    "public company": ("public company", "publicly traded company"),
    "private company": ("private company", "privately held company"),
    "smb": ("smb", "small business", "small-to-medium business"),
    "system integrator": (
        "system integrator", "systems integrator", "systems integration provider",
    ),
    "ai/ml": ("ai/ml", "artificial intelligence", "machine learning"),
    # Leadership titles: sources spell out roles the response abbreviates.
    "ceo": ("ceo", "chief executive officer"),
    "cto": ("cto", "chief technology officer"),
    "cfo": ("cfo", "chief financial officer"),
    "coo": ("coo", "chief operating officer"),
    "cmo": ("cmo", "chief marketing officer"),
    "cio": ("cio", "chief information officer"),
    "cro": ("cro", "chief revenue officer"),
    "cpo": ("cpo", "chief product officer"),
    "chro": ("chro", "chief people officer", "chief human resources officer"),
    "vp": ("vp", "vice president"),
    "svp": ("svp", "senior vice president"),
    "evp": ("evp", "executive vice president"),
    "co-founder": ("co-founder", "cofounder", "co founder"),
    "co founder": ("co founder", "co-founder", "cofounder"),
}


def _support_text(value: str) -> str:
    """Normalize text for claim comparison without changing its wording."""
    return _THOUSANDS_SEPARATOR.sub("", _normalized_source_text(value))


def _value_variants(value: str) -> list[str]:
    """Wordings that express the same claim (number, currency and unit forms)."""
    base = _support_text(value)
    variants = {base}
    match = re.fullmatch(r"\$?\s*(\d+(?:\.\d+)?)\s*([kmb])\b", base)
    if match:
        number, unit = match.groups()
        word = _UNIT_WORDS[unit]
        variants.update({
            f"{number} {word}", f"${number} {word}", f"{number}{unit}",
            f"${number}{unit}",
        })
    return [variant for variant in variants if variant]


def _text_supports_value(value: str, text: str) -> bool:
    """True only when `text` states this exact value as a whole, non-negated phrase.

    Substring containment is deliberately not enough: a value that happens to
    appear inside a different word, or in a negated sentence ("is not a
    cybersecurity company"), does not support the claim.
    """
    haystack = _support_text(text)
    if len(haystack) < _MIN_SUPPORT_TEXT_CHARS:
        return False
    for variant in _value_variants(value):
        pattern = rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])"
        for match in re.finditer(pattern, haystack):
            prefix = haystack[max(0, match.start() - 48):match.start()]
            if not _NEGATION_CUE.search(prefix):
                return True
    return False


def _url_host(url: str) -> Optional[str]:
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:
        return None
    return host.removeprefix("www.") or None


# --- Source quality ----------------------------------------------------------
# Mirrors the source priority the prompt gives the model. Tier 1 is the company
# itself, tier 2 is first-party disclosure (press wires, investor relations,
# regulators), tier 3 is established business press and company databases, and
# tier 4 is everything else (blogs, forums, social posts, aggregator scrapes).
# Tiers never upgrade a model's stated confidence; they only cap it.
SOURCE_TIER_OFFICIAL = 1
SOURCE_TIER_FIRST_PARTY = 2
SOURCE_TIER_ESTABLISHED = 3
SOURCE_TIER_UNCLASSIFIED = 4

_FIRST_PARTY_SOURCE_HOSTS = (
    "prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com",
    "newsfilecorp.com", "einpresswire.com", "sec.gov", "sedar.com",
)
_ESTABLISHED_SOURCE_HOSTS = (
    "linkedin.com", "crunchbase.com", "pitchbook.com", "cbinsights.com",
    "bloomberg.com", "reuters.com", "ft.com", "wsj.com", "cnbc.com",
    "forbes.com", "businessinsider.com", "techcrunch.com", "axios.com",
    "g2.com", "capterra.com", "gartner.com", "wikipedia.org", "owler.com",
    "zoominfo.com", "glassdoor.com", "builtin.com",
)
_CONFIDENCE_ORDER = ("low", "medium", "high")
_SOURCE_TIER_CONFIDENCE_CAP = {
    SOURCE_TIER_OFFICIAL: "high",
    SOURCE_TIER_FIRST_PARTY: "high",
    SOURCE_TIER_ESTABLISHED: "medium",
    SOURCE_TIER_UNCLASSIFIED: "low",
}


def _host_within_domain(host: Optional[str], domain: Optional[str]) -> bool:
    """True when `host` is the domain itself or a subdomain of it."""
    normalized_host = (host or "").strip().lower().removeprefix("www.")
    normalized_domain = (domain or "").strip().lower().removeprefix("www.")
    return bool(
        normalized_host and normalized_domain
        and (
            normalized_host == normalized_domain
            or normalized_host.endswith(f".{normalized_domain}")
        )
    )


def _source_quality_tier(
    url: Any,
    company_domain: Optional[str] = None,
    official_hosts: Optional[set[str]] = None,
) -> int:
    """Deterministic source tier for a cited URL (see the constants above).

    `official_hosts` carries domains proven to belong to the company by a
    verified relationship (see `_verified_official_alias_hosts`), so a
    legitimate alias is tier 1 without a hardcoded company/domain list.
    """
    host = _url_host(url) if isinstance(url, str) else None
    if host is None:
        return SOURCE_TIER_UNCLASSIFIED
    if _host_within_domain(host, company_domain):
        return SOURCE_TIER_OFFICIAL
    if any(_host_within_domain(host, alias) for alias in (official_hosts or set())):
        return SOURCE_TIER_OFFICIAL
    if any(host == name or host.endswith(f".{name}") for name in _FIRST_PARTY_SOURCE_HOSTS):
        return SOURCE_TIER_FIRST_PARTY
    if any(host == name or host.endswith(f".{name}") for name in _ESTABLISHED_SOURCE_HOSTS):
        return SOURCE_TIER_ESTABLISHED
    return SOURCE_TIER_UNCLASSIFIED


def _evidence_key(
    field: str,
    criterion: str,
    claim: Any,
    source_url: Any,
    excerpt: Any,
) -> str:
    """Stable identity for one evidence claim.

    One definition keeps synthesis and validation agreeing on what counts as
    the same evidence, so a value's own provenance item is added when the model
    cited a different source for the same field, and never duplicated.
    """
    key_material = json.dumps(
        [field, criterion, claim, source_url, excerpt], sort_keys=True,
    )
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


def _evidence_identity(item: dict[str, Any]) -> tuple:
    """What makes two evidence items the same assertion.

    Two grounding chunks can resolve to the same article, so the stored page --
    not the cited redirect -- identifies the source here.
    """
    return (
        item.get("field"),
        json.dumps(item.get("claim"), sort_keys=True),
        item.get("source_url"),
        item.get("excerpt"),
    )


def _claim_is_call_to_action(text: str) -> bool:
    """A promo line is not a description of the company.

    Live responses offered "Check out posthog.com/careers if you want to be part
    of this journey" as hiring-fit evidence; such copy invites the reader to do
    something instead of asserting a fact.
    """
    normalized = " ".join(str(text or "").lower().split())
    if not normalized or len(normalized.split()) > 18:
        return False
    starters = (
        "check out", "visit ", "learn more", "read more", "find out", "sign up",
        "apply now", "apply today", "join us", "join our", "contact us",
        "get started", "get in touch", "book a demo", "request a demo",
        "subscribe", "follow us", "see how", "discover how", "explore our",
    )
    return normalized.startswith(starters)


def _tier_capped_confidence(confidence: Any, tier: int) -> str:
    """Keep the model's confidence when it is already at or below the tier cap."""
    stated = str(confidence or "medium").strip().lower()
    if stated not in _CONFIDENCE_ORDER:
        stated = "medium"
    cap = _SOURCE_TIER_CONFIDENCE_CAP.get(tier, "low")
    if _CONFIDENCE_ORDER.index(stated) <= _CONFIDENCE_ORDER.index(cap):
        return stated
    return cap


def _grounding_candidate_chunks(
    source_url: Any,
    allowed_source_urls: set[str],
    resolved_source_urls: dict[str, str],
) -> list[str]:
    """Grounding chunks that may back a cited URL.

    Exact chunk URL first, then the real URL it resolves to. If the model cites
    a page on the same site as a resolved chunk (a deep link rather than the
    chunk's landing page), those chunks are candidates too -- but the claim
    still has to appear in one of their support segments, so the fact remains
    tied to the chunk Google actually used.
    """
    canonical = _normalize_grounding_url(source_url)
    if canonical is None:
        return []
    if canonical in allowed_source_urls:
        return [canonical]
    exact = resolved_source_urls.get(canonical)
    if exact:
        return [exact]
    host = _url_host(canonical)
    if host is None:
        return []
    return [
        chunk for final, chunk in resolved_source_urls.items()
        if _url_host(final) == host
    ]


def _claim_matches_excerpt(claim: dict[str, Any], excerpt: str) -> bool:
    """Every value in `claim` must be stated by the same supporting text."""
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            values.append(str(value).strip())

    collect(claim)

    def supported(value: str) -> bool:
        alternatives = _CLAIM_ALIASES.get(_normalized_source_text(value), (value,))
        return any(_text_supports_value(alternative, excerpt) for alternative in alternatives)

    return bool(values) and all(supported(value) for value in values)


def _is_manually_approved(value: ProvenanceValue | None) -> bool:
    if value is None:
        return False
    source = re.sub(r"[^a-z0-9]+", "_", str(value.source).lower()).strip("_")
    return any(marker in source for marker in (
        "manual", "operator", "human_verified", "human_approved", "operator_approved",
    ))


def _mark_research_not_searched(company: Company, reason: str) -> None:
    now = _utc_now_iso()
    for field, criterion in _FIELD_CRITERIA.items():
        company.research_outcomes.setdefault(field, {
            "status": "not_searched", "criterion": criterion,
            "checked_at": now, "reason": reason,
        })


def enrichment_completeness(company: Company) -> dict[str, Any]:
    """Summarize terminal field outcomes without treating unavailable facts as successes.

    Verified research, values carried forward from an earlier run, discovery
    hints and genuinely unavailable facts are counted separately so a
    discovery label is never presented as a verified finding.
    """
    missing: dict[str, str] = {}
    blocking: dict[str, str] = {}
    review: dict[str, str] = {}
    verified: dict[str, str] = {}
    carried: dict[str, str] = {}
    hints: dict[str, str] = {}
    response_missing_data = company.research_outcomes.get("_missing_response_fields") or {}
    if isinstance(response_missing_data, dict):
        response_missing = response_missing_data.get("missing") or []
    elif isinstance(response_missing_data, list):
        response_missing = response_missing_data
    else:
        response_missing = []
    if response_missing:
        reason = "Gemini omitted required response keys: " + ", ".join(response_missing)
        blocking["response_schema"] = reason
        missing["response_schema"] = reason
    for field in _FIELD_CRITERIA:
        outcome = company.research_outcomes.get(field)
        if not isinstance(outcome, dict):
            blocking[field] = "No research outcome was recorded"
            continue
        status = outcome.get("status")
        reason = outcome.get("reason")
        if status == "accepted_and_mapped":
            verified[field] = outcome.get("criterion") or _FIELD_CRITERIA[field]
            continue
        if status == "carried_forward":
            carried[field] = reason or "Previously stored value retained; not re-verified in this run"
            continue
        if status == "discovery_hint_unverified":
            hint = outcome.get("discovery_hint")
            hints[field] = (
                f"Discovery hint retained but not verified: {hint}" if hint
                else "Discovery hint retained but not verified"
            )
            missing[field] = hints[field]
            continue
        if status == "searched_but_not_found":
            missing[field] = reason or "No reliable information was returned"
            continue
        if status == "found_but_rejected":
            reason = reason or "The returned claim could not be verified"
            review[field] = reason
            missing[field] = reason
            blocking[field] = reason
            continue
        blocking[field] = reason or {
            "not_returned": "The provider omitted this required field",
            "found_but_rejected": "The returned claim failed validation",
            "not_searched": "The field was not searched",
        }.get(status, f"Research outcome is incomplete ({status or 'missing status'})")
        missing[field] = blocking[field]
    accepted = bool(verified) or bool(carried)
    if review:
        status = "needs_review"
    elif accepted and blocking:
        status = "partial"
    elif accepted and missing:
        status = "complete_with_gaps"
    elif accepted:
        status = "complete"
    elif blocking or missing:
        status = "research_incomplete"
    else:
        status = "complete"
    return {
        "status": status,
        "missing_fields": missing,
        "blocking_fields": blocking,
        "review_fields": review,
        # Completeness breakdown: newly verified vs carried forward vs
        # unverified discovery hints. ``missing_fields`` still lists every
        # field without a verified value, so a hint is a gap, never a success.
        "verified_fields": verified,
        "carried_fields": carried,
        "hint_fields": hints,
        "counts": {
            "verified": len(verified),
            "carried_forward": len(carried),
            "discovery_hints": len(hints),
            "searched_but_not_found": len(missing) - len(hints) - len(review),
            "found_but_rejected": len(review),
            "blocking": len(blocking),
        },
    }


def is_field_level_enrichment_reason(reason: Any) -> bool:
    """True when a saved reason describes field evidence, not a provider outage."""
    return reason in _FIELD_LEVEL_ENRICHMENT_REASONS


def stored_enrichment_is_terminal(summary: Any) -> bool:
    """Persisted research that should not be run again.

    Older runs stored useful partial research as ``failed`` with a field-level
    reason such as ``incomplete_result``. That is finished research. Quota,
    timeout, parse, and transport failures stay non-terminal.
    """
    if not isinstance(summary, dict):
        return False
    status = summary.get("status")
    if status in _TERMINAL_ENRICHMENT_STATUSES:
        return True
    return status == "failed" and is_field_level_enrichment_reason(summary.get("failure_reason"))


def present_stored_enrichment_summary(outcomes: Any) -> dict[str, Any]:
    """Copy stored outcomes so a field-level result is not labeled as a failure.

    The input is not mutated. Operational failures keep ``status=failed`` and
    their ``failure_reason``.
    """
    if not isinstance(outcomes, dict):
        return {}
    summary = outcomes.get("_summary")
    if not isinstance(summary, dict):
        return outcomes
    status = summary.get("status")
    reason = summary.get("failure_reason")
    if status == "research_incomplete":
        return outcomes
    if status in _TERMINAL_ENRICHMENT_STATUSES:
        if not is_field_level_enrichment_reason(reason) or "failure_reason" not in summary:
            return outcomes
        cleaned = {key: value for key, value in summary.items() if key != "failure_reason"}
        return {**outcomes, "_summary": cleaned}
    if status == "failed" and reason == "empty_response":
        field_rows = [name for name in _FIELD_CRITERIA if isinstance(outcomes.get(name), dict)]
        if field_rows:
            company = Company(
                domain="stored.example",
                research_outcomes={key: value for key, value in outcomes.items() if key != "_summary"},
            )
            return {**outcomes, "_summary": enrichment_completeness(company)}
        cleaned = {key: value for key, value in summary.items() if key != "failure_reason"}
        cleaned["status"] = "research_incomplete"
        return {**outcomes, "_summary": cleaned}
    if status != "failed" or not is_field_level_enrichment_reason(reason):
        return outcomes
    field_rows = [name for name in _FIELD_CRITERIA if isinstance(outcomes.get(name), dict)]
    if not field_rows:
        mapped = {
            "incomplete_result": "partial",
            "ungrounded_response": "research_incomplete",
            "complete_with_gaps": "complete_with_gaps",
            "partial": "partial",
            "needs_review": "needs_review",
        }[reason]
        cleaned = {key: value for key, value in summary.items() if key != "failure_reason"}
        cleaned["status"] = mapped
        return {**outcomes, "_summary": cleaned}
    company = Company(
        domain="stored.example",
        research_outcomes={key: value for key, value in outcomes.items() if key != "_summary"},
    )
    return {**outcomes, "_summary": enrichment_completeness(company)}


def _summary_reason_text(summary: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("missing_fields", "review_fields", "blocking_fields"):
        value = summary.get(key) or {}
        if isinstance(value, dict):
            chunks.extend(str(item) for item in value.values())
    return " ".join(chunks).lower()


def present_enrichment_summary_entry(summary: Any) -> dict[str, Any]:
    """Copy one run-summary entry into a user-facing research status.

    Field-validation text stays out of this copy. Operational failures keep
    ``status=failed`` and their reason code. The input is not mutated.
    """
    if not isinstance(summary, dict):
        return {}
    item = dict(summary)
    status = item.get("status")
    reason = item.get("failure_reason")
    if status == "failed" and reason in _OPERATIONAL_ENRICHMENT_REASONS:
        item["missing_fields"] = {}
        item["blocking_fields"] = {}
        item["review_fields"] = {}
        return item
    if status in _TERMINAL_ENRICHMENT_STATUSES or status == "research_incomplete":
        item.pop("failure_reason", None)
        item["missing_fields"] = {}
        item["blocking_fields"] = {}
        item["review_fields"] = {}
        return item
    text = _summary_reason_text(item)
    validation = any(marker in text for marker in _VALIDATION_REASON_MARKERS)
    no_usable = "has not returned usable" in text or reason in {
        "empty_response", "ungrounded_response", "research_incomplete",
    }
    if validation:
        presented = "needs_review"
    elif reason == "incomplete_result":
        presented = "partial"
    elif no_usable or status == "failed":
        presented = "research_incomplete"
    else:
        return item
    item["status"] = presented
    item.pop("failure_reason", None)
    item["missing_fields"] = {}
    item["blocking_fields"] = {}
    item["review_fields"] = {}
    return item


def present_discover_result(result: Any) -> dict[str, Any]:
    """Copy a stored discover result so validation outcomes are not failures.

    Historical job rows are left unchanged. Counts are derived again from the
    presented per-company summaries.
    """
    if not isinstance(result, dict):
        return {}
    presented = dict(result)
    summary = result.get("enrichment_summary")
    if isinstance(summary, dict):
        entries = {
            str(domain): present_enrichment_summary_entry(item)
            for domain, item in summary.items()
        }
        presented["enrichment_summary"] = entries
        values = list(entries.values())
        presented["enrich_partial"] = sum(item.get("status") == "partial" for item in values)
        presented["enrich_needs_review"] = sum(item.get("status") == "needs_review" for item in values)
        presented["enrich_complete_with_gaps"] = sum(
            item.get("status") == "complete_with_gaps" for item in values
        )
        presented["enrich_research_incomplete"] = sum(
            item.get("status") == "research_incomplete" for item in values
        )
    failures = result.get("enrich_failures")
    if isinstance(failures, dict):
        presented["enrich_failures"] = {
            str(reason): count
            for reason, count in failures.items()
            if count and reason in _OPERATIONAL_ENRICHMENT_REASONS
        }
        if not summary:
            # No per-company detail survives (older job rows): keep the
            # aggregate counts, but only under neutral statuses. Raw
            # validation reasons never reach the Dashboard summary.
            neutral: dict[str, int] = {}
            for reason, count in failures.items():
                if not count or reason in _OPERATIONAL_ENRICHMENT_REASONS:
                    continue
                field = _PRESENTED_FAILURE_COUNT_FIELD.get(
                    str(reason), "enrich_research_incomplete",
                )
                neutral[field] = neutral.get(field, 0) + int(count)
            for field, count in neutral.items():
                presented[field] = max(int(presented.get(field) or 0), count)
    return presented


def _is_gemini_transient_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    status = status or getattr(response, "status_code", None)
    return status == 429 and not is_daily_quota_exhausted(exc)


def _parse_number(text: str) -> Optional[int]:
    """Parse a number from text like '1,200', '~500', '200+', '1.2k', '1200'."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace("+", "").replace("~", "").replace("approximately", "")
    # Handle 'k' suffix (e.g., '1.2k' -> 1200)
    if text.lower().endswith("k"):
        try:
            return int(float(text[:-1]) * 1000)
        except ValueError:
            pass
    try:
        return int(float(text))
    except ValueError:
        return None


def _employee_range_bounds(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        bounds = [_employee_range_bounds(item) for item in value.values()]
        numeric_bounds = [item for item in bounds if item is not None]
        if numeric_bounds:
            return min(item[0] for item in numeric_bounds), max(item[1] for item in numeric_bounds)
        return None
    if isinstance(value, (list, tuple)):
        bounds = [_employee_range_bounds(item) for item in value]
        numeric_bounds = [item for item in bounds if item is not None]
        if numeric_bounds:
            return min(item[0] for item in numeric_bounds), max(item[1] for item in numeric_bounds)
        return None
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return None
    text = str(value).lower().replace(",", "")
    matches = re.findall(r"\d+(?:\.\d+)?\s*k?", text)
    numbers = [float(_parse_number(item.strip())) for item in matches if _parse_number(item.strip()) is not None]
    if not numbers:
        return None
    has_range_marker = re.search(r"\d[^\s]*(?:-|\bto\b)\s*\d", text) is not None
    if len(numbers) > 1 and has_range_marker:
        return min(numbers), max(numbers)
    number = numbers[0]
    if "+" in text:
        return number, float("inf")
    return number, number


def _parse_revenue(text: str) -> Optional[str]:
    """Normalize revenue text like '$100M', '$1.2 billion', etc."""
    if not text or text.lower() in ("unknown", "n/a", "not available", "private", "not publicly available"):
        return None
    return text.strip()


# --- Financial semantics ------------------------------------------------------
# A figure only means what its label says. These kinds are the concepts the
# prompt tells the model to keep apart, so a valuation is never stored as
# funding and a funding round never as revenue.
_FINANCIAL_METRIC_PATTERNS = (
    ("valuation", re.compile(r"\b(?:valuation|valued at|post[- ]money|pre[- ]money|enterprise value)\b")),
    ("market_cap", re.compile(r"\bmarket cap(?:italization)?\b")),
    ("funding", re.compile(r"\b(?:raised|raising|funding round|series [a-e]\b|seed round|pre-seed|financing round|investment round|closed a)\b")),
    ("arr", re.compile(r"\b(?:arr|annual recurring revenue)\b")),
    ("revenue", re.compile(r"\b(?:revenue|revenues|net sales|sales|turnover|fiscal year|full[- ]year)\b")),
)
# Metric kinds that make a claim wrong for that field. Explicitly labelled ARR
# is handled separately: it may be stored as revenue only when the value itself
# carries the ARR label, so nothing is relabelled behind the reader's back.
_FINANCIAL_FIELD_CONFLICTS = {
    "revenue": frozenset({"valuation", "market_cap", "funding"}),
    "funding": frozenset({"valuation", "market_cap", "arr", "revenue"}),
}
_FINANCIAL_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _financial_metric_named_in(text: str) -> Optional[str]:
    """The metric kind a value names for itself ('$100M Series C', '$200M ARR')."""
    lowered = _support_text(text)
    for kind, pattern in _FINANCIAL_METRIC_PATTERNS:
        if pattern.search(lowered):
            return kind
    return None


def _financial_metric_in_context(value: str, context: str) -> Optional[str]:
    """The metric word a supporting sentence ties to this value's number.

    Distances decide: "raised $50M at a $4.85B valuation" labels $50M as
    funding and $4.85B as valuation, in the same sentence. Only the marker
    closest to the value's own number counts.
    """
    if not context:
        return None
    haystack = _support_text(context)
    markers: list[tuple[int, int, str]] = []
    for kind, pattern in _FINANCIAL_METRIC_PATTERNS:
        markers.extend((match.start(), match.end(), kind) for match in pattern.finditer(haystack))
    if not markers:
        return None
    best: Optional[tuple[float, str]] = None
    for number in _FINANCIAL_NUMBER.findall(_support_text(value)):
        pattern = rf"(?<![\d.]){re.escape(number)}(?![\d])"
        for match in re.finditer(pattern, haystack):
            for start, end, kind in markers:
                distance = 0.0 if start <= match.start() < end else min(
                    abs(match.start() - end), abs(start - match.end()),
                )
                if best is None or distance < best[0]:
                    best = (distance, kind)
    return best[1] if best else None


def _financial_value_is_valid(
    field: str, value: Any, context: str = "",
) -> tuple[bool, str]:
    """Reject a financial claim that states a different financial concept."""
    conflicting = _FINANCIAL_FIELD_CONFLICTS.get(field)
    if not conflicting or value in (None, ""):
        return True, ""
    text = str(value)
    named = _financial_metric_named_in(text)
    kind = named or _financial_metric_in_context(text, context)
    if kind is None:
        return True, ""
    if kind == "arr" and field == "revenue" and named != "arr":
        # The sentence says ARR but the stored value would read as total
        # revenue. Rejecting keeps ARR from being presented as revenue, and an
        # explicitly labelled ARR value is still accepted (named == "arr").
        return False, "financial metric mismatch: the source states annual recurring revenue, not total revenue"
    if kind in conflicting:
        label = kind.replace("_", " ")
        return False, f"financial metric mismatch: the source states {label}, not {field}"
    return True, ""


# --- Leadership ---------------------------------------------------------------
# The prompt asks for one schema (name + title, with source and excerpt), but
# models drift between objects, alternative key names, and "Name - Title"
# strings. Everything downstream therefore reads one normalized shape, so a
# half-populated entry can never become an evidence claim of its own.
_LEADERSHIP_TITLE_HINT = re.compile(
    r"\b(?:ceo|cto|coo|cfo|cmo|cio|ciso|chro|cpo|vp|svp|evp|vice president|"
    r"president|founder|co-founder|owner|chair(?:man|woman|person)?|partner|"
    r"chief|head of|director|manager|lead|principal|general counsel)\b",
    re.IGNORECASE,
)
_PERSON_NAME = re.compile(r"^[^\W\d_][\w.'’-]*(?:\s+[^\W\d_][\w.'’-]*){1,3}$")
_LEADERSHIP_KEY_ALIASES = {
    "name": ("name", "full_name", "person", "executive"),
    "title": ("title", "role", "position", "job_title"),
}


def _leadership_string_parts(text: str) -> Optional[tuple[str, str]]:
    """Split 'Jane Doe - CEO' style strings, but only when a title is explicit."""
    parts = [
        part.strip() for part in re.split(r"\s+[–—]\s+|\s*[-|]\s*|\s*,\s*|\s*\(\s*", text.strip(), maxsplit=1)
        if part.strip()
    ]
    if len(parts) != 2:
        return None
    left, right = (part.rstrip(")").strip() for part in parts)
    left_title = bool(_LEADERSHIP_TITLE_HINT.search(left))
    right_title = bool(_LEADERSHIP_TITLE_HINT.search(right))
    if left_title == right_title:
        # Both or neither side names a role: the mapping is ambiguous, so it is
        # not used instead of guessing which token is the person.
        return None
    name, title = (right, left) if left_title else (left, right)
    if not _PERSON_NAME.match(name) or not title:
        return None
    return name, title


def _normalize_leadership_person(person: Any) -> Optional[dict[str, str]]:
    """One leadership item in the canonical shape, or None if it is incomplete."""
    if isinstance(person, str):
        parts = _leadership_string_parts(person)
        if parts is None:
            return None
        return {"name": parts[0], "title": parts[1]}
    if not isinstance(person, dict):
        return None
    name = ""
    title = ""
    for field, aliases in _LEADERSHIP_KEY_ALIASES.items():
        for alias in aliases:
            candidate = person.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                if field == "name":
                    name = name or candidate.strip()
                else:
                    title = title or candidate.strip()
                break
    if not name and not title:
        return None
    if not name or not title:
        # A name without a title (or the reverse) cannot be attributed to a
        # person and is dropped rather than stored as a partial claim.
        return None
    if _text_is_malformed(name) or _text_is_malformed(title):
        # Corrupt names/titles are not a partial claim; they are unusable text.
        return None
    normalized = {"name": name, "title": title}
    for extra in ("source_url", "source_name", "excerpt"):
        value = person.get(extra)
        if isinstance(value, str) and value.strip():
            normalized[extra] = value.strip()
    return normalized


def _normalize_leadership(raw: Any) -> list[dict[str, str]]:
    """Canonical leadership list: name + title only, deduplicated, order kept."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    people: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for person in raw:
        normalized = _normalize_leadership_person(person)
        if normalized is None:
            continue
        key = (normalized["name"].casefold(), normalized["title"].casefold())
        if key in seen:
            continue
        seen.add(key)
        people.append(normalized)
    return people


_PROMPT_TEMPLATE = """You are a business research analyst. Research the company with domain "{domain}" (name: {name}) using grounded web search.

STEP 1 - FINDINGS (prose, first)
Start your answer with a "FINDINGS" section written in plain prose. Write one short sentence per field, before any JSON. Each sentence must:
- be written so it can be copied verbatim (minus the trailing source URL) into the matching JSON value, including names, places, numbers, dates and category labels (for example, if the JSON says "Austin, TX", write "Austin, TX" in the sentence, not "Austin, Texas");
- end with the source URL in parentheses;
- exist only when a search result supports it. When nothing supports a field, write "No verified information found for <field>." instead of guessing.

Example shape (not real data):
FINDINGS
Industry: Acme operates in the cybersecurity industry, selling cloud security software (https://acme.com/about).
Business category: Acme is a cybersecurity company (https://acme.com/about).
Headquarters: Acme is headquartered in Austin, TX, United States (https://acme.com/contact).
Employee count: Acme has approximately 500 employees (https://acme.com/about).
Revenue: Acme reports annual revenue of $50M (https://acme.com/investors).
Funding: Acme raised a $10M Series A round (https://acme.com/news).
Official website: Acme's official website is https://acme.com (https://acme.com).
Company type: Acme is a privately held company (https://acme.com/about).
Services: Acme provides software implementation services (https://acme.com/services).
Leadership: A. Example is the CEO of Acme (https://acme.com/team).
Hiring and service fit: Acme lists engineering roles on its careers page (https://acme.com/careers).
Description: Acme provides workflow software for operations teams (https://acme.com/about).

STEP 2 - JSON (last)
After the findings, return one JSON object with exactly these fields:
{{
    "official_website": "https://company.example or null",
        "official_website_source_url": "URL supporting the official website or null",
        "official_website_excerpt": "exact supporting excerpt from that source or null",
    "headquarters": {{"city": "...", "state": "...", "country": "..."}} or null,
        "headquarters_source_url": "URL supporting headquarters or null",
        "headquarters_excerpt": "exact supporting excerpt from that source or null",
    "company_type": "ownership/maturity only: Startup, SMB, System Integrator, Public Company, Private Company, or another explicit type | null",
        "company_type_source_url": "URL supporting company_type or null",
        "company_type_excerpt": "exact supporting excerpt from that source or null",
    "business_category": "what the company does: Cybersecurity, SaaS, AI/ML, IT Services, Healthcare Technology, or another category explicitly supported by the source | null",
        "business_category_source_url": "URL supporting business_category or null",
        "business_category_excerpt": "exact supporting excerpt from that source or null",
  "employee_count": <number or null>,
    "employee_count_source_url": "URL supporting employee_count or null",
        "employee_count_excerpt": "exact supporting excerpt from that source or null",
  "revenue": "<string like '$50M' or '$1.2B' or null>",
    "revenue_source_url": "URL supporting revenue or null",
        "revenue_excerpt": "exact supporting excerpt from that source or null",
  "funding": "<string like '$100M Series C' or null>",
    "funding_source_url": "URL supporting funding or null",
        "funding_excerpt": "exact supporting excerpt from that source or null",
  "industry": "<one primary industry phrase, 1-3 words, e.g. 'Cybersecurity' or 'Marketing Automation', or null>",
        "industry_source_url": "URL supporting industry or null",
        "industry_excerpt": "exact supporting excerpt from that source or null",
  "description": "<1-2 sentence description of what the company does>",
        "description_source_url": "URL supporting description or null",
        "description_excerpt": "exact supporting excerpt from that source or null",
    "employee_range": "exact source wording or null",
    "employee_range_source_url": "exact supporting URL or null",
    "employee_range_excerpt": "exact supporting excerpt from that source or null",
    "services": "short factual summary or null",
    "services_source_url": "exact supporting URL or null",
    "services_excerpt": "exact supporting excerpt from that source or null",
    "leadership": [{{"name": "...", "title": "...", "source_url": "...", "excerpt": "..."}}],
    "hiring_service_fit": "documented hiring or staffing/service-fit fact, or null",
    "hiring_service_fit_source_url": "exact supporting URL or null",
    "hiring_service_fit_excerpt": "exact supporting excerpt from that source or null",
    "research_evidence": [
        {{"field": "leadership|services|hiring_service_fit|employee_range|industry|description|business_category", "criterion": "company_leadership|company_services|company_hiring_fit|automatic_company_size|company_industry|company_description|automatic_business_category", "claim": {{}}, "source_name": "source title", "source_url": "exact URL", "excerpt": "exact supporting excerpt", "confidence": "high|medium|low", "evidence_type": "direct|inferred"}}
    ],
    "classification_evidence": [
        {{"category": "startup|smb|system_integrator", "supports": true, "fact": "short exact fact", "why": "why this supports or contradicts the category", "source_name": "source title", "source_url": "exact supporting URL", "excerpt": "exact supporting excerpt", "confidence": "high|medium|low", "evidence_type": "direct|inferred"}}
    ]
}}

Important rules:
- Write the FINDINGS prose first. The JSON object comes last, after the findings. Never answer with JSON alone.
- Copy each findings sentence, without its trailing source URL, into the matching JSON value. This applies in particular to description, services, hiring_service_fit and industry: use exactly the findings wording, do not paraphrase or lengthen it in the JSON.
- For every non-null field, its *_excerpt must contain the same wording as the JSON value; for official_website the excerpt must include the URL itself.
- Use null for any field you cannot determine with reasonable confidence. Never guess, and never fill a field from general knowledge about the company name.
- For revenue, if the company is private and revenue is not public, return null.
- Employee count should be the approximate current number.
- company_type is ownership/maturity only (Startup, SMB, System Integrator, Public Company, Private Company). Do not use it to describe what the company does.
- business_category is what the company actually does (Cybersecurity, SaaS, AI/ML, IT Services, Healthcare Technology, or another category the source explicitly supports). Use an explicit category word from the source, not a guess, and do not reuse company_type wording.
- industry must be one short primary-industry phrase (1-3 words). Be specific rather than generic ("Software / DevOps" is acceptable, "Technology" alone is not).
- Identify headquarters from the company's official About/Contact page, LinkedIn, Crunchbase, or another reputable company profile. Never infer headquarters from job-posting locations.
- Do not call a company a startup merely because it raised funding, and do not classify it from estimated employee count alone. Use explicit company identity language from a reliable source when available.
- Source URLs must be exact URLs from the provided search results, including any Google redirect URLs exactly as they appear; use null when no result supports a field. Never rewrite, shorten, or canonicalize a redirect URL.
- Source quality priority: (1) official company website and leadership/team pages, (2) official press releases, investor relations, regulatory filings, and company announcements, (3) reputable third-party business/industry sources, (4) other sources only when sufficiently corroborated. Prefer the highest-priority source that genuinely supports the claim.
- Financial semantics: revenue means total revenue for a stated period; ARR means annual recurring revenue; funding raised means capital raised in a round; valuation means company value. Never report valuation as funding, ARR as revenue, or estimated revenue as reported revenue. Preserve the source's original wording, reporting period, currency, and metric type. If the metric type is ambiguous, return null.
- Leadership must use one consistent schema: each item is an object with exactly name and title (plus source_url and excerpt). Every leadership item needs both a name and a title; omit items missing either.
- company_type is ownership/maturity only. business_category is what the company does. Never merge these concepts and never use one as evidence for the other.
- Research these independently: official website, headquarters, current employee count/range, explicit company classification, business category, named leadership, services, and documented hiring/staffing signals.
- For leadership, include only names and titles explicitly present in a source. Do not claim a person is a validated decision-maker; that requires the existing contact validation flow.
- Keep company services separate from hiring/service-fit. Do not claim staffing openness or CSG service fit from a general services description.
- Every research_evidence item must quote a concise supporting excerpt and use a URL present in grounding results. Do not create evidence for unsupported fields.
- Every non-null fact must include its matching source URL and exact supporting excerpt. The excerpt must be copied verbatim from the supplied source content; do not paraphrase it.
- Run targeted research for each category: founding date/funding stage and explicit startup language for startup; reliable employee-count/company-size facts for SMB; and explicit evidence that the company provides systems integration, technology implementation, IT consulting, or integration services to clients for system integrator.
- Return classification_evidence only when the extracted fact supports or reasonably contradicts that category. Never use a generic company URL as evidence and never infer SMB from an unsupported estimate.
"""


_GROUNDING_UNAVAILABLE_REASON = (
    "provider grounding support was unavailable, so the claim could not be verified"
)


def _apply_firmographics(
    company: Company, data: dict[str, Any], source: str, allowed_source_urls: set[str] | None = None,
    searched_fields: set[str] | None = None, source_content: dict[str, str] | None = None,
    strict_response: bool = False, grounding_supports: dict[str, list[str]] | None = None,
    grounding_redirects: dict[str, str] | None = None,
    grounding_available: bool | None = None,
) -> None:
    """Apply supported facts and retain a field-level account of each result."""
    now = _utc_now_iso()
    if grounding_available is None:
        # For Gemini, no provider support metadata means no claim can be
        # verified from this response. The model's own prose and excerpts are
        # not evidence and must never stand in for the provider's association.
        grounding_available = (
            bool(grounding_supports) if source == "gemini_grounded_search" else True
        )
    allowed_source_urls = {
        canonical
        for url in (allowed_source_urls or set())
        if (canonical := _normalize_grounding_url(url)) is not None
    }
    source_content = source_content or {}
    # Resolved grounding redirects: real source URL -> the chunk that grounded
    # it. A model-cited real URL is accepted only through that chunk.
    redirect_map = {
        canonical: str(final).strip()
        for chunk, final in (grounding_redirects or {}).items()
        if (canonical := _normalize_grounding_url(chunk)) is not None
    }
    resolved_source_urls: dict[str, str] = {}
    for chunk, value in redirect_map.items():
        canonical_final = _normalize_grounding_url(value)
        if canonical_final is not None:
            resolved_source_urls[canonical_final] = chunk

    def stored_source(url: Any) -> Any:
        """Store the real page URL, never Google's opaque redirect."""
        if not isinstance(url, str):
            return url
        return redirect_map.get(_normalize_grounding_url(url) or "", url)

    def candidate_chunks(url: Any) -> list[str]:
        return _grounding_candidate_chunks(url, allowed_source_urls, resolved_source_urls)

    def is_provider_source(url: Any) -> bool:
        return bool(candidate_chunks(url))

    # Domains proven to belong to the company by a verified relationship (not
    # guessed from the name). Every resolved tier for this response consults it.
    official_aliases: set[str] = set()
    # Accepted value -> the provider segment that established it. Evidence for
    # the value is written from this, never from a model-paraphrased excerpt.
    accepted_support: dict[str, tuple[str, str]] = {}

    def tier_for(url: Any) -> int:
        # Tier the page a reviewer would open, not Google's opaque redirect:
        # an official site reached through a redirect would otherwise be
        # graded as an unclassified aggregator.
        return _source_quality_tier(stored_source(url), company.domain, official_aliases)

    def tiered_confidence(url: Any, stated: Any) -> str:
        """A weak source can never be presented as high confidence."""
        return _tier_capped_confidence(stated, tier_for(url))

    def grounded_supports_for(claim: dict[str, Any]) -> list[tuple[int, int, str, str]]:
        """Provider support segments that state the whole claim, best source first.

        Segments come from Google's grounding metadata, not from the model's
        prose or its excerpt fields, so a model paraphrase cannot manufacture
        its own support. Only clean (un-corrupted) segments qualify.
        """
        supported: list[tuple[int, int, str, str]] = []
        for order, (chunk, segments) in enumerate((grounding_supports or {}).items()):
            for segment in segments:
                if not isinstance(segment, str) or _text_is_malformed(segment):
                    continue
                if _claim_matches_excerpt(claim, segment):
                    supported.append((tier_for(chunk), order, chunk, segment))
        supported.sort(key=lambda item: (item[0], item[1]))
        return supported

    def best_grounded_support(claim: dict[str, Any]) -> Optional[tuple[int, int, str, str]]:
        supports = grounded_supports_for(claim)
        return supports[0] if supports else None

    def grounding_supports_claim(url: Any, claim: dict[str, Any]) -> bool:
        for chunk in candidate_chunks(url):
            segments = (grounding_supports or {}).get(chunk, [])
            if any(_claim_matches_excerpt(claim, segment) for segment in segments):
                return True
        return False

    def verified_official_alias_hosts() -> set[str]:
        """Domains the company demonstrably owns, proven by a grounded disclosure.

        The response may name an official site on a different domain (a global
        brand, a post-rebrand domain). It is accepted only when the cited page
        is the company's own domain or a first-party disclosure (press wire,
        regulator) and the provider support segment for that page states the
        alias URL. A directory, the alias page itself, or the model's excerpt
        cannot establish the relationship.
        """
        website = data.get("official_website")
        if not isinstance(website, str) or not website.strip():
            return set()
        try:
            parsed = httpx.URL(website)
        except Exception:
            return set()
        if parsed.scheme not in {"http", "https"}:
            return set()
        alias_host = (parsed.host or "").lower().removeprefix("www.")
        if not alias_host or _host_within_domain(alias_host, company.domain):
            return set()
        disclosure = data.get("official_website_source_url")
        if not isinstance(disclosure, str) or not is_provider_source(disclosure):
            return set()
        disclosure_page = stored_source(disclosure)
        if _source_quality_tier(disclosure_page, company.domain) > SOURCE_TIER_FIRST_PARTY:
            return set()
        if not grounding_supports_claim(disclosure, {"official_website": website}):
            return set()
        return {alias_host}

    official_aliases.update(verified_official_alias_hosts())
    searched_fields = set(_FIELD_CRITERIA) if searched_fields is None else searched_fields
    initial_employee_count = company.employee_count
    outcomes = {
        field: {"status": "not_searched", "criterion": criterion, "checked_at": now}
        for field, criterion in _FIELD_CRITERIA.items()
    }
    # A field can be (a) newly verified in this response, (b) carried forward
    # from a value we already stored, or (c) a discovery hint we have never
    # verified. Only (a) is new research; (b) and (c) must never be presented
    # as if this run proved them.
    accepted_fields: set[str] = set()
    carried_fields: set[str] = set()
    hint_fields: set[str] = set()
    hint_values: dict[str, str] = {}
    rejection_reasons: dict[str, str] = {}
    # Caveats about an accepted value. Item-level failures are not notes: the
    # field they belong to was dropped, not the accepted value.
    outcome_notes: dict[str, str] = {}

    for outcome_field, company_field in (
        ("official_website", "website"),
        ("headquarters", "headquarters"),
        ("company_classification", "company_type"),
        ("business_category", "business_category"),
        ("employee_range", "employee_count"),
        ("revenue", "revenue"),
        ("funding", "funding"),
    ):
        prior = getattr(company, company_field)
        if prior is not None and _normalize_grounding_url(getattr(prior, "url", None)):
            carried_fields.add(outcome_field)
    for item in company.research_evidence:
        if isinstance(item, dict) and item.get("field") in _FIELD_CRITERIA:
            carried_fields.add(str(item["field"]))
    if company.industry:
        if "industry" not in carried_fields:
            # Discovery writes a bare industry string with no provenance. Keep
            # it, but label it as an unverified hint until grounded research
            # confirms it.
            hint_fields.add("industry")
            hint_values["industry"] = company.industry
    if company.description:
        carried_fields.add("description")

    def fact_source_url(field: str) -> str | None:
        candidate = data.get(f"{field}_source_url")
        if field == "employee_count" and candidate is None:
            candidate = data.get("employee_range_source_url")
        if not isinstance(candidate, str):
            return None
        canonical_candidate = _normalize_grounding_url(candidate)
        if canonical_candidate is None or not is_provider_source(candidate):
            return None
        try:
            parsed_url = httpx.URL(candidate)
            return candidate if parsed_url.scheme in {"http", "https"} and parsed_url.host else None
        except Exception:
            return None

    def fact_claim(field: str) -> dict[str, Any]:
        claim = data.get(field)
        if field == "employee_count" and claim is None:
            claim = data.get("employee_range")
        return {field: claim}

    def fact_source_is_valid(field: str, source_url: str) -> bool:
        if source not in {"gemini_grounded_search", "groq_tavily_search"}:
            return True
        claim = fact_claim(field)
        if source == "gemini_grounded_search":
            # The provider's grounding segment -- not the model's excerpt -- is
            # what establishes the claim. A paraphrased model excerpt is no
            # longer a rejection; a claim the provider never associated with a
            # source still is.
            return grounding_available and best_grounded_support(claim) is not None
        excerpt = data.get(f"{field}_excerpt")
        if not _claim_matches_excerpt(claim, str(excerpt or "")):
            return False
        return _has_source_excerpt(source_url, excerpt, source_content)

    def fact_source_rejection_reason(field: str, source_url: str) -> str:
        claim = fact_claim(field)
        if source == "gemini_grounded_search":
            if not grounding_available:
                return _GROUNDING_UNAVAILABLE_REASON
            if not is_provider_source(source_url):
                return "cited URL is not among the grounded sources for this response"
            if best_grounded_support(claim) is None:
                return "provider grounding text does not state the claim"
            return "supporting evidence failed validation"
        excerpt = data.get(f"{field}_excerpt")
        if not _claim_matches_excerpt(claim, str(excerpt or "")):
            return "supporting excerpt does not contain the claim"
        if source == "groq_tavily_search" and not _has_source_excerpt(
            source_url, excerpt, source_content,
        ):
            return "supporting excerpt is not present in cited source content"
        return "supporting evidence failed validation"

    def support_text_for(url: Any) -> str:
        parts: list[str] = []
        for chunk in candidate_chunks(url):
            parts.extend((grounding_supports or {}).get(chunk, []))
        return " ".join(parts)

    def financial_context(field: str) -> str:
        """Excerpt plus the best provider support sentence for the claim."""
        support = best_grounded_support(fact_claim(field))
        return " ".join(filter(None, (
            str(data.get(f"{field}_excerpt") or ""),
            support[3] if support else "",
            support_text_for(fact_source_url(field)),
        )))

    def save_fact(field: str, value: Any, source_field: str, outcome_field: str, *, method: str = "reported",
                  confidence: str = "high") -> bool:
        source_url = fact_source_url(source_field)
        if value is None:
            return False
        if _has_malformed_text(value):
            # Never store corrupted text as a verified fact (for example a
            # replacement-character city name such as "S\ufffdo Paulo").
            rejection_reasons[outcome_field] = (
                "value contains malformed or replacement characters"
            )
            return False
        if source == "gemini_grounded_search" and not grounding_available:
            rejection_reasons[outcome_field] = _GROUNDING_UNAVAILABLE_REASON
            return False
        if field in _FINANCIAL_FIELD_CONFLICTS:
            # Revenue and funding must not be stored from a claim that names a
            # different financial concept (valuation, market cap, the other
            # metric), however well the source is grounded.
            valid, mismatch = _financial_value_is_valid(
                field, value, financial_context(source_field),
            )
            if not valid:
                rejection_reasons[outcome_field] = mismatch
                return False
        if not source_url:
            candidate = data.get(f"{source_field}_source_url")
            candidate_canonical = _normalize_grounding_url(candidate)
            if candidate_canonical is not None and candidate_canonical in allowed_source_urls:
                try:
                    parsed_url = httpx.URL(candidate)
                    valid_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.host)
                except Exception:
                    valid_url = False
                if not valid_url:
                    rejection_reasons[outcome_field] = "supporting URL is not a valid HTTP(S) URL"
                    return False
            rejection_reasons[outcome_field] = "supporting URL missing or not present in provider results"
            return False
        if not fact_source_is_valid(source_field, source_url):
            rejection_reasons[outcome_field] = fact_source_rejection_reason(source_field, source_url)
            return False
        existing = getattr(company, field)
        if existing is not None:
            carried_fields.add(outcome_field)
            return True
        # Store the source that actually states the claim. When more than one
        # grounded source does, the best tier wins instead of whatever page
        # the model happened to cite (official page over an aggregator).
        support = (
            best_grounded_support(fact_claim(source_field))
            if source == "gemini_grounded_search" else None
        )
        stored_url = support[2] if support else source_url
        if support:
            accepted_support[outcome_field] = (support[2], support[3])
        setattr(company, field, ProvenanceValue(
            value=value, source=source, url=stored_source(stored_url),
            fetched_at=now, method=method,
            # Source quality caps the stored confidence too: an unclassified
            # aggregator must not leave a value labelled "high".
            confidence=tiered_confidence(stored_url, confidence),
        ))
        accepted_fields.add(outcome_field)
        return True

    official_website = data.get("official_website")
    if isinstance(official_website, str) and official_website.startswith(("http://", "https://")):
        parsed = httpx.URL(official_website)
        domain = company.domain.lower().removeprefix("www.")
        host = (parsed.host or "").lower().removeprefix("www.")
        if _host_within_domain(host, domain) or any(
            _host_within_domain(host, alias) for alias in official_aliases
        ):
            save_fact("website", official_website, "official_website", "official_website")
        else:
            rejection_reasons["official_website"] = "website host does not match the company domain"
    elif data.get("official_website"):
        rejection_reasons["official_website"] = "website is not an absolute HTTP(S) URL"

    headquarters = data.get("headquarters")
    if isinstance(headquarters, dict) and any(headquarters.get(key) for key in ("city", "state", "country")):
        save_fact("headquarters", {
            key: str(headquarters[key]).strip()
            for key in ("city", "state", "country") if headquarters.get(key)
        }, "headquarters", "headquarters")

    company_type = data.get("company_type")
    if isinstance(company_type, str) and company_type.strip():
        normalized_type = company_type.strip().lower().replace(" ", "_")
        if normalized_type not in {"startup", "smb", "system_integrator"}:
            normalized_type = company_type.strip()
        save_fact("company_type", normalized_type, "company_type", "company_classification")

    # --- Business category (what the company does, not its ownership type) ---
    business_category = data.get("business_category")
    if isinstance(business_category, str) and business_category.strip():
        save_fact(
            "business_category", business_category.strip(),
            "business_category", "business_category",
        )

    classification_evidence = data.get("classification_evidence")
    if isinstance(classification_evidence, list):
        validated: list[dict[str, Any]] = []
        support_values: dict[str, set[bool]] = {}
        for item in classification_evidence:
            if isinstance(item, dict) and isinstance(item.get("supports"), bool):
                category = str(item.get("category") or "").strip().lower()
                support_values.setdefault(category, set()).add(item["supports"])
        conflicting_categories = {
            category for category, values in support_values.items() if len(values) > 1
        }
        for item in classification_evidence:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip().lower()
            if category in conflicting_categories:
                rejection_reasons["company_classification"] = "conflicting classification evidence supplied for the same category"
                continue
            source_url = item.get("source_url")
            fact = str(item.get("fact") or "").strip()
            why = str(item.get("why") or "").strip()
            if category not in {"startup", "smb", "system_integrator"}:
                continue
            if source == "gemini_grounded_search" and not grounding_available:
                rejection_reasons["company_classification"] = _GROUNDING_UNAVAILABLE_REASON
                continue
            if not isinstance(source_url, str) or not source_url.strip():
                rejection_reasons["company_classification"] = "classification evidence source URL is missing"
                continue
            if not fact or not why:
                rejection_reasons["company_classification"] = "classification evidence URL missing or not present in provider results"
                continue
            if _has_malformed_text(fact) or _has_malformed_text(why):
                rejection_reasons["company_classification"] = (
                    "classification evidence contains malformed or replacement characters"
                )
                continue
            try:
                parsed_url = httpx.URL(source_url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
                    raise ValueError("invalid source URL")
            except Exception:
                rejection_reasons["company_classification"] = "classification evidence URL is not a valid HTTP(S) URL"
                continue
            if not is_provider_source(source_url):
                rejection_reasons["company_classification"] = "classification evidence URL not present in provider results"
                continue
            excerpt = item.get("excerpt")
            evidence_source = source_url
            if source == "gemini_grounded_search":
                support = best_grounded_support({"fact": fact})
                if support is None:
                    rejection_reasons["company_classification"] = (
                        _GROUNDING_UNAVAILABLE_REASON if not grounding_available
                        else "provider grounding text does not state the classification claim"
                    )
                    continue
                evidence_source, excerpt = support[2], support[3]
            else:
                if source == "groq_tavily_search" and not _has_source_excerpt(
                    source_url, excerpt, source_content,
                ):
                    rejection_reasons["company_classification"] = "classification excerpt missing or not found in cited source content"
                    continue
                if not _claim_matches_excerpt({"fact": fact}, str(excerpt or "")):
                    rejection_reasons["company_classification"] = "classification claim is not represented in its supporting excerpt"
                    continue
            if _text_is_malformed(str(excerpt or "")):
                rejection_reasons["company_classification"] = (
                    "classification evidence contains malformed or replacement characters"
                )
                continue
            if not isinstance(item.get("supports"), bool):
                continue
            evidence_type = str(item.get("evidence_type") or "").lower()
            if evidence_type not in {"direct", "inferred"}:
                continue
            confidence = str(item.get("confidence") or "medium").lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            validated.append({
                "category": category, "supports": item["supports"], "fact": fact,
                "why": why, "source_name": str(item.get("source_name") or evidence_source),
                "source_url": stored_source(evidence_source), "excerpt": str(excerpt or ""),
                "confidence": tiered_confidence(evidence_source, confidence),
                "source_tier": tier_for(evidence_source),
                "evidence_type": evidence_type, "retrieved_at": now,
            })
        existing_keys = {
            (item.get("category"), item.get("supports"), item.get("fact"), item.get("source_url"))
            for item in company.classification_evidence
        }
        for item in validated:
            key = (item["category"], item["supports"], item["fact"], item["source_url"])
            if key not in existing_keys:
                company.classification_evidence.append(item)
                existing_keys.add(key)
        if validated:
            accepted_fields.add("company_classification")
        elif isinstance(classification_evidence, list) and classification_evidence:
            rejection_reasons.setdefault(
                "company_classification", "classification evidence failed source or claim validation",
            )

    # --- Employee Count ---
    emp = data.get("employee_count")
    if emp is not None:
        parsed_emp = _parse_number(str(emp))
        if parsed_emp and parsed_emp > 0:
            save_fact("employee_count", parsed_emp, "employee_count", "employee_range",
                      method="estimated", confidence="medium")

    # --- Revenue ---
    rev = _parse_revenue(data.get("revenue") or "")
    if rev and company.revenue is not None:
        carried_fields.add("revenue")
    elif rev:
        save_fact(
            "revenue", rev, "revenue", "revenue", method="estimated",
            confidence="medium" if "$" in rev else "low",
        )

    # --- Funding ---
    funding = data.get("funding")
    if funding and str(funding).lower() not in ("null", "none", "n/a", "unknown"):
        if company.funding is not None:
            carried_fields.add("funding")
        else:
            save_fact("funding", str(funding), "funding", "funding", method="estimated")

    accepted_research: list[dict[str, Any]] = []
    research_evidence = list(data.get("research_evidence") or []) if isinstance(data.get("research_evidence"), list) else []
    field_sources = {
        "official_website": (data.get("official_website"), data.get("official_website_source_url")),
        "headquarters": (data.get("headquarters"), data.get("headquarters_source_url")),
        "company_classification": (data.get("company_type"), data.get("company_type_source_url")),
        "business_category": (data.get("business_category"), data.get("business_category_source_url")),
        "employee_range": (data.get("employee_range") or data.get("employee_count"),
                           data.get("employee_range_source_url") or data.get("employee_count_source_url")),
        "revenue": (data.get("revenue"), data.get("revenue_source_url")),
        "funding": (data.get("funding"), data.get("funding_source_url")),
        "services": (data.get("services"), data.get("services_source_url")),
        "hiring_service_fit": (data.get("hiring_service_fit"), data.get("hiring_service_fit_source_url")),
        "industry": (data.get("industry"), data.get("industry_source_url")),
        "description": (data.get("description"), data.get("description_source_url")),
    }
    def field_excerpt(field: str) -> Any:
        for key in (field, *_EVIDENCE_EXCERPT_ALIASES.get(field, ())):
            excerpt_value = data.get(f"{key}_excerpt")
            if excerpt_value not in (None, ""):
                return excerpt_value
        return ""

    def value_evidence_exists(field: str, claim: Any, source_url: Any, excerpt: Any) -> bool:
        key = _evidence_key(field, _FIELD_CRITERIA[field], claim, source_url, excerpt)
        return any(
            isinstance(item, dict) and item.get("evidence_key") == key
            for item in list(research_evidence) + list(company.research_evidence)
        )

    for field, (value, source_url) in field_sources.items():
        if value in (None, "", [], {}):
            continue
        if field in _EVIDENCE_ONLY_FIELDS and any(
            isinstance(item, dict) and item.get("field") == field
            for item in research_evidence
        ):
            # These fields are accepted claim-by-claim from research_evidence;
            # when the model supplied those claims, a bare top-level string is
            # not an additional verified claim.
            continue
        if not isinstance(source_url, str) or not source_url.strip():
            # Evidence without a citable source is not evidence; the field's
            # own outcome already records the missing-URL rejection.
            continue
        claim = {field: value}
        # Prefer the provider segment that actually established the value; the
        # model's *_excerpt is only a fallback for non-Gemini sources or for a
        # field this response did not accept.
        support = accepted_support.get(field)
        evidence_source, excerpt = support if support else (source_url, field_excerpt(field))
        # Dedupe by evidence identity, not by field: a model-supplied evidence
        # item for the same field may cite a different source and claim, and
        # the stored value still needs its own auditable provenance.
        if value_evidence_exists(field, claim, evidence_source, excerpt):
            continue
        research_evidence.append({
            "field": field, "criterion": _FIELD_CRITERIA[field],
            "claim": claim, "source_name": str(evidence_source or ""),
            "source_url": stored_source(evidence_source), "excerpt": excerpt,
            "evidence_type": "inferred", "confidence": tiered_confidence(evidence_source, "medium"),
            "source_tier": tier_for(evidence_source),
        })
    raw_leadership = data.get("leadership")
    leadership = _normalize_leadership(raw_leadership)
    if raw_leadership and not leadership and _has_malformed_text(raw_leadership):
        rejection_reasons["leadership"] = (
            "leadership contains malformed or replacement characters"
        )
    # One evidence item per person: the structured list carries each person's
    # own excerpt, which is the only text that can ground that person.
    for person in leadership:
        claim = {key: person[key] for key in ("name", "title") if person.get(key)}
        person_url = person.get("source_url")
        person_excerpt = str(person.get("excerpt") or "").strip()
        if any(
            isinstance(item, dict) and item.get("field") == "leadership"
            and item.get("claim") == claim
            and _normalize_grounding_url(item.get("source_url"))
            == _normalize_grounding_url(stored_source(person_url))
            and str(item.get("excerpt") or "").strip() == person_excerpt
            for item in research_evidence
        ):
            continue
        research_evidence.append({
            "field": "leadership", "criterion": _FIELD_CRITERIA["leadership"],
            "claim": claim, "source_name": str(person.get("source_name") or person.get("source_url") or ""),
            "source_url": stored_source(person_url),
            "excerpt": person_excerpt,
            "evidence_type": "direct",
            "confidence": tiered_confidence(person_url, "high"),
            "source_tier": tier_for(person_url),
        })
    claims_by_field: dict[str, set[str]] = {}
    for field, (value, _source_url) in field_sources.items():
        if field not in _EVIDENCE_CONFLICT_FIELDS:
            continue
        if value not in (None, "", [], {}):
            claims_by_field.setdefault(field, set()).add(json.dumps({field: value}, sort_keys=True, default=str))
    for item in research_evidence:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        claim = item.get("claim")
        if field in _EVIDENCE_CONFLICT_FIELDS and isinstance(claim, dict):
            claims_by_field.setdefault(field, set()).add(json.dumps(claim, sort_keys=True, default=str))
    for person in leadership:
        claim = {key: person[key] for key in ("name", "title") if person.get(key)}
        if claim:
            claims_by_field.setdefault("leadership", set()).add(json.dumps(claim, sort_keys=True, default=str))
    conflicting_fields: set[str] = set()
    multi_value_fields = {"leadership", "services"}
    for field, claims in claims_by_field.items():
        if len(claims) < 2 or field in multi_value_fields:
            continue
        if field == "employee_range":
            bounds = [
                _employee_range_bounds(json.loads(claim))
                for claim in claims
            ]
            comparable = [item for item in bounds if item is not None]
            if any(
                left[1] < right[0] or right[1] < left[0]
                for index, left in enumerate(comparable)
                for right in comparable[index + 1:]
            ):
                conflicting_fields.add(field)
            continue
        conflicting_fields.add(field)
    if "employee_range" in conflicting_fields:
        company.employee_count = initial_employee_count
    for field in conflicting_fields:
        rejection_reasons[field] = "conflicting claims supplied for the same field"

    # Every stored value must be locatable in the evidence that backs it. The
    # model can return both a range string and a count; whichever one is stored
    # gets its own item from the excerpt that actually supports it.
    for outcome_field, attribute in _PROVENANCE_OUTCOME_FIELDS.items():
        stored = getattr(company, attribute, None)
        if stored is None:
            continue
        stored_url = getattr(stored, "url", None)
        if not isinstance(stored_url, str) or not stored_url:
            continue
        claim = {outcome_field: stored.value}
        if any(
            isinstance(item, dict) and item.get("field") == outcome_field
            and item.get("claim") == claim
            and _normalize_grounding_url(item.get("source_url"))
            == _normalize_grounding_url(stored_url)
            for item in research_evidence
        ):
            continue
        support = accepted_support.get(outcome_field)
        if support:
            evidence_source, excerpt = support
        else:
            excerpt = ""
            for excerpt_key in (outcome_field, *_EVIDENCE_EXCERPT_ALIASES.get(outcome_field, ())):
                candidate = data.get(f"{excerpt_key}_excerpt")
                if isinstance(candidate, str) and candidate.strip() and _claim_matches_excerpt(
                    claim, candidate,
                ):
                    excerpt = candidate.strip()
                    break
            if not excerpt:
                continue
            evidence_source = stored_url
        research_evidence.append({
            "field": outcome_field, "criterion": _FIELD_CRITERIA[outcome_field],
            "claim": claim, "source_name": stored_source(evidence_source),
            "source_url": stored_source(evidence_source),
            "excerpt": excerpt, "evidence_type": "inferred",
            "confidence": tiered_confidence(evidence_source, "medium"),
            "source_tier": tier_for(evidence_source),
        })

    # A count that no returned size band can contain is worth flagging to the
    # reviewer, but the value passed its own source check, so it is kept.
    if company.employee_count is not None and "employee_range" not in conflicting_fields:
        bands = [
            _employee_range_bounds(json.loads(claim))
            for claim in claims_by_field.get("employee_range", ())
        ]
        bands = [band for band in bands if band is not None]
        stored = company.employee_count
        stored_value = getattr(stored, "value", stored)
        if bands and isinstance(stored_value, (int, float)) and not any(
            low <= stored_value <= high for low, high in bands
        ):
            outcome_notes.setdefault(
                "employee_range",
                f"stored count {stored_value} is outside the size range the same response reported",
            )

    if isinstance(research_evidence, list):
        for item in research_evidence:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            criterion = _FIELD_CRITERIA.get(field)
            item_criterion = str(item.get("criterion") or "").strip()
            claim = item.get("claim")
            source_url = item.get("source_url")
            excerpt = str(item.get("excerpt") or "").strip()
            if field not in _EVIDENCE_FIELDS:
                continue
            if field in conflicting_fields:
                continue
            if (
                field not in _EVIDENCE_ONLY_FIELDS
                and field not in _DEFERRED_VALUE_EVIDENCE_FIELDS
                and field not in accepted_fields
            ):
                # The authoritative value check rejected (or carried) this
                # field, so no evidence item may outlive the value it claims.
                continue
            if item_criterion != criterion:
                rejection_reasons[field] = "evidence criterion does not match the field mapping"
                continue
            if source == "gemini_grounded_search" and not grounding_available:
                rejection_reasons[field] = _GROUNDING_UNAVAILABLE_REASON
                continue
            if not isinstance(source_url, str):
                rejection_reasons[field] = "source URL is missing"
                continue
            try:
                parsed_url = httpx.URL(source_url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
                    raise ValueError("invalid source URL")
            except Exception:
                rejection_reasons[field] = "source URL is not a valid HTTP(S) URL"
                continue
            if not is_provider_source(source_url):
                rejection_reasons[field] = "source URL not present in provider results"
                continue
            if not isinstance(claim, dict) or not claim:
                rejection_reasons[field] = "claim is missing"
                continue
            if _has_malformed_text(claim):
                rejection_reasons[field] = (
                    "claim contains malformed or replacement characters"
                )
                continue
            if field == "leadership" and isinstance(claim.get("leadership"), list):
                # One item bundling several people cannot be grounded in a
                # single person's excerpt; the structured list is validated
                # person by person instead of rejecting the whole team.
                continue
            if source == "gemini_grounded_search":
                # Google's grounding segment is the evidence. Swap out a
                # paraphrased model excerpt (and a weaker cited page) for the
                # provider segment that actually states the claim.
                support = best_grounded_support(claim)
                if support is None:
                    rejection_reasons[field] = (
                        _GROUNDING_UNAVAILABLE_REASON if not grounding_available
                        else "provider grounding text does not state the claim"
                    )
                    continue
                source_url, excerpt = support[2], support[3]
            else:
                if not excerpt:
                    rejection_reasons[field] = "claim or supporting excerpt is missing"
                    continue
                if source == "groq_tavily_search" and not _has_source_excerpt(
                    source_url, excerpt, source_content,
                ):
                    rejection_reasons[field] = "supporting excerpt not found in cited source content"
                    continue
                if not _claim_matches_excerpt(claim, excerpt):
                    rejection_reasons[field] = "claim is not represented in its supporting excerpt"
                    continue
            if _text_is_malformed(excerpt):
                rejection_reasons[field] = (
                    "supporting text contains malformed or replacement characters"
                )
                continue
            if any(_claim_is_call_to_action(str(value)) for value in claim.values()):
                rejection_reasons[field] = "claim is a call to action, not a description of the company"
                continue
            evidence_type = str(item.get("evidence_type") or "inferred").lower()
            if evidence_type not in {"direct", "inferred"}:
                evidence_type = "inferred"
            confidence = str(item.get("confidence") or "medium").lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            evidence_key = _evidence_key(field, item_criterion, claim, source_url, excerpt)
            accepted_research.append({
                "evidence_key": evidence_key, "field": field, "criterion": criterion,
                "claim": claim, "source_name": str(item.get("source_name") or source_url),
                "source_url": stored_source(source_url), "excerpt": excerpt,
                "evidence_type": evidence_type,
                "confidence": tiered_confidence(source_url, confidence),
                "source_tier": tier_for(source_url), "retrieved_at": now,
            })
            if field in _EVIDENCE_ONLY_FIELDS:
                accepted_fields.add(field)
        for item in research_evidence:
            if isinstance(item, dict) and item.get("field") in _FIELD_CRITERIA:
                field = str(item["field"])
                if field not in accepted_fields:
                    rejection_reasons.setdefault(field, "evidence failed source, excerpt, or criterion validation")

    previous_evidence: dict[str, dict[str, Any]] = {}
    seen_identities: set[tuple] = set()
    for item in company.research_evidence:
        if not isinstance(item, dict) or not item.get("evidence_key"):
            continue
        identity = _evidence_identity(item)
        if identity in seen_identities:
            # Two grounding chunks for one page produced duplicate copies.
            continue
        seen_identities.add(identity)
        previous_evidence.setdefault(item["evidence_key"], item)
    for item in accepted_research:
        identity = _evidence_identity(item)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        previous_evidence.setdefault(item["evidence_key"], item)
    company.research_evidence = list(previous_evidence.values())
    for field in conflicting_fields:
        accepted_fields.discard(field)

    # --- Industry ---
    industry = data.get("industry")
    industry_url = fact_source_url("industry")
    if industry and source == "gemini_grounded_search" and not grounding_available:
        rejection_reasons["industry"] = _GROUNDING_UNAVAILABLE_REASON
    elif industry and _has_malformed_text(str(industry)):
        rejection_reasons["industry"] = "value contains malformed or replacement characters"
    elif industry and str(industry).lower() not in ("null", "none", "n/a") and not industry_url:
        rejection_reasons["industry"] = "supporting URL missing or not present in provider results"
    elif industry and industry_url and not fact_source_is_valid("industry", industry_url):
        rejection_reasons["industry"] = fact_source_rejection_reason("industry", industry_url)
    elif industry and industry_url:
        if "industry" in hint_fields:
            # Verified research replaces the unverified discovery hint; the
            # hint itself is preserved on the field outcome below.
            company.industry = str(industry)
            hint_fields.discard("industry")
            accepted_fields.add("industry")
        elif not company.industry:
            company.industry = str(industry)
            accepted_fields.add("industry")
        else:
            carried_fields.add("industry")

    # --- Description ---
    desc = data.get("description")
    description_url = fact_source_url("description")
    if desc and source == "gemini_grounded_search" and not grounding_available:
        rejection_reasons["description"] = _GROUNDING_UNAVAILABLE_REASON
    elif desc and _has_malformed_text(str(desc)):
        rejection_reasons["description"] = "value contains malformed or replacement characters"
    elif desc and str(desc).lower() not in ("null", "none") and not description_url:
        rejection_reasons["description"] = "supporting URL missing or not present in provider results"
    elif desc and description_url and not fact_source_is_valid("description", description_url):
        rejection_reasons["description"] = fact_source_rejection_reason("description", description_url)
    elif desc and description_url:
        if not company.description:
            company.description = str(desc)
            accepted_fields.add("description")
        else:
            carried_fields.add("description")

    # Keep the legacy display field populated from the researched HQ envelope.
    if company.headquarters:
        hq = company.headquarters.get("value") if isinstance(company.headquarters, dict) else company.headquarters.value
        if isinstance(hq, dict) and hq.get("state"):
            company.hq_state = str(hq["state"])

    # A validated top-level claim can coexist with a rejected model-supplied
    # evidence item for the same field (different excerpt or claim shape).
    # Every accepted field still needs a persisted source URL and excerpt, so
    # synthesise one from the claim that already passed validation.
    value_fields = {
        "official_website": data.get("official_website"),
        "headquarters": data.get("headquarters"),
        "employee_range": data.get("employee_range") or data.get("employee_count"),
        "company_classification": data.get("company_type") or data.get("classification_evidence"),
        "business_category": data.get("business_category"),
        "leadership": leadership,
        "services": data.get("services"),
        "hiring_service_fit": data.get("hiring_service_fit"),
        "revenue": data.get("revenue"),
        "funding": data.get("funding"),
        "industry": data.get("industry"),
        "description": data.get("description"),
    }
    response_fields = {
        "official_website": "official_website",
        "headquarters": "headquarters",
        "employee_range": "employee_range",
        "company_classification": "company_type",
        "business_category": "business_category",
        "leadership": "leadership",
        "services": "services",
        "hiring_service_fit": "hiring_service_fit",
        "revenue": "revenue",
        "funding": "funding",
        "industry": "industry",
        "description": "description",
    }
    for field in _FIELD_CRITERIA:
        if field not in searched_fields:
            continue
        response_key = response_fields[field]
        if strict_response and response_key not in data and not (
            field == "employee_range" and "employee_count" in data
        ):
            outcomes[field] = {
                "status": "not_returned", "criterion": _FIELD_CRITERIA[field],
                "checked_at": now, "reason": "Gemini omitted required response field",
            }
            continue
        present = value_fields[field] not in (None, "", [], {})
        if field in accepted_fields:
            status = "accepted_and_mapped"
        elif field in carried_fields:
            status = "carried_forward"
        elif field in hint_fields:
            status = "discovery_hint_unverified"
        elif present or field in rejection_reasons:
            status = "found_but_rejected"
        else:
            status = "searched_but_not_found"
        outcome = {"status": status, "criterion": _FIELD_CRITERIA[field], "checked_at": now}
        if field in rejection_reasons and status == "carried_forward":
            # The stored value is still good; the note explains why this
            # response's claim for the same field was not used.
            outcome["note"] = rejection_reasons[field]
        elif field in rejection_reasons and status != "accepted_and_mapped":
            outcome["reason"] = rejection_reasons[field]
        elif status == "carried_forward":
            outcome["reason"] = (
                "previously stored value retained; no newly verified claim in this response"
            )
        elif status == "discovery_hint_unverified":
            outcome["reason"] = (
                "discovery hint retained; grounded research did not verify this field"
            )
        elif status == "searched_but_not_found":
            outcome["reason"] = {
                "employee_range": "Gemini returned no supported employee count or employee-range claim",
                "revenue": "Gemini returned no supported revenue claim",
            }.get(field, "Gemini explicitly returned no reliable information for this field")
        if field in outcome_notes:
            outcome["note"] = outcome_notes[field]
        if field in hint_values:
            outcome["discovery_hint"] = hint_values[field]
        evidence_ids = [
            item["evidence_key"] for item in company.research_evidence
            if isinstance(item, dict) and item.get("field") == field and item.get("evidence_key")
        ]
        if evidence_ids:
            outcome["evidence_keys"] = evidence_ids
        # Best (lowest) source tier backing this field: what a reviewer should
        # weigh when reading an accepted value.
        tiers = [
            item.get("source_tier") for item in company.research_evidence
            if isinstance(item, dict) and item.get("field") == field
            and isinstance(item.get("source_tier"), int)
        ]
        provenance_attribute = _PROVENANCE_OUTCOME_FIELDS.get(field)
        if provenance_attribute:
            stored = getattr(company, provenance_attribute, None)
            stored_url = getattr(stored, "url", None)
            if isinstance(stored_url, str) and stored_url:
                tiers.append(tier_for(stored_url))
        if tiers:
            outcome["source_tier"] = min(tiers)
        outcomes[field] = outcome
    company.research_outcomes.update(outcomes)

def _normalize_grounding_url(value: Any) -> Optional[str]:
    """Canonicalize a grounding URL without inventing or weakening provenance."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = httpx.URL(value)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            return None
        host = parsed.host.lower().removeprefix("www.")
        port = parsed.port
        if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            port = None
        netloc = host if port is None else f"{host}:{port}"
        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")
        else:
            path = ""
        canonical = f"{parsed.scheme}://{netloc}{path}"
        if parsed.query:
            canonical += f"?{parsed.query}"
        return canonical
    except Exception:
        return None


def _grounding_value(container: Any, *names: str) -> Any:
    if isinstance(container, dict):
        for name in names:
            if name in container:
                return container[name]
        return None
    for name in names:
        value = getattr(container, name, None)
        if value is not None:
            return value
    return None


def _extract_gemini_grounding_urls(response: Any) -> dict[str, str]:
    """Extract actual HTTP(S) grounding URIs from object or dictionary SDK shapes."""
    actual_urls: dict[str, str] = {}

    def add_uri(value: Any) -> None:
        canonical = _normalize_grounding_url(value)
        if canonical:
            actual_urls.setdefault(canonical, str(value).strip())

    candidates = _grounding_value(response, "candidates") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, (list, tuple)):
        candidates = []

    containers: list[Any] = [response]
    containers.extend(candidates)
    for container in containers:
        metadata = _grounding_value(container, "grounding_metadata", "groundingMetadata")
        if metadata is None:
            continue
        chunks = _grounding_value(metadata, "grounding_chunks", "groundingChunks") or []
        if isinstance(chunks, dict):
            chunks = [chunks]
        if not isinstance(chunks, (list, tuple)):
            continue
        for chunk in chunks:
            web = _grounding_value(chunk, "web")
            if web is None:
                continue
            add_uri(_grounding_value(web, "uri", "url"))
    return actual_urls


def _extract_gemini_grounding_supports(response: Any) -> dict[str, list[str]]:
    """Map grounded source URLs to answer segments Google associates with them."""
    supports_by_url: dict[str, list[str]] = {}
    candidates = _grounding_value(response, "candidates") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, (list, tuple)):
        candidates = []
    for container in [response, *candidates]:
        metadata = _grounding_value(container, "grounding_metadata", "groundingMetadata")
        if metadata is None:
            continue
        chunks = _grounding_value(metadata, "grounding_chunks", "groundingChunks") or []
        if isinstance(chunks, dict):
            chunks = [chunks]
        if not isinstance(chunks, (list, tuple)):
            continue
        urls_by_index: dict[int, str] = {}
        for index, chunk in enumerate(chunks):
            web = _grounding_value(chunk, "web")
            uri = _grounding_value(web, "uri", "url") if web is not None else None
            canonical = _normalize_grounding_url(uri)
            if canonical:
                urls_by_index[index] = canonical
        supports = _grounding_value(metadata, "grounding_supports", "groundingSupports") or []
        if isinstance(supports, dict):
            supports = [supports]
        if not isinstance(supports, (list, tuple)):
            continue
        for support in supports:
            segment = _grounding_value(support, "segment")
            segment_text = _grounding_value(segment, "text")
            if not isinstance(segment_text, str) or not segment_text.strip():
                continue
            chunk_indices = _grounding_value(
                support, "grounding_chunk_indices", "groundingChunkIndices",
            ) or []
            if not isinstance(chunk_indices, (list, tuple)):
                continue
            for index in chunk_indices:
                if isinstance(index, int) and index in urls_by_index:
                    supports_by_url.setdefault(urls_by_index[index], []).append(segment_text.strip())
    return supports_by_url


# Gemini's grounding chunks carry opaque redirect URLs, not the page URL.
# Resolving them gives provenance a human-checkable source and lets a claim
# that cites the real URL be tied back to the chunk that actually grounded it.
_GROUNDING_REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)
_GROUNDING_RESOLVE_TIMEOUT_SECONDS = 6.0
_GROUNDING_RESOLVE_MAX_URLS = 12
# These are plain HEAD requests to Google's redirector, not provider calls: the
# shared Gemini queue is untouched. A small bounded pool and a per-process cache
# only shorten how long one enrichment waits here, which the outer enrichment
# timeout would otherwise bill to an already-completed Gemini response.
_GROUNDING_RESOLVE_MAX_WORKERS = 4
_GROUNDING_RESOLVE_BUDGET_SECONDS = 20.0
_GROUNDING_REDIRECT_CACHE_MAX = 512
_GROUNDING_REDIRECT_CACHE: dict[str, str] = {}
_GROUNDING_REDIRECT_CACHE_LOCK = threading.Lock()


def _is_grounding_redirect(url: str) -> bool:
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:
        return False
    return any(host == name or host.endswith(f".{name}") for name in _GROUNDING_REDIRECT_HOSTS)


def _cached_grounding_redirect(canonical: str) -> Optional[str]:
    with _GROUNDING_REDIRECT_CACHE_LOCK:
        return _GROUNDING_REDIRECT_CACHE.get(canonical)


def _cache_grounding_redirect(canonical: str, final: str) -> None:
    with _GROUNDING_REDIRECT_CACHE_LOCK:
        if len(_GROUNDING_REDIRECT_CACHE) >= _GROUNDING_REDIRECT_CACHE_MAX:
            # Bound the process-lifetime cache; successful resolutions are
            # still cheaper to redo than to grow memory without limit.
            _GROUNDING_REDIRECT_CACHE.clear()
        _GROUNDING_REDIRECT_CACHE[canonical] = final


def _resolve_grounding_redirect(client: httpx.Client, url: str) -> Optional[str]:
    """One redirect hop to the real page URL, or None when it cannot be read."""
    try:
        response = client.head(url)
        if response.status_code >= 400:
            response = client.get(url)
        location = response.headers.get("location")
        if not isinstance(location, str) or not location.strip():
            return None
        # A relative Location is resolved against the redirector URL rather
        # than stored as an unusable path fragment.
        target = str(httpx.URL(url).join(location.strip())).strip()
        final = _normalize_grounding_url(target)
        canonical = _normalize_grounding_url(url)
        if not final or not canonical or final == canonical:
            return None
        return target
    except Exception:
        return None


def _cited_source_urls(data: Any) -> set[str]:
    """Every source URL a response cites, however deeply it is nested."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(item, str) and item.startswith(("http://", "https://"))
                    and (key == "source_url" or str(key).endswith("_source_url"))
                ):
                    found.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found


def _resolve_cited_redirects(
    data: Any, redirects: dict[str, str] | None,
) -> dict[str, str]:
    """Resolve cited redirects the chunk pass could not reach.

    The chunk list can be longer than the resolution budget, and a chunk left
    opaque is what the response ends up citing; persisted provenance must still
    be a page a human can open.
    """
    resolved = dict(redirects or {})
    unresolved = {
        url for url in _cited_source_urls(data)
        if _is_grounding_redirect(url)
        and (_normalize_grounding_url(url) or url) not in resolved
    }
    if unresolved:
        resolved.update(_resolve_grounding_urls(unresolved))
    return resolved


def _resolve_grounding_urls(
    chunk_urls: set[str],
    timeout: float = _GROUNDING_RESOLVE_TIMEOUT_SECONDS,
    max_urls: int = _GROUNDING_RESOLVE_MAX_URLS,
    budget: Optional[float] = None,
) -> dict[str, str]:
    """Resolve Google grounding redirects to their real source URLs.

    Best effort and read-only: a chunk that cannot be resolved keeps its
    redirect URL, which still resolves for a human. Returns
    ``{canonical chunk URL: final URL}``. Duplicate chunks are resolved once
    and successful resolutions are reused for the life of the process.
    """
    resolved: dict[str, str] = {}
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in sorted(chunk_urls):
        if len(seen) >= max_urls:
            break
        if not _is_grounding_redirect(url):
            continue
        canonical = _normalize_grounding_url(url)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        cached = _cached_grounding_redirect(canonical)
        if cached:
            resolved[canonical] = cached
        else:
            pending.append((canonical, url))
    if not pending:
        return resolved

    deadline = _time.monotonic() + (budget if budget is not None else _GROUNDING_RESOLVE_BUDGET_SECONDS)
    client = httpx.Client(follow_redirects=False, timeout=timeout)
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(_GROUNDING_RESOLVE_MAX_WORKERS, len(pending))),
    )
    futures = {
        executor.submit(_resolve_grounding_redirect, client, url): canonical
        for canonical, url in pending
    }
    try:
        while futures:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(list(futures), timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                canonical = futures.pop(future)
                try:
                    final = future.result()
                except Exception:
                    final = None
                if final:
                    resolved[canonical] = final
                    _cache_grounding_redirect(canonical, final)
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False)
        client.close()
    return resolved


def _gemini_grounding_shape(response: Any) -> dict[str, Any]:
    """Return non-sensitive response-shape counts to diagnose SDK/provider changes."""
    candidates = _grounding_value(response, "candidates") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, (list, tuple)):
        candidates = []
    containers = [response, *candidates]
    metadata_values = [
        _grounding_value(container, "grounding_metadata", "groundingMetadata")
        for container in containers
    ]
    metadata_values = [metadata for metadata in metadata_values if metadata is not None]
    chunk_count = 0
    support_count = 0
    for metadata in metadata_values:
        chunks = _grounding_value(metadata, "grounding_chunks", "groundingChunks") or []
        supports = _grounding_value(metadata, "grounding_supports", "groundingSupports") or []
        chunk_count += len(chunks) if isinstance(chunks, (list, tuple, dict)) else 0
        support_count += len(supports) if isinstance(supports, (list, tuple, dict)) else 0
    return {
        "response_type": type(response).__name__,
        "candidate_count": len(candidates),
        "grounding_metadata_count": len(metadata_values),
        "grounding_metadata_types": sorted({type(value).__name__ for value in metadata_values}),
        "grounding_chunk_count": chunk_count,
        "grounding_support_count": support_count,
    }


def _gemini_response_diagnostics(response: Any) -> dict[str, Any]:
    candidates = _grounding_value(response, "candidates") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, (list, tuple)):
        candidates = []

    finish_reasons = []
    for candidate in candidates:
        reason = _grounding_value(candidate, "finish_reason", "finishReason")
        if reason is not None:
            finish_reasons.append(getattr(reason, "name", str(reason)))
    prompt_feedback = _grounding_value(response, "prompt_feedback", "promptFeedback")
    block_reason = _grounding_value(prompt_feedback, "block_reason", "blockReason")
    return {
        **_gemini_grounding_shape(response),
        "finish_reasons": finish_reasons,
        "prompt_block_reason": (
            getattr(block_reason, "name", str(block_reason)) if block_reason is not None else None
        ),
        "text_length": len(getattr(response, "text", "") or ""),
    }


def _grounding_actual_url(url: Any, grounding_urls: dict[str, str]) -> Optional[str]:
    canonical = _normalize_grounding_url(url)
    return grounding_urls.get(canonical) if canonical else None


def _balanced_json_objects(text: str):
    """Yield top-level balanced {...} substrings, ignoring braces in strings."""
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if depth:
                in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:index + 1]
                start = None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the JSON object from a response that may start with prose findings.

    The prompt asks Gemini for grounded prose followed by JSON, so the object is
    normally the last balanced braces group. A fenced ```json block is preferred
    when the model supplies one.
    """
    raw_text = raw_text.strip()
    fenced = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.extend(reversed(list(_balanced_json_objects(raw_text))))
    greedy = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if greedy:
        candidates.append(greedy.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise json.JSONDecodeError("no JSON object found in provider response", raw_text[:200], 0)


def _gemini_fallback_required(
    data: dict[str, Any] | None,
    grounding_urls: set[str],
    grounding_supports: dict[str, list[str]] | None = None,
    grounding_redirects: dict[str, str] | None = None,
    grounding_available: bool | None = None,
) -> bool:
    """Return True if any non-null Gemini claim lacks grounded, claim-bearing evidence."""
    if not isinstance(data, dict) or not data:
        return True
    if grounding_available is None:
        grounding_available = bool(grounding_supports)
    if not grounding_available:
        # Without provider grounding metadata no claim can be verified. The
        # model's own excerpt/text is never substituted for it.
        return True

    resolved_finals: dict[str, str] = {}
    for chunk, final in (grounding_redirects or {}).items():
        canonical_final = _normalize_grounding_url(final)
        if canonical_final is not None:
            resolved_finals[canonical_final] = chunk

    def cited_url_is_grounded(value: Any) -> bool:
        return bool(_grounding_candidate_chunks(value, grounding_urls, resolved_finals))

    def claim_is_supported(claim: dict[str, Any]) -> bool:
        # Any provider segment that states the claim counts; the best-tier one
        # is what gets stored, so the model's cited page need not be the best.
        return any(
            _claim_matches_excerpt(claim, segment)
            for segments in (grounding_supports or {}).values()
            for segment in segments
            if isinstance(segment, str) and not _text_is_malformed(segment)
        )

    scalar_claims = (
        ("official_website", "official_website_source_url", "official_website_excerpt"),
        ("headquarters", "headquarters_source_url", "headquarters_excerpt"),
        ("company_type", "company_type_source_url", "company_type_excerpt"),
        ("business_category", "business_category_source_url", "business_category_excerpt"),
        ("employee_count", "employee_count_source_url", "employee_count_excerpt"),
        ("revenue", "revenue_source_url", "revenue_excerpt"),
        ("funding", "funding_source_url", "funding_excerpt"),
        ("industry", "industry_source_url", "industry_excerpt"),
        ("description", "description_source_url", "description_excerpt"),
        ("employee_range", "employee_range_source_url", "employee_range_excerpt"),
        ("services", "services_source_url", "services_excerpt"),
        ("hiring_service_fit", "hiring_service_fit_source_url", "hiring_service_fit_excerpt"),
    )
    for field, url_key, _excerpt_key in scalar_claims:
        claim = data.get(field)
        source_url = data.get(url_key)
        if field == "employee_range" and claim is None:
            claim = data.get("employee_count")
        if field == "employee_range" and not source_url:
            source_url = data.get("employee_count_source_url")
        if claim in (None, "", [], {}, "null", "N/A", "n/a", "unknown"):
            continue
        if not cited_url_is_grounded(source_url):
            return True
        if _has_malformed_text(claim):
            return True
        if not claim_is_supported({field: claim}):
            return True

    for item in data.get("classification_evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("supports") is not True and item.get("supports") is not False:
            return True
        if not cited_url_is_grounded(item.get("source_url")):
            return True
        if _has_malformed_text(item.get("fact")) or _has_malformed_text(item.get("why")):
            return True
        if not claim_is_supported({"fact": item.get("fact")}):
            return True

    for item in data.get("research_evidence") or []:
        if not isinstance(item, dict):
            continue
        claim = item.get("claim") or {}
        if not cited_url_is_grounded(item.get("source_url")):
            return True
        if _has_malformed_text(claim):
            return True
        if not claim_is_supported(claim):
            return True

    # Only complete, normalized people are used downstream, so only those can
    # make the response ungrounded. A half-populated entry is dropped, not
    # treated as an unverified claim about the company.
    for person in _normalize_leadership(data.get("leadership")):
        claim = {key: person[key] for key in ("name", "title") if person.get(key)}
        if not cited_url_is_grounded(person.get("source_url")):
            return True
        if _has_malformed_text(claim):
            return True
        if not claim_is_supported(claim):
            return True
    return False


# Tavily's free tier is metered per-minute, so a burst of concurrent
# enrichment calls (ENRICH_CONCURRENCY companies in flight at once) can hit
# its rate limit even when the account has plenty of monthly quota left --
# same shape of transient, retry-worthy failure as Groq's 429 below, not a
# hard exhaustion.
_TAVILY_RATE_LIMIT_RETRIES = 2
_TAVILY_RATE_LIMIT_DEFAULT_WAIT = 5.0


async def _enrich_via_groq_tavily(
    company: Company, tavily_plan_limited: Optional[asyncio.Event] = None,
) -> Optional[str]:
    """DISABLED legacy Tavily/Groq fallback; retained but cannot execute."""
    # Tavily/Groq fallback intentionally disabled. Gemini failures are handled
    # by the queue and returned to durable job state for retry/manual resume.
    now = _utc_now_iso()
    for field, criterion in _FIELD_CRITERIA.items():
        outcome = company.research_outcomes.setdefault(field, {
            "status": "not_searched", "criterion": criterion, "checked_at": now,
        })
        outcome["reason"] = "Tavily/Groq fallback is disabled; Gemini processing is required"
    return "fallback_disabled"

    # --- legacy implementation retained below for reference ---
    if tavily_plan_limited is not None and tavily_plan_limited.is_set():
        now = _utc_now_iso()
        for field, criterion in _FIELD_CRITERIA.items():
            outcome = company.research_outcomes.setdefault(field, {
                "status": "not_searched", "criterion": criterion, "checked_at": now,
            })
            outcome["reason"] = "Tavily key or plan limit was reached earlier in this run"
        return "quota_exhausted"

    groq_key = os.environ.get("GROQ_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not groq_key or not tavily_key:
        print(f"  [WARN] GROQ_API_KEY/TAVILY_API_KEY not set, cannot fall back "
              f"for {company.domain}")
        for field in _FIELD_CRITERIA:
            company.research_outcomes[field]["reason"] = "Gemini unavailable and fallback provider credentials are missing"
        return "quota_exhausted"

    name = company.name or company.domain.split(".")[0]
    targeted_queries = [
        (field, template.format(name=name, domain=company.domain))
        for field, template in _FALLBACK_QUERIES
    ]
    searched_fields: set[str] = set()

    try:
        async with make_ipv4_async_httpx_client() as client:
            results: list[dict[str, Any]] = []
            for field, query in targeted_queries:
                with track("tavily", "tavily_search", domain=company.domain) as u:
                    for attempt in range(_TAVILY_RATE_LIMIT_RETRIES + 1):
                        r = await client.post(_TAVILY_URL, json={
                            "api_key": tavily_key,
                            "query": query,
                            "max_results": 4,
                            "search_depth": "basic",
                            "include_raw_content": "markdown",
                        }, timeout=20.0)
                        if r.status_code == 432:
                            u.mark_failure(
                                status_code=432,
                                error_kind="quota_exhausted",
                                error_details={
                                    "type": "TavilyPlanLimit",
                                    "message": "Tavily returned HTTP 432",
                                    "response": r.text[:4000],
                                    "quota_type": "provider_plan_limit",
                                },
                            )
                            if tavily_plan_limited is not None:
                                tavily_plan_limited.set()
                        if r.status_code in {429, 500, 502, 503, 504} and attempt < _TAVILY_RATE_LIMIT_RETRIES:
                            u.retry_count += 1
                            wait = min(_ENRICH_RETRY_DELAY_CAP_SECONDS,
                                       GroqTavilyProvider._retry_delay(r, attempt))
                            print(f"  [WARN] Tavily rate limited for {company.domain}, waiting "
                                  f"{wait:.0f}s ({attempt + 1}/{_TAVILY_RATE_LIMIT_RETRIES})")
                            await asyncio.sleep(wait)
                            continue
                        if r.status_code in {429, 500, 502, 503, 504}:
                            u.mark_failure(status_code=r.status_code, error_kind="rate_limited" if r.status_code == 429 else "other")
                        r.raise_for_status()
                        u.status_code = r.status_code
                        break
                searched_fields.add(field)
                for result in r.json().get("results", []):
                    result_url = str(result.get("url") or "")
                    existing = next((row for row in results if row.get("url") == result_url and result_url), None)
                    if existing:
                        existing["target_fields"].add(field)
                    else:
                        results.append({**result, "target_fields": {field}, "targeted_query": query})
            unique_results: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for result in results:
                url = str(result.get("url") or "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    unique_results.append(result)
            results = unique_results
            context_results: list[dict[str, Any]] = []
            covered_fields: set[str] = set()
            remaining = list(results)
            while remaining and len(context_results) < 8:
                best = max(
                    remaining,
                    key=lambda result: len(set(result.get("target_fields", set())) - covered_fields),
                )
                remaining.remove(best)
                context_results.append(best)
                covered_fields.update(best.get("target_fields", set()))
            allowed_source_urls = {
                str(result.get("url")) for result in context_results if result.get("url")
            }

            if not results:
                _apply_firmographics(
                    company, {}, source="groq_tavily_search", allowed_source_urls=set(),
                    searched_fields=searched_fields,
                )
                for field in searched_fields:
                    company.research_outcomes[field]["reason"] = "Tavily returned no source URLs for this field"
                return None

            context = "\n\n".join(
                f"[Fields: {', '.join(sorted(res.get('target_fields', set())))}] "
                f"[Query: {res.get('targeted_query', '')}] [{res.get('title', '')}]({res.get('url', '')})\n"
                f"{(res.get('raw_content') or res.get('content') or '')[:900]}"
                for res in context_results
            )
            source_content = {
                str(result["url"]): str(result.get("raw_content") or result.get("content") or "")[:900]
                for result in context_results if result.get("url")
            }
            instruction = _PROMPT_TEMPLATE.format(domain=company.domain, name=name) + (
                f"\n\nUse the following targeted web search results to answer. "
                f"Return structured research_evidence for leadership, services, hiring_service_fit, "
                f"and employee_range when a result supports them. If the results don't "
                f"contain enough information for a field, use null for that field rather than "
                f"guessing.\n\n{context}"
            )

            with track("groq", "groq_parse", domain=company.domain) as u:
                for attempt in range(GroqTavilyProvider._RATE_LIMIT_RETRIES + 1):
                    gr = await client.post(_GROQ_URL, headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                    }, json={
                        "model": _GROQ_MODEL,
                        "messages": [{"role": "user", "content": instruction}],
                        "temperature": 0.1,
                    }, timeout=30.0)
                    if gr.status_code in {429, 500, 502, 503, 504} and attempt < GroqTavilyProvider._RATE_LIMIT_RETRIES:
                        u.retry_count += 1
                        wait = min(_ENRICH_RETRY_DELAY_CAP_SECONDS,
                                   GroqTavilyProvider._retry_delay(gr, attempt))
                        print(f"  [WARN] Groq rate limited for {company.domain}, waiting "
                              f"{wait:.0f}s ({attempt + 1}/{GroqTavilyProvider._RATE_LIMIT_RETRIES})")
                        await asyncio.sleep(wait)
                        continue
                    if gr.status_code in {429, 500, 502, 503, 504}:
                        u.mark_failure(status_code=gr.status_code, error_kind="rate_limited" if gr.status_code == 429 else "other")
                    gr.raise_for_status()
                    u.status_code = gr.status_code
                    body = gr.json()
                    choices = body.get("choices")
                    raw_text = (
                        choices[0].get("message", {}).get("content")
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                        else None
                    )
                    if not isinstance(raw_text, str) or not raw_text.strip():
                        raise json.JSONDecodeError("Groq returned an empty structured response", "", 0)
                    if isinstance(body.get("usage"), dict):
                        usage = body["usage"]
                        u.set_tokens(
                            prompt=usage.get("prompt_tokens"),
                            completion=usage.get("completion_tokens"),
                            total=usage.get("total_tokens"),
                        )
                    break

        data = _extract_json_object(raw_text)
        _apply_firmographics(
            company, data, source="groq_tavily_search", allowed_source_urls=allowed_source_urls,
            searched_fields=searched_fields, source_content=source_content,
        )
        return None

    except json.JSONDecodeError as e:
        print(f"  [WARN] Could not parse Groq fallback response for {company.domain}: {e}")
        _apply_firmographics(
            company, {}, source="groq_tavily_search", allowed_source_urls=set(),
            searched_fields=searched_fields,
        )
        for field in searched_fields:
            company.research_outcomes[field]["reason"] = "provider response could not be parsed"
        return "parse_error"
    except Exception as e:
        from ..usage_tracker import classify_error
        reason = classify_error(e)
        kind = {
            "connection_error": "connection error (network/TLS path to Tavily or Groq)",
            "timeout": "request to Tavily or Groq timed out",
            "rate_limited": "rate limited (429) with retries exhausted",
        }.get(reason, "request error")
        print(f"  [WARN] Groq/Tavily fallback enrichment failed for {company.domain} "
              f"({kind}): {type(e).__name__}: {e}")
        if searched_fields:
            _apply_firmographics(
                company, {}, source="groq_tavily_search", allowed_source_urls=set(),
                searched_fields=searched_fields,
            )
        for field in _FIELD_CRITERIA:
            outcome = company.research_outcomes.setdefault(field, {
                "status": "not_searched", "criterion": _FIELD_CRITERIA[field],
                "checked_at": _utc_now_iso(),
            })
            if field not in searched_fields:
                outcome["reason"] = f"search did not complete: {reason}"
            else:
                outcome["reason"] = f"search completed but structured extraction failed: {reason}"
        return reason if reason in ("connection_error", "timeout", "rate_limited", "quota_exhausted") else "request_error"


async def enrich_company_firmographics(
    company: Company,
    run_id: Optional[str] = None,
) -> Optional[str]:
    """Enrich a company with real firmographic data via Gemini API.

    Gemini is the sole external AI/research provider. Its adaptive queue and
    retry/backoff handling classify failures by RPM, TPM, daily quota, monthly
    quota, or temporary capacity. Quota-blocked jobs remain persistent and can
    be resumed manually. Tavily/Groq fallback code is retained but disabled.
    Returns a short failure-reason string or None on success.
    """
    _mark_research_not_searched(company, "provider call has not returned usable research results")
    pool = GeminiKeyPool.from_env()

    if not pool:
        return "no_api_key"

    prompt = _PROMPT_TEMPLATE.format(
        domain=company.domain,
        name=company.name or company.domain.split(".")[0],
    )

    try:
        from .gemini_keys import make_gemini_client

        def _call_with_connection_retry(key: str):
            with track("gemini", "enrich_firmographics", run_id=run_id, domain=company.domain) as u:
                connection_retries = 0
                while True:
                    try:
                        client = make_gemini_client(key)
                        response = generate_content(
                            lambda: client.models.generate_content(
                                model=_GEMINI_MODEL,
                                contents=prompt,
                                config={"temperature": 0.1, "tools": [{"google_search": {}}]},
                            ),
                            call_kind="enrich_firmographics",
                            run_id=run_id,
                            domain=company.domain,
                        )
                        meta = getattr(response, "usage_metadata", None)
                        if meta is not None:
                            u.set_tokens(
                                prompt=getattr(meta, "prompt_token_count", None),
                                completion=getattr(meta, "candidates_token_count", None),
                                total=getattr(meta, "total_token_count", None),
                            )
                        return response
                    except Exception as exc:
                        if is_connection_error(exc) and connection_retries < _CONNECTION_ERROR_RETRIES:
                            u.retry_count += 1
                            print(f"  [WARN] transient connection error reaching Gemini for "
                                  f"{company.domain} ({type(exc).__name__}), retrying once")
                            connection_retries += 1
                            _time.sleep(_CONNECTION_ERROR_RETRY_DELAY)
                            continue
                        raise

        response = None
        last_exc: Exception | None = None
        for attempt in pool.attempts():
            try:
                response = await asyncio.to_thread(_call_with_connection_retry, attempt.key)
                break
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, (GeminiQuotaBlocked, GeminiRetryLater)):
                    raise
                if pool.should_advance(exc):
                    pool.mark_exhausted(attempt.key)
                    continue
                # A non-quota failure (network blip that outlasted the retry
                # above, malformed/empty response from the grounded-search
                # tool, transient 5xx, safety-filter block, ...) is not
                # specific to this one key -- trying the next configured key
                # would just repeat the same failure, so stop the key loop
                # and return a resumable provider failure.
                break
        if response is None:
            from ..usage_tracker import classify_error
            classified = classify_error(last_exc) if last_exc is not None else "other"
            reason = {
                "quota_exhausted": "all Gemini keys exhausted",
                "connection_error": "Gemini unreachable (connection/TLS error, not a quota or request issue)",
                "timeout": "Gemini request timed out",
                "rate_limited": "Gemini rate limit retries exhausted",
            }.get(classified, "Gemini request failed")
            print(f"  [WARN] {reason} for {company.domain}: {last_exc}")
            return classified if classified in {
                "quota_exhausted", "connection_error", "timeout", "rate_limited",
            } else "request_error"

        raw_text = (getattr(response, "text", "") or "").strip()
        if not raw_text:
            diagnostics = _gemini_response_diagnostics(response)
            reason = "Gemini returned no text content"
            missing_fields = {}
            for field, criterion in _FIELD_CRITERIA.items():
                company.research_outcomes[field] = {
                    "status": "not_returned", "criterion": criterion,
                    "checked_at": _utc_now_iso(), "reason": reason,
                }
                missing_fields[field] = reason
            company.research_outcomes["_summary"] = {
                "status": "research_incomplete", "missing_fields": {},
                "blocking_fields": {}, "review_fields": {},
            }
            log_event(
                "gemini_empty_response", domain=company.domain, run_id=run_id,
                reason=reason, **diagnostics,
            )
            print(f"  [WARN] Gemini returned no usable research for {company.domain}")
            return None

        data = _extract_json_object(raw_text)
        grounding_started = _time.monotonic()
        grounding_map = _extract_gemini_grounding_urls(response)
        grounding_urls = set(grounding_map)
        grounding_supports = _extract_gemini_grounding_supports(response)
        # Google's grounding metadata is the only accepted evidence. The
        # model's prose (a FINDINGS line containing a redirect URL) is
        # generated text and cannot establish which source supports which
        # claim; when the metadata is absent, nothing in this response can be
        # verified and every non-null claim is recorded as needing review.
        grounding_available = bool(grounding_supports)
        grounding_source = "grounding_metadata" if grounding_available else "unavailable"
        if not grounding_available:
            log_event(
                "gemini_grounding_unavailable", domain=company.domain, run_id=run_id,
                grounding_url_count=len(grounding_urls),
                reason="grounding metadata or support segments missing",
            )
        # Google's chunk URLs are opaque redirects; resolve them so a claim that
        # cites the real page URL can be tied to the chunk that grounded it and
        # so stored provenance is a human-checkable URL.
        # Off the event loop: redirect resolution is blocking I/O and must not
        # stall other companies' enrichment or the job's progress reporting.
        grounding_redirects = await asyncio.to_thread(
            _resolve_grounding_urls, grounding_urls,
        )
        grounding_redirects = await asyncio.to_thread(
            _resolve_cited_redirects, data, grounding_redirects,
        )
        grounding_shape = _gemini_grounding_shape(response)
        log_event(
            "gemini_response_processing",
            domain=company.domain,
            run_id=run_id,
            grounding_url_count=len(grounding_urls),
            grounding_resolved_count=len(grounding_redirects),
            grounding_source=grounding_source,
            grounding_available=grounding_available,
            **grounding_shape,
            processing_ms=int((_time.monotonic() - grounding_started) * 1000),
        )

        grounding_invalid = _gemini_fallback_required(
            data, grounding_urls, grounding_supports, grounding_redirects,
            grounding_available=grounding_available,
        )
        if grounding_invalid:
            print(f"  [WARN] Gemini response for {company.domain} contained a rejected claim")
        _apply_firmographics(
            company, data, source="gemini_grounded_search", allowed_source_urls=grounding_urls,
            strict_response=True, grounding_supports=grounding_supports,
            grounding_redirects=grounding_redirects,
            grounding_available=grounding_available,
        )
        missing_response_fields = sorted(_REQUIRED_RESPONSE_KEYS - data.keys())
        company.research_outcomes["_missing_response_fields"] = {
            "missing": missing_response_fields,
        }
        completeness = enrichment_completeness(company)
        accepted_fields = sorted(
            field for field in _FIELD_CRITERIA
            if company.research_outcomes.get(field, {}).get("status") == "accepted_and_mapped"
        )
        rejected_fields = sorted(
            field for field in _FIELD_CRITERIA
            if company.research_outcomes.get(field, {}).get("status") == "found_but_rejected"
        )
        missing_fields = sorted(
            field for field in _FIELD_CRITERIA
            if company.research_outcomes.get(field, {}).get("status") in {
                "not_returned", "searched_but_not_found", "not_searched",
            }
        )
        field_diagnostics = {
            field: {
                "status": (
                    "accepted" if outcome.get("status") == "accepted_and_mapped" else
                    "rejected" if outcome.get("status") == "found_but_rejected" else "missing"
                ),
                "reason": outcome.get("reason"),
            }
            for field in _FIELD_CRITERIA
            if isinstance((outcome := company.research_outcomes.get(field)), dict)
        }
        if grounding_invalid:
            completeness["status"] = "needs_review"
            completeness.setdefault("review_fields", {})["grounding"] = (
                "One or more claims did not meet the grounding requirements"
            )
        company.research_outcomes["_summary"] = completeness
        log_event(
            "gemini_enrichment_outcomes", domain=company.domain, run_id=run_id,
            status=completeness["status"], grounding_rejected=grounding_invalid,
            accepted_fields=accepted_fields, rejected_fields=rejected_fields,
            missing_fields=missing_fields, field_outcomes=field_diagnostics,
            grounding_url_count=len(grounding_urls),
            grounding_available=grounding_available, **grounding_shape,
        )
        if grounding_invalid:
            log_event(
                "gemini_grounding_rejected", domain=company.domain, run_id=run_id,
                grounding_url_count=len(grounding_urls),
                grounding_available=grounding_available, **grounding_shape,
                rejected_fields=rejected_fields,
                reason=(
                    "grounding_metadata_unavailable" if not grounding_available
                    else "missing_or_unverified_grounding"
                ),
            )
        if completeness["status"] == "complete_with_gaps":
            print(f"  [OK] Enriched {company.domain} with documented gaps")
        elif completeness["status"] == "needs_review":
            print(f"  [OK] Researched {company.domain}; some fields need review")
        elif completeness["status"] == "partial":
            print(f"  [OK] Partially researched {company.domain}")
        else:
            print(f"  [OK] Enriched {company.domain}")
        return None

    except json.JSONDecodeError as e:
        print(f"  [WARN] Could not parse Gemini response for {company.domain}: {e}")
        return "parse_error"
    except (GeminiQuotaBlocked, GeminiRetryLater):
        raise
    except Exception as e:
        # Reachable only for a failure outside the key-attempt loop itself
        # (e.g. _extract_json_object/_apply_firmographics raising on a
        # response Gemini did return) -- the Gemini call path's own failures
        # are all handled above and never reach here.
        from ..usage_tracker import classify_error
        reason = classify_error(e)
        print(f"  [WARN] Enrichment failed for {company.domain} ({reason}): {type(e).__name__}: {e}")
        return reason if reason in ("connection_error", "timeout", "rate_limited", "quota_exhausted") else "request_error"
