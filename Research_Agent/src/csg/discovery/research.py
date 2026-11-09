"""Real company enrichment using Gemini AI for firmographic research.

Uses Google's Gemini API with grounded search to look up real company
data: employee count, revenue, funding, industry, and description.
All data is returned with strict provenance tracking.

Falls back to Tavily search + Groq parsing (same pair used as the discovery
fallback in groq_tavily.py) when every Gemini key is exhausted or missing,
so a Gemini daily-quota outage doesn't leave every company unenriched.
"""

from __future__ import annotations

import json
import os
import re
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .gemini_keys import GeminiKeyPool, is_connection_error, is_daily_quota_exhausted, make_ipv4_httpx_clients
from .groq_tavily import GroqTavilyProvider, _GROQ_URL, _GROQ_MODEL, _TAVILY_URL
from ..models import Company, ProvenanceValue, provenance_value
from ..usage_tracker import track

_GEMINI_MODEL = "gemini-3.8-flash"

# A connection error (dropped TLS handshake, connect timeout, ...) can be a
# one-off blip even with IPv4 pinning -- worth one quick retry on the same
# key before burning the whole key pool and falling through to Tavily+Groq.
_CONNECTION_ERROR_RETRIES = 1
_CONNECTION_ERROR_RETRY_DELAY = 2.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


_PROMPT_TEMPLATE = """You are a business research analyst. For the company with domain "{domain}" (name: {name}), provide the following information in JSON format.

Research this company and return ONLY a valid JSON object with these fields:
{{
    "official_website": "https://company.example or null",
        "official_website_source_url": "URL supporting the official website or null",
    "headquarters": {{"city": "...", "state": "...", "country": "..."}} or null,
        "headquarters_source_url": "URL supporting headquarters or null",
    "company_type": "Startup, SMB, System Integrator, Public Company, Private Company, or another explicit type | null",
        "company_type_source_url": "URL supporting company_type or null",
  "employee_count": <number or null>,
    "employee_count_source_url": "URL supporting employee_count or null",
  "revenue": "<string like '$50M' or '$1.2B' or null>",
    "revenue_source_url": "URL supporting revenue or null",
  "funding": "<string like '$100M Series C' or null>",
    "funding_source_url": "URL supporting funding or null",
  "industry": "<string like 'Software / Marketing Automation' or null>",
  "description": "<1-2 sentence description of what the company does>",
  "hq_location": "<city, state or null>",
    "founded_year": <number or null>,
    "classification_evidence": [
        {{"category": "startup|smb|system_integrator", "supports": true, "fact": "short exact fact", "why": "why this supports or contradicts the category", "source_name": "source title", "source_url": "exact supporting URL", "confidence": "high|medium|low", "evidence_type": "direct|inferred"}}
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
- Run targeted research for each category: founding date/funding stage and explicit startup language for startup; reliable employee-count/company-size facts for SMB; and explicit evidence that the company provides systems integration, technology implementation, IT consulting, or integration services to clients for system integrator.
- Return classification_evidence only when the extracted fact supports or reasonably contradicts that category. Never use a generic company URL as evidence and never infer SMB from an unsupported estimate.
"""


