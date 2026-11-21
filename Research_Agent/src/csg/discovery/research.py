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
import time as _time
import unicodedata
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .gemini_keys import (
    GeminiKeyPool, is_connection_error, is_daily_quota_exhausted,
    make_ipv4_async_httpx_client,
)
from ..gemini_queue import GeminiQuotaBlocked, GeminiRetryLater, generate_content
from ..models import Company, ProvenanceValue, provenance_value
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

_FIELD_CRITERIA = {
    "official_website": "automatic_website",
    "headquarters": "automatic_headquarters",
    "employee_range": "automatic_company_size",
    "company_classification": "automatic_company_type",
    "leadership": "company_leadership",
    "services": "company_services",
    "hiring_service_fit": "company_hiring_fit",
    "revenue": "company_revenue",
    "funding": "company_funding",
    "industry": "company_industry",
    "description": "company_description",
}
_REQUIRED_RESPONSE_FIELDS = (
    "official_website", "headquarters", "company_type", "employee_count",
    "revenue", "funding", "industry", "description", "employee_range",
    "services", "leadership", "hiring_service_fit", "classification_evidence",
    "research_evidence",
)
_REQUIRED_RESPONSE_KEYS = frozenset({
    *_REQUIRED_RESPONSE_FIELDS,
    "official_website_source_url", "official_website_excerpt",
    "headquarters_source_url", "headquarters_excerpt",
    "company_type_source_url", "company_type_excerpt",
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
    return " ".join(normalized.split()).casefold()


def _has_source_excerpt(source_url: str, excerpt: Any, source_content: dict[str, str]) -> bool:
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    content = source_content.get(source_url, "")
    return bool(content) and _normalized_source_text(excerpt) in _normalized_source_text(content)


def _claim_matches_excerpt(claim: dict[str, Any], excerpt: str) -> bool:
    normalized_excerpt = _normalized_source_text(excerpt)
    aliases = {
        "public company": ("public company", "publicly traded company"),
        "private company": ("private company", "privately held company"),
        "smb": ("smb", "small business", "small-to-medium business"),
        "system_integrator": ("system integrator", "systems integrator", "systems integration provider"),
    }
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)
        elif isinstance(value, (str, int, float)) and str(value).strip():
            values.append(str(value).strip())

    collect(claim)
    def supported(value: str) -> bool:
        normalized = _normalized_source_text(value)
        alternatives = aliases.get(normalized, (value,))
        return any(_normalized_source_text(item) in normalized_excerpt for item in alternatives)

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
    """Summarize terminal field outcomes without treating unavailable facts as successes."""
    missing: dict[str, str] = {}
    blocking: dict[str, str] = {}
    response_missing = company.research_outcomes.get("_missing_response_fields") or []
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
        if status == "accepted_and_mapped":
            continue
        reason = outcome.get("reason")
        if status == "searched_but_not_found":
            missing[field] = reason or "No reliable information was returned"
            continue
        blocking[field] = reason or {
            "not_returned": "The provider omitted this required field",
            "found_but_rejected": "The returned claim failed validation",
            "not_searched": "The field was not searched",
        }.get(status, f"Research outcome is incomplete ({status or 'missing status'})")
        missing[field] = blocking[field]
    status = "partial" if blocking else ("complete_with_gaps" if missing else "complete")
    return {"status": status, "missing_fields": missing, "blocking_fields": blocking}


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


def _parse_revenue(text: str) -> Optional[str]:
    """Normalize revenue text like '$100M', '$1.2 billion', etc."""
    if not text or text.lower() in ("unknown", "n/a", "not available", "private", "not publicly available"):
        return None
    return text.strip()


_PROMPT_TEMPLATE = """You are a business research analyst. Research the company with domain "{domain}" (name: {name}) using grounded search. Return one JSON object with the following fields:

Research this company and return ONLY a valid JSON object with these fields:
{{
    "official_website": "https://company.example or null",
        "official_website_source_url": "URL supporting the official website or null",
        "official_website_excerpt": "exact supporting excerpt from that source or null",
    "headquarters": {{"city": "...", "state": "...", "country": "..."}} or null,
        "headquarters_source_url": "URL supporting headquarters or null",
        "headquarters_excerpt": "exact supporting excerpt from that source or null",
    "company_type": "Startup, SMB, System Integrator, Public Company, Private Company, or another explicit type | null",
        "company_type_source_url": "URL supporting company_type or null",
        "company_type_excerpt": "exact supporting excerpt from that source or null",
  "employee_count": <number or null>,
    "employee_count_source_url": "URL supporting employee_count or null",
        "employee_count_excerpt": "exact supporting excerpt from that source or null",
  "revenue": "<string like '$50M' or '$1.2B' or null>",
    "revenue_source_url": "URL supporting revenue or null",
        "revenue_excerpt": "exact supporting excerpt from that source or null",
  "funding": "<string like '$100M Series C' or null>",
    "funding_source_url": "URL supporting funding or null",
        "funding_excerpt": "exact supporting excerpt from that source or null",
  "industry": "<string like 'Software / Marketing Automation' or null>",
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
        {{"field": "leadership|services|hiring_service_fit|employee_range|industry|description", "criterion": "company_leadership|company_services|company_hiring_fit|automatic_company_size|company_industry|company_description", "claim": {{}}, "source_name": "source title", "source_url": "exact URL", "excerpt": "exact supporting excerpt", "confidence": "high|medium|low", "evidence_type": "direct|inferred"}}
    ],
    "classification_evidence": [
        {{"category": "startup|smb|system_integrator", "supports": true, "fact": "short exact fact", "why": "why this supports or contradicts the category", "source_name": "source title", "source_url": "exact supporting URL", "excerpt": "exact supporting excerpt", "confidence": "high|medium|low", "evidence_type": "direct|inferred"}}
    ]
}}

Important rules:
- Return ONLY the JSON object, no other text.
- Use null for any field you cannot determine with reasonable confidence.
- For revenue, if the company is private and revenue is not public, return null.
- Employee count should be the approximate current number.
- Identify headquarters from the company's official About/Contact page, LinkedIn,
    Crunchbase, or another reputable company profile. Never infer headquarters from
    job-posting locations.
- Do not call a company a startup merely because it raised funding, and do not
    classify it from estimated employee count alone. Use explicit company identity
    language from a reliable source when available.
- Be specific about industry - include sub-category (e.g., "Software / DevOps" not just "Technology").
- Source URLs must be exact URLs from the provided search results; use null when no result supports a field.
- Research these independently: official website, headquarters, current employee count/range, explicit company classification, named leadership, services, and documented hiring/staffing signals.
- For leadership, include only names and titles explicitly present in a source. Do not claim a person is a validated decision-maker; that requires the existing contact validation flow.
- Keep company services separate from hiring/service-fit. Do not claim staffing openness or CSG service fit from a general services description.
- Every research_evidence item must quote a concise supporting excerpt and use a URL present in grounding results. Do not create evidence for unsupported fields.
- Every non-null fact must include its matching source URL and exact supporting excerpt. The excerpt must be copied verbatim from the supplied source content; do not paraphrase it.
- Run targeted research for each category: founding date/funding stage and explicit startup language for startup; reliable employee-count/company-size facts for SMB; and explicit evidence that the company provides systems integration, technology implementation, IT consulting, or integration services to clients for system integrator.
- Return classification_evidence only when the extracted fact supports or reasonably contradicts that category. Never use a generic company URL as evidence and never infer SMB from an unsupported estimate.
"""


def _apply_firmographics(
    company: Company, data: dict[str, Any], source: str, allowed_source_urls: set[str] | None = None,
    searched_fields: set[str] | None = None, source_content: dict[str, str] | None = None,
    strict_response: bool = False, grounding_supports: dict[str, list[str]] | None = None,
) -> None:
    """Apply supported facts and retain a field-level account of each result."""
    now = _utc_now_iso()
    allowed_source_urls = {
        canonical
        for url in (allowed_source_urls or set())
        if (canonical := _normalize_grounding_url(url)) is not None
    }
    source_content = source_content or {}
    searched_fields = set(_FIELD_CRITERIA) if searched_fields is None else searched_fields
    outcomes = {
        field: {"status": "not_searched", "criterion": criterion, "checked_at": now}
        for field, criterion in _FIELD_CRITERIA.items()
    }
    accepted_fields: set[str] = set()
    rejection_reasons: dict[str, str] = {}

    for outcome_field, company_field in (
        ("official_website", "website"),
        ("headquarters", "headquarters"),
        ("company_classification", "company_type"),
        ("employee_range", "employee_count"),
        ("revenue", "revenue"),
        ("funding", "funding"),
    ):
        prior = getattr(company, company_field)
        if prior is not None and _normalize_grounding_url(getattr(prior, "url", None)):
            accepted_fields.add(outcome_field)
    for item in company.research_evidence:
        if isinstance(item, dict) and item.get("field") in _FIELD_CRITERIA:
            accepted_fields.add(str(item["field"]))
    for outcome_field, value in (("industry", company.industry), ("description", company.description)):
        if value:
            accepted_fields.add(outcome_field)

    def fact_source_url(field: str) -> str | None:
        candidate = data.get(f"{field}_source_url")
        if field == "employee_count" and candidate is None:
            candidate = data.get("employee_range_source_url")
        if not isinstance(candidate, str):
            return None
        canonical_candidate = _normalize_grounding_url(candidate)
        if canonical_candidate is None or canonical_candidate not in allowed_source_urls:
            return None
        try:
            parsed_url = httpx.URL(candidate)
            return candidate if parsed_url.scheme in {"http", "https"} and parsed_url.host else None
        except Exception:
            return None

    def fact_source_is_valid(field: str, source_url: str) -> bool:
        if source not in {"gemini_grounded_search", "groq_tavily_search"}:
            return True
        excerpt = data.get(f"{field}_excerpt")
        claim = data.get(field)
        if field == "employee_count" and claim is None:
            claim = data.get("employee_range")
        if not _claim_matches_excerpt({field: claim}, str(excerpt or "")):
            return False
        if source == "gemini_grounded_search":
            canonical = _normalize_grounding_url(source_url)
            segments = (grounding_supports or {}).get(canonical or "", [])
            if not any(_claim_matches_excerpt({field: claim}, segment) for segment in segments):
                return False
        if source == "groq_tavily_search":
            return _has_source_excerpt(source_url, excerpt, source_content)
        return True

    def save_fact(field: str, value: Any, source_field: str, outcome_field: str, *, method: str = "reported",
                  confidence: str = "high") -> bool:
        source_url = fact_source_url(source_field)
        if value is None:
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
            rejection_reasons[outcome_field] = "supporting excerpt does not contain the claim or is not in cited source content"
            return False
        existing = getattr(company, field)
        if existing is not None:
            accepted_fields.add(outcome_field)
            return True
        setattr(company, field, ProvenanceValue(
            value=value, source=source, url=source_url,
            fetched_at=now, method=method, confidence=confidence,
        ))
        accepted_fields.add(outcome_field)
        return True

    official_website = data.get("official_website")
    if isinstance(official_website, str) and official_website.startswith(("http://", "https://")):
        parsed = httpx.URL(official_website)
        domain = company.domain.lower().removeprefix("www.")
        host = (parsed.host or "").lower().removeprefix("www.")
        if host == domain or host.endswith(f".{domain}"):
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
            if not isinstance(source_url, str) or not source_url.strip():
                rejection_reasons["company_classification"] = "classification evidence source URL is missing"
                continue
            if not fact or not why:
                rejection_reasons["company_classification"] = "classification evidence URL missing or not present in provider results"
                continue
            try:
                parsed_url = httpx.URL(source_url)
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
                    raise ValueError("invalid source URL")
            except Exception:
                rejection_reasons["company_classification"] = "classification evidence URL is not a valid HTTP(S) URL"
                continue
            canonical_source_url = _normalize_grounding_url(source_url)
            allowed_canonical = {
                normalized for url in allowed_source_urls
                if (normalized := _normalize_grounding_url(url)) is not None
            }
            if canonical_source_url is None or canonical_source_url not in allowed_canonical:
                rejection_reasons["company_classification"] = "classification evidence URL not present in provider results"
                continue
            excerpt = item.get("excerpt")
            if source == "groq_tavily_search" and not _has_source_excerpt(
                source_url, excerpt, source_content,
            ):
                rejection_reasons["company_classification"] = "classification excerpt missing or not found in cited source content"
                continue
            if not _claim_matches_excerpt({"fact": fact}, str(excerpt or "")):
                rejection_reasons["company_classification"] = "classification claim is not represented in its supporting excerpt"
                continue
            if source == "gemini_grounded_search":
                canonical = _normalize_grounding_url(source_url)
                segments = (grounding_supports or {}).get(canonical or "", [])
                if not any(_claim_matches_excerpt({"fact": fact}, segment) for segment in segments):
                    rejection_reasons["company_classification"] = "grounding support does not associate the claim with its cited source"
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
                "why": why, "source_name": str(item.get("source_name") or source_url),
                "source_url": source_url, "excerpt": str(excerpt or ""), "confidence": confidence,
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
    revenue_url = fact_source_url("revenue")
    if rev and company.revenue is not None:
        accepted_fields.add("revenue")
    elif rev and revenue_url and fact_source_is_valid("revenue", revenue_url):
        company.revenue = ProvenanceValue(
            value=rev, source=source, url=fact_source_url("revenue"),
            fetched_at=now, method="estimated",
            confidence="medium" if "$" in rev else "low",
        )
        accepted_fields.add("revenue")
    elif rev and not revenue_url:
        rejection_reasons["revenue"] = "supporting URL missing or not present in provider results"
    elif rev and revenue_url and not fact_source_is_valid("revenue", revenue_url):
        rejection_reasons["revenue"] = "supporting excerpt does not contain the claim or is not in cited source content"

    # --- Funding ---
    funding = data.get("funding")
    if funding and str(funding).lower() not in ("null", "none", "n/a", "unknown"):
        funding_url = fact_source_url("funding")
        if company.funding is not None:
            accepted_fields.add("funding")
        elif funding_url and fact_source_is_valid("funding", funding_url):
            company.funding = ProvenanceValue(
                value=str(funding), source=source, url=funding_url,
                fetched_at=now, confidence="medium",
            )
            accepted_fields.add("funding")
        elif not funding_url:
            rejection_reasons["funding"] = "supporting URL missing or not present in provider results"
        elif not fact_source_is_valid("funding", funding_url):
            rejection_reasons["funding"] = "supporting excerpt does not contain the claim or is not in cited source content"

    accepted_research: list[dict[str, Any]] = []
    research_evidence = list(data.get("research_evidence") or []) if isinstance(data.get("research_evidence"), list) else []
    field_sources = {
        "employee_range": (data.get("employee_range") or data.get("employee_count"),
                           data.get("employee_range_source_url") or data.get("employee_count_source_url")),
        "services": (data.get("services"), data.get("services_source_url")),
        "hiring_service_fit": (data.get("hiring_service_fit"), data.get("hiring_service_fit_source_url")),
        "industry": (data.get("industry"), data.get("industry_source_url")),
        "description": (data.get("description"), data.get("description_source_url")),
    }
    for field, (value, source_url) in field_sources.items():
        if value in (None, "", [], {}) or any(
            isinstance(item, dict) and item.get("field") == field for item in research_evidence
        ):
            continue
        research_evidence.append({
            "field": field, "criterion": _FIELD_CRITERIA[field],
            "claim": {field: value}, "source_name": str(source_url or ""),
            "source_url": source_url, "excerpt": data.get(f"{field}_excerpt", ""),
            "evidence_type": "inferred", "confidence": "medium",
        })
    leadership = data.get("leadership")
    if isinstance(leadership, list) and not any(
        isinstance(item, dict) and item.get("field") == "leadership" for item in research_evidence
    ):
        for person in leadership:
            if not isinstance(person, dict):
                continue
            claim = {key: person[key] for key in ("name", "title") if person.get(key)}
            if not claim:
                continue
            research_evidence.append({
                "field": "leadership", "criterion": _FIELD_CRITERIA["leadership"],
                "claim": claim, "source_name": str(person.get("source_name") or person.get("source_url") or ""),
                "source_url": person.get("source_url"),
                "excerpt": str(person.get("excerpt") or "").strip(),
                "evidence_type": "direct", "confidence": "high",
            })
    claims_by_field: dict[str, set[str]] = {}
    for field, (value, _source_url) in field_sources.items():
        if value not in (None, "", [], {}):
            claims_by_field.setdefault(field, set()).add(json.dumps({field: value}, sort_keys=True, default=str))
    for item in research_evidence:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        claim = item.get("claim")
        if field in {"employee_range", "leadership", "services", "hiring_service_fit", "industry", "description"} and isinstance(claim, dict):
            claims_by_field.setdefault(field, set()).add(json.dumps(claim, sort_keys=True, default=str))
    if isinstance(leadership, list):
        for person in leadership:
            if not isinstance(person, dict):
                continue
            claim = {key: person[key] for key in ("name", "title") if person.get(key)}
            if claim:
                claims_by_field.setdefault("leadership", set()).add(json.dumps(claim, sort_keys=True, default=str))
    conflicting_fields = {field for field, claims in claims_by_field.items() if len(claims) > 1}
    for field in conflicting_fields:
        rejection_reasons[field] = "conflicting claims supplied for the same field"

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
            if field not in {"employee_range", "leadership", "services", "hiring_service_fit", "industry", "description"}:
                continue
            if field in conflicting_fields:
                continue
            if item_criterion != criterion:
                rejection_reasons[field] = "evidence criterion does not match the field mapping"
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
            if _normalize_grounding_url(source_url) not in {
                normalized for url in allowed_source_urls
                if (normalized := _normalize_grounding_url(url)) is not None
            }:
                rejection_reasons[field] = "source URL not present in provider results"
                continue
            if not isinstance(claim, dict) or not claim or not excerpt:
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
            if source == "gemini_grounded_search":
                canonical = _normalize_grounding_url(source_url)
                segments = (grounding_supports or {}).get(canonical or "", [])
                if not any(_claim_matches_excerpt(claim, segment) for segment in segments):
                    rejection_reasons[field] = "grounding support does not associate the claim with its cited source"
                    continue
            evidence_type = str(item.get("evidence_type") or "inferred").lower()
            if evidence_type not in {"direct", "inferred"}:
                evidence_type = "inferred"
            confidence = str(item.get("confidence") or "medium").lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            key_material = json.dumps([field, item_criterion, claim, source_url, excerpt], sort_keys=True)
            evidence_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
            accepted_research.append({
                "evidence_key": evidence_key, "field": field, "criterion": criterion,
                "claim": claim, "source_name": str(item.get("source_name") or source_url),
                "source_url": source_url, "excerpt": excerpt,
                "evidence_type": evidence_type, "confidence": confidence, "retrieved_at": now,
            })
            accepted_fields.add(field)
        for item in research_evidence:
            if isinstance(item, dict) and item.get("field") in _FIELD_CRITERIA:
                field = str(item["field"])
                if field not in accepted_fields:
                    rejection_reasons.setdefault(field, "evidence failed source, excerpt, or criterion validation")

    previous_evidence = {item.get("evidence_key"): item for item in company.research_evidence
                         if isinstance(item, dict) and item.get("evidence_key")}
    for item in accepted_research:
        previous_evidence.setdefault(item["evidence_key"], item)
    company.research_evidence = list(previous_evidence.values())
    for field in conflicting_fields:
        accepted_fields.discard(field)

    # --- Industry ---
    industry = data.get("industry")
    industry_url = fact_source_url("industry")
    if industry and str(industry).lower() not in ("null", "none", "n/a") and not industry_url:
        rejection_reasons["industry"] = "supporting URL missing or not present in provider results"
    elif industry and industry_url and not fact_source_is_valid("industry", industry_url):
        rejection_reasons["industry"] = "supporting excerpt does not contain the claim or is not in cited source content"
    elif industry and industry_url:
        if not company.industry:
            company.industry = str(industry)
        accepted_fields.add("industry")

    # --- Description ---
    desc = data.get("description")
    description_url = fact_source_url("description")
    if desc and str(desc).lower() not in ("null", "none") and not description_url:
        rejection_reasons["description"] = "supporting URL missing or not present in provider results"
    elif desc and description_url and not fact_source_is_valid("description", description_url):
        rejection_reasons["description"] = "supporting excerpt does not contain the claim or is not in cited source content"
    elif desc and description_url:
        if not company.description:
            company.description = str(desc)
        accepted_fields.add("description")

    # Keep the legacy display field populated from the researched HQ envelope.
    if company.headquarters:
        hq = company.headquarters.get("value") if isinstance(company.headquarters, dict) else company.headquarters.value
        if isinstance(hq, dict) and hq.get("state"):
            company.hq_state = str(hq["state"])

    value_fields = {
        "official_website": data.get("official_website"),
        "headquarters": data.get("headquarters"),
        "employee_range": data.get("employee_range") or data.get("employee_count"),
        "company_classification": data.get("company_type") or data.get("classification_evidence"),
        "leadership": data.get("leadership"),
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
        status = "accepted_and_mapped" if field in accepted_fields else (
            "found_but_rejected" if present or field in rejection_reasons else "searched_but_not_found"
        )
        outcome = {"status": status, "criterion": _FIELD_CRITERIA[field], "checked_at": now}
        if field in rejection_reasons:
            outcome["reason"] = rejection_reasons[field]
        elif status == "searched_but_not_found":
            outcome["reason"] = "Gemini explicitly returned no reliable information for this field"
        evidence_ids = [item["evidence_key"] for item in accepted_research if item["field"] == field]
        if evidence_ids:
            outcome["evidence_keys"] = evidence_ids
        outcomes[field] = outcome
    company.research_outcomes.update(outcomes)

    print(f"  [OK] Enriched {company.domain}: "
            f"emp={provenance_value(company.employee_count) or '?'}, "
            f"rev={provenance_value(company.revenue) or '?'}, "
          f"industry={company.industry or '?'}")


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


def _grounding_actual_url(url: Any, grounding_urls: dict[str, str]) -> Optional[str]:
    canonical = _normalize_grounding_url(url)
    return grounding_urls.get(canonical) if canonical else None


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    raw_text = raw_text.strip()
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
    if json_match:
        raw_text = json_match.group(1).strip()
    obj_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if obj_match:
        raw_text = obj_match.group(0)
    return json.loads(raw_text)


def _gemini_fallback_required(
    data: dict[str, Any] | None,
    grounding_urls: set[str],
    grounding_supports: dict[str, list[str]] | None = None,
) -> bool:
    """Return True if any non-null Gemini claim lacks grounded, claim-bearing evidence."""
    if not isinstance(data, dict) or not data:
        return True

    def cited_url_is_grounded(value: Any) -> bool:
        canonical = _normalize_grounding_url(value)
        return canonical is not None and canonical in grounding_urls

    def claim_is_supported(url: Any, claim: dict[str, Any]) -> bool:
        canonical = _normalize_grounding_url(url)
        if canonical is None:
            return False
        segments = (grounding_supports or {}).get(canonical, [])
        return any(_claim_matches_excerpt(claim, segment) for segment in segments)

    scalar_claims = (
        ("official_website", "official_website_source_url", "official_website_excerpt"),
        ("headquarters", "headquarters_source_url", "headquarters_excerpt"),
        ("company_type", "company_type_source_url", "company_type_excerpt"),
        ("employee_count", "employee_count_source_url", "employee_count_excerpt"),
        ("revenue", "revenue_source_url", "revenue_excerpt"),
        ("funding", "funding_source_url", "funding_excerpt"),
        ("industry", "industry_source_url", "industry_excerpt"),
        ("description", "description_source_url", "description_excerpt"),
        ("employee_range", "employee_range_source_url", "employee_range_excerpt"),
        ("services", "services_source_url", "services_excerpt"),
        ("hiring_service_fit", "hiring_service_fit_source_url", "hiring_service_fit_excerpt"),
    )
    for field, url_key, excerpt_key in scalar_claims:
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
        if not _claim_matches_excerpt({field: claim}, str(data.get(excerpt_key) or "")):
            return True
        if not claim_is_supported(source_url, {field: claim}):
            return True

    for item in data.get("classification_evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("supports") is not True and item.get("supports") is not False:
            return True
        if not cited_url_is_grounded(item.get("source_url")) or not _claim_matches_excerpt(
            {"fact": item.get("fact")},
            str(item.get("excerpt") or ""),
        ):
            return True
        if not claim_is_supported(item.get("source_url"), {"fact": item.get("fact")}):
            return True

    for item in data.get("research_evidence") or []:
        if not isinstance(item, dict):
            continue
        if (not cited_url_is_grounded(item.get("source_url"))
            or not _claim_matches_excerpt(item.get("claim") or {}, str(item.get("excerpt") or ""))):
            return True
        if not claim_is_supported(item.get("source_url"), item.get("claim") or {}):
            return True

    leadership = data.get("leadership")
    if isinstance(leadership, list):
        for person in leadership:
            if not isinstance(person, dict):
                continue
            if person.get("name") or person.get("title"):
                if (not cited_url_is_grounded(person.get("source_url"))
                        or not _claim_matches_excerpt(
                            {key: person.get(key) for key in ("name", "title") if person.get(key)},
                            str(person.get("excerpt") or ""),
                        )):
                    return True
                if not claim_is_supported(
                    person.get("source_url"),
                    {key: person.get(key) for key in ("name", "title") if person.get(key)},
                ):
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
            return "empty_response"

        data = _extract_json_object(raw_text)
        grounding_started = _time.monotonic()
        grounding_map = _extract_gemini_grounding_urls(response)
        grounding_urls = set(grounding_map)
        grounding_supports = _extract_gemini_grounding_supports(response)
        log_event(
            "gemini_response_processing",
            domain=company.domain,
            run_id=run_id,
            grounding_url_count=len(grounding_urls),
            processing_ms=int((_time.monotonic() - grounding_started) * 1000),
        )

        grounding_invalid = _gemini_fallback_required(data, grounding_urls, grounding_supports)
        if grounding_invalid:
            print(f"  [WARN] Gemini response for {company.domain} had an ungrounded claim")
            log_event(
                "gemini_grounding_rejected",
                domain=company.domain,
                grounding_url_count=len(grounding_urls),
                reason="missing_or_unverified_grounding",
            )
        _apply_firmographics(
            company, data, source="gemini_grounded_search", allowed_source_urls=grounding_urls,
            strict_response=True, grounding_supports=grounding_supports,
        )
        missing_response_fields = sorted(_REQUIRED_RESPONSE_KEYS - data.keys())
        company.research_outcomes["_missing_response_fields"] = missing_response_fields
        completeness = enrichment_completeness(company)
        company.research_outcomes["_summary"] = completeness
        if grounding_invalid:
            return "ungrounded_response"
        if completeness["status"] == "partial":
            return "incomplete_result"
        if completeness["status"] == "complete_with_gaps":
            return "complete_with_gaps"
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
