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
from ..models import Company
from ..usage_tracker import track

_GEMINI_MODEL = "gemini-3.6-flash"

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
  "employee_count": <number or null>,
  "revenue": "<string like '$50M' or '$1.2B' or null>",
  "funding": "<string like '$100M Series C' or null>",
  "industry": "<string like 'Software / Marketing Automation' or null>",
  "description": "<1-2 sentence description of what the company does>",
  "hq_location": "<city, state or null>",
  "founded_year": <number or null>
}}

Important rules:
- Return ONLY the JSON object, no other text.
- Use null for any field you cannot determine with reasonable confidence.
- For revenue, if the company is private and revenue is not public, return null.
- Employee count should be the approximate current number.
- Be specific about industry - include sub-category (e.g., "Software / DevOps" not just "Technology").
"""


def _apply_firmographics(company: Company, data: dict[str, Any], source: str) -> None:
    """Write parsed firmographic JSON fields onto `company`, with provenance."""
    now = _utc_now_iso()

    # --- Employee Count ---
    emp = data.get("employee_count")
    if emp is not None:
        parsed_emp = _parse_number(str(emp))
        if parsed_emp and parsed_emp > 0:
            company.employee_count = {
                "value": parsed_emp,
                "source": source,
                "fetched_at": now,
                "method": "estimated",
                "confidence": "medium",
            }

    # --- Revenue ---
    rev = _parse_revenue(data.get("revenue") or "")
    if rev:
        company.revenue = {
            "value": rev,
            "source": source,
            "fetched_at": now,
            "method": "estimated",
            "confidence": "medium" if "$" in rev else "low",
        }

    # --- Funding ---
    funding = data.get("funding")
    if funding and str(funding).lower() not in ("null", "none", "n/a", "unknown"):
        company.funding = {
            "value": str(funding),
            "source": source,
            "fetched_at": now,
            "confidence": "medium",
        }

    # --- Industry ---
    industry = data.get("industry")
    if industry and str(industry).lower() not in ("null", "none", "n/a"):
        company.industry = str(industry)

    # --- Description ---
    desc = data.get("description")
    if desc and str(desc).lower() not in ("null", "none"):
        company.description = str(desc)

    # --- HQ Location ---
    hq = data.get("hq_location")
    if hq and str(hq).lower() not in ("null", "none"):
        # Try to extract state
        state_match = re.search(r',\s*([A-Z]{2})$', str(hq))
        if state_match:
            company.hq_state = state_match.group(1)

    print(f"  [OK] Enriched {company.domain}: "
          f"emp={company.employee_count.get('value') if isinstance(company.employee_count, dict) else '?'}, "
          f"rev={company.revenue.get('value') if isinstance(company.revenue, dict) else '?'}, "
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
    query = f"{name} ({company.domain}) company employee count revenue funding industry"

    try:
        sync_client, _ = make_ipv4_httpx_clients()
        with sync_client as client:
            with track("tavily", "tavily_search", domain=company.domain) as u:
                for attempt in range(_TAVILY_RATE_LIMIT_RETRIES + 1):
                    r = client.post(_TAVILY_URL, json={
                        "api_key": tavily_key,
                        "query": query,
                        "max_results": 8,
                        "search_depth": "basic",
                    }, timeout=20.0)
                    if r.status_code == 429 and attempt < _TAVILY_RATE_LIMIT_RETRIES:
                        u.retry_count += 1
                        wait = GroqTavilyProvider._parse_retry_after(r) or _TAVILY_RATE_LIMIT_DEFAULT_WAIT
                        print(f"  [WARN] Tavily rate limited for {company.domain}, waiting "
                              f"{wait:.0f}s ({attempt + 1}/{_TAVILY_RATE_LIMIT_RETRIES})")
                        _time.sleep(wait)
                        continue
                    if r.status_code == 429:
                        u.mark_failure(status_code=429, error_kind="rate_limited")
                    r.raise_for_status()
                    u.status_code = r.status_code
                    break
            results = r.json().get("results", [])

            context = "\n\n".join(
                f"[{res.get('title', '')}]({res.get('url', '')})\n{res.get('content', '')[:2000]}"
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
                    if gr.status_code == 429 and attempt < GroqTavilyProvider._RATE_LIMIT_RETRIES:
                        u.retry_count += 1
                        wait = GroqTavilyProvider._parse_retry_after(gr) or GroqTavilyProvider._RATE_LIMIT_DEFAULT_WAIT
                        print(f"  [WARN] Groq rate limited for {company.domain}, waiting "
                              f"{wait:.0f}s ({attempt + 1}/{GroqTavilyProvider._RATE_LIMIT_RETRIES})")
                        _time.sleep(wait)
                        continue
                    if gr.status_code == 429:
                        u.mark_failure(status_code=429, error_kind="rate_limited")
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
        _apply_firmographics(company, data, source="groq_tavily_search")
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
        _apply_firmographics(company, data, source="gemini_grounded_search")
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