def _apply_firmographics(
    company: Company, data: dict[str, Any], source: str, allowed_source_urls: set[str] | None = None,
) -> None:
    """Write parsed firmographic JSON fields onto `company`, with provenance."""
    now = _utc_now_iso()
    allowed_source_urls = allowed_source_urls or set()

    def fact_source_url(field: str) -> str | None:
        candidate = data.get(f"{field}_source_url")
        return candidate if isinstance(candidate, str) and candidate in allowed_source_urls else None

    def save_fact(field: str, value: Any, source_field: str, *, method: str = "reported",
                  confidence: str = "high") -> None:
        source_url = fact_source_url(source_field)
        if value is not None and source_url:
            setattr(company, field, ProvenanceValue(
                value=value, source=source, url=source_url,
                fetched_at=now, method=method, confidence=confidence,
            ))

    official_website = data.get("official_website")
    if isinstance(official_website, str) and official_website.startswith(("http://", "https://")):
        parsed = httpx.URL(official_website)
        domain = company.domain.lower().removeprefix("www.")
        host = (parsed.host or "").lower().removeprefix("www.")
        if host == domain or host.endswith(f".{domain}"):
            save_fact("website", official_website, "official_website")

    headquarters = data.get("headquarters")
    if isinstance(headquarters, dict) and any(headquarters.get(key) for key in ("city", "state", "country")):
        save_fact("headquarters", {
            key: str(headquarters[key]).strip()
            for key in ("city", "state", "country") if headquarters.get(key)
        }, "headquarters")

    company_type = data.get("company_type")
    if isinstance(company_type, str) and company_type.strip():
        normalized_type = company_type.strip().lower().replace(" ", "_")
        if normalized_type not in {"startup", "smb", "system_integrator"}:
            normalized_type = company_type.strip()
        save_fact("company_type", normalized_type, "company_type")

    classification_evidence = data.get("classification_evidence")
    if isinstance(classification_evidence, list):
        validated: list[dict[str, Any]] = []
        for item in classification_evidence:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip().lower()
            source_url = item.get("source_url")
            fact = str(item.get("fact") or "").strip()
            why = str(item.get("why") or "").strip()
            if category not in {"startup", "smb", "system_integrator"} or not isinstance(source_url, str):
                continue
            if source_url not in allowed_source_urls or not fact or not why:
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
                "source_url": source_url, "confidence": confidence,
                "evidence_type": evidence_type, "retrieved_at": now,
            })
        company.classification_evidence = validated

    # --- Employee Count ---
    emp = data.get("employee_count")
    if emp is not None:
        parsed_emp = _parse_number(str(emp))
        if parsed_emp and parsed_emp > 0:
                company.employee_count = ProvenanceValue(
                    value=parsed_emp, source=source,
                    url=fact_source_url("employee_count"), fetched_at=now,
                    method="estimated", confidence="medium",
                )

    # --- Revenue ---
    rev = _parse_revenue(data.get("revenue") or "")
    if rev:
            company.revenue = ProvenanceValue(
                value=rev, source=source, url=fact_source_url("revenue"),
                fetched_at=now, method="estimated",
                confidence="medium" if "$" in rev else "low",
            )

    # --- Funding ---
    funding = data.get("funding")
    if funding and str(funding).lower() not in ("null", "none", "n/a", "unknown"):
            company.funding = ProvenanceValue(
                value=str(funding), source=source, url=fact_source_url("funding"),
                fetched_at=now, confidence="medium",
            )

    # --- Industry ---
    industry = data.get("industry")
    if industry and str(industry).lower() not in ("null", "none", "n/a"):
        company.industry = str(industry)

    # --- Description ---
    desc = data.get("description")
    if desc and str(desc).lower() not in ("null", "none"):
        company.description = str(desc)

    # Keep the legacy display field populated from the researched HQ envelope.
    if company.headquarters:
        hq = company.headquarters.get("value") if isinstance(company.headquarters, dict) else company.headquarters.value
        if isinstance(hq, dict) and hq.get("state"):
            company.hq_state = str(hq["state"])

    print(f"  [OK] Enriched {company.domain}: "
            f"emp={provenance_value(company.employee_count) or '?'}, "
            f"rev={provenance_value(company.revenue) or '?'}, "
          f"industry={company.industry or '?'}")


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    raw_text = raw_text.strip()
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
    if json_match:
        raw_text = json_match.group(1).strip()
    obj_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if obj_match:
        raw_text = obj_match.group(0)
    return json.loads(raw_text)


# Tavily's free tier is metered per-minute, so a burst of concurrent
# enrichment calls (ENRICH_CONCURRENCY companies in flight at once) can hit
# its rate limit even when the account has plenty of monthly quota left --
# same shape of transient, retry-worthy failure as Groq's 429 below, not a
# hard exhaustion.
_TAVILY_RATE_LIMIT_RETRIES = 2
_TAVILY_RATE_LIMIT_DEFAULT_WAIT = 5.0


async def _enrich_via_groq_tavily(company: Company) -> Optional[str]:
    """Fallback firmographic enrichment via Tavily search + Groq parsing.

    Used when the Gemini key pool is missing or every key's daily quota is
    exhausted. Mirrors GroqTavilyProvider's discovery fallback: Tavily does
    the web search, Groq (no built-in search) turns the snippets into the
    same structured JSON shape enrich_company_firmographics expects.

    Both calls retry on a 429 (rate limit, not quota exhaustion) with the
    same backoff GroqTavilyProvider uses for discovery -- without this, this
    fallback had zero resilience of its own, so a concurrent-run rate limit
    on either provider surfaced immediately as "enrichment request failed"
    even though the correct response was just to wait and retry.
    """
    groq_key = os.environ.get("GROQ_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not groq_key or not tavily_key:
        print(f"  [WARN] GROQ_API_KEY/TAVILY_API_KEY not set, cannot fall back "
              f"for {company.domain}")
        return "quota_exhausted"

    name = company.name or company.domain.split(".")[0]
    targeted_queries = [
        f"{name} company size number of employees",
        f"{name} company type startup or established company",
        f"{name} company profile founded funding employees",
        f"{name} system integrator services",
    ]

    try:
        sync_client, _ = make_ipv4_httpx_clients()
        with sync_client as client:
            results: list[dict[str, Any]] = []
            for query in targeted_queries:
                with track("tavily", "tavily_search", domain=company.domain) as u:
                    for attempt in range(_TAVILY_RATE_LIMIT_RETRIES + 1):
                        r = client.post(_TAVILY_URL, json={
                            "api_key": tavily_key,
                            "query": query,
                            "max_results": 4,
                            "search_depth": "basic",
                        }, timeout=20.0)
                        if r.status_code in {429, 500, 502, 503, 504} and attempt < _TAVILY_RATE_LIMIT_RETRIES:
                            u.retry_count += 1
                            wait = GroqTavilyProvider._retry_delay(r, attempt)
                            print(f"  [WARN] Tavily rate limited for {company.domain}, waiting "
                                  f"{wait:.0f}s ({attempt + 1}/{_TAVILY_RATE_LIMIT_RETRIES})")
                            _time.sleep(wait)
                            continue
                        if r.status_code in {429, 500, 502, 503, 504}:
                            u.mark_failure(status_code=r.status_code, error_kind="rate_limited" if r.status_code == 429 else "other")
                        r.raise_for_status()
                        u.status_code = r.status_code
                        break
                results.extend({**res, "targeted_query": query} for res in r.json().get("results", []))
            unique_results: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for result in results:
                url = str(result.get("url") or "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    unique_results.append(result)
            results = unique_results
            allowed_source_urls = seen_urls

            context = "\n\n".join(
                f"[Query: {res.get('targeted_query', '')}] [{res.get('title', '')}]({res.get('url', '')})\n{res.get('content', '')[:800]}"
                for res in results
            )
            instruction = _PROMPT_TEMPLATE.format(domain=company.domain, name=name) + (
                f"\n\nUse the following web search results to answer. If the results don't "
                f"contain enough information for a field, use null for that field rather than "
                f"guessing.\n\n{context}"
            )

            with track("groq", "groq_parse", domain=company.domain) as u:
                for attempt in range(GroqTavilyProvider._RATE_LIMIT_RETRIES + 1):
                    gr = client.post(_GROQ_URL, headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                    }, json={
                        "model": _GROQ_MODEL,
                        "messages": [{"role": "user", "content": instruction}],
                        "temperature": 0.1,
                    }, timeout=30.0)
                    if gr.status_code in {429, 500, 502, 503, 504} and attempt < GroqTavilyProvider._RATE_LIMIT_RETRIES:
                        u.retry_count += 1
                        wait = GroqTavilyProvider._retry_delay(gr, attempt)
                        print(f"  [WARN] Groq rate limited for {company.domain}, waiting "
                              f"{wait:.0f}s ({attempt + 1}/{GroqTavilyProvider._RATE_LIMIT_RETRIES})")
                        _time.sleep(wait)
                        continue
                    if gr.status_code in {429, 500, 502, 503, 504}:
                        u.mark_failure(status_code=gr.status_code, error_kind="rate_limited" if gr.status_code == 429 else "other")
                    gr.raise_for_status()
                    u.status_code = gr.status_code
                    body = gr.json()
                    raw_text = body["choices"][0]["message"]["content"]
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
        )
        return None

    except json.JSONDecodeError as e:
        print(f"  [WARN] Could not parse Groq fallback response for {company.domain}: {e}")
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
        return reason if reason in ("connection_error", "timeout", "rate_limited") else "request_error"


async def enrich_company_firmographics(company: Company) -> Optional[str]:
    """Enrich a company with real firmographic data via Gemini API.

    Falls back to Tavily+Groq (see _enrich_via_groq_tavily) if no Gemini key
    is configured or every key's daily quota is exhausted, so a Gemini
    outage doesn't leave every company unenriched. Only if that fallback is
    also unavailable/fails does this mark the company unresearched -- a
    failed enrichment must not fail the whole discovery run. Returns a short
    failure-reason string (e.g. "no_api_key", "quota_exhausted",
    "parse_error") the caller can tally, so a systemic problem shows up as a
    clear run-level warning instead of N silent per-company skips buried in
    server logs. Returns None on success.
    """
    pool = GeminiKeyPool.from_env()
    if not pool:
        print(f"  [WARN] No GEMINI_API_KEY set, trying Tavily+Groq fallback for {company.domain}")
        return await _enrich_via_groq_tavily(company)

    prompt = _PROMPT_TEMPLATE.format(
        domain=company.domain,
        name=company.name or company.domain.split(".")[0],
    )

    try:
        from .gemini_keys import make_gemini_client

        def _call_with_connection_retry(key: str):
            with track("gemini", "enrich_firmographics", domain=company.domain) as u:
                for i in range(_CONNECTION_ERROR_RETRIES + 1):
                    try:
                        client = make_gemini_client(key)
                        response = client.models.generate_content(
                            model=_GEMINI_MODEL,
                            contents=prompt,
                            config={
                                "temperature": 0.1,
                                "tools": [{"google_search": {}}],
                            },
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
                        if is_connection_error(exc) and i < _CONNECTION_ERROR_RETRIES:
                            u.retry_count += 1
                            print(f"  [WARN] transient connection error reaching Gemini for "
                                  f"{company.domain} ({type(exc).__name__}), retrying "
                                  f"({i + 1}/{_CONNECTION_ERROR_RETRIES})")
                            _time.sleep(_CONNECTION_ERROR_RETRY_DELAY)
                            continue
                        raise

        response = None
        last_exc: Exception | None = None
        for attempt in pool.attempts():
            try:
                response = _call_with_connection_retry(attempt.key)
                break
            except Exception as exc:
                last_exc = exc
                if pool.should_advance(exc):
                    pool.mark_exhausted(attempt.key)
                    continue
                # A non-quota failure (network blip that outlasted the retry
                # above, malformed/empty response from the grounded-search
                # tool, transient 5xx, safety-filter block, ...) is not
                # specific to this one key -- trying the next configured key
                # would just repeat the same failure, so stop the key loop
                # here and fall through to the Tavily+Groq fallback below
                # exactly the same way quota-exhaustion does, rather than
                # giving up on this company outright.
                break
        if response is None:
            if last_exc and pool.should_advance(last_exc):
                reason = "all Gemini keys exhausted"
            elif last_exc and is_connection_error(last_exc):
                reason = "Gemini unreachable (connection/TLS error, not a quota or request issue)"
            else:
                reason = "Gemini request failed"
            print(f"  [WARN] {reason} for {company.domain}, trying Tavily+Groq fallback: {last_exc}")
            return await _enrich_via_groq_tavily(company)

        data = _extract_json_object(response.text)
        grounding_urls: set[str] = set()
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            metadata = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(metadata, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None)
                if uri:
                    grounding_urls.add(uri)
        _apply_firmographics(
            company, data, source="gemini_grounded_search", allowed_source_urls=grounding_urls,
        )
        return None

    except json.JSONDecodeError as e:
        print(f"  [WARN] Could not parse Gemini response for {company.domain}: {e}")
        return "parse_error"
    except Exception as e:
        # Reachable only for a failure outside the key-attempt loop itself
        # (e.g. _extract_json_object/_apply_firmographics raising on a
        # response Gemini did return) -- the Gemini call path's own failures
        # are all handled above and never reach here.
        from ..usage_tracker import classify_error
        reason = classify_error(e)
        print(f"  [WARN] Enrichment failed for {company.domain} ({reason}): {type(e).__name__}: {e}")
        return reason if reason in ("connection_error", "timeout", "rate_limited", "quota_exhausted") else "request_error"
