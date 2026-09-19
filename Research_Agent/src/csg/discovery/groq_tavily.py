"""Company discovery using Tavily web search + Groq for result parsing.

A fallback DiscoveryProvider for when the Gemini key pool is quota-exhausted.
Split into two cheap calls instead of one expensive grounded-search call:
Tavily does the actual web search (returns compact snippets, not full pages),
Groq (a plain non-agentic model, no built-in search) turns those snippets into
structured CompanyCandidate JSON. This sidesteps groq/compound's built-in web
search, which burns 6000+ tokens per query against an 8000 TPM free-tier cap.

Same DiscoveryProvider interface and prompt-building as web_search.py, so
discover.py can swap providers with no other code changes.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import List

import httpx

from .base import DiscoveryProvider, CompanyCandidate
from .web_search import build_discovery_prompts, _clean_domain, _fallback_parse, DiscoveryError
from ..usage_tracker import track

_GROQ_MODEL = "openai/gpt-oss-120b"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_TAVILY_URL = "https://api.tavily.com/search"


class GroqTavilyProvider(DiscoveryProvider):
    """Discovery provider using Tavily search + Groq parsing."""

    def __init__(self, fetcher=None, cfg=None, groq_key: str | None = None, tavily_key: str | None = None):
        self.cfg = cfg
        self.groq_key = groq_key or os.environ.get("GROQ_API_KEY")
        self.tavily_key = tavily_key or os.environ.get("TAVILY_API_KEY")

    @property
    def name(self) -> str:
        return "groq_tavily_search"

    # Groq's free/on-demand tier is metered on tokens-per-minute, not
    # requests-per-minute -- a 429 here means "wait", not "exhausted for
    # today" (contrast with Gemini's daily quota), so a short sleep-and-retry
    # is the correct response rather than failing the whole run.
    _RATE_LIMIT_RETRIES = 3
    _RATE_LIMIT_DEFAULT_WAIT = 20.0

    def _tavily_search(self, client: httpx.Client, query: str, max_results: int = 8) -> list[dict]:
        if not self.tavily_key:
            raise DiscoveryError("TAVILY_API_KEY is not set -- required for groq_tavily_search discovery.")
        with track("tavily", "discovery_search") as u:
            r = client.post(_TAVILY_URL, json={
                "api_key": self.tavily_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            }, timeout=20.0)
            if r.status_code == 429:
                u.mark_failure(status_code=429, error_kind="rate_limited")
            r.raise_for_status()
            u.status_code = r.status_code
            return r.json().get("results", [])

    def _groq_parse(self, client: httpx.Client, prompt: str, search_results: list[dict]) -> str:
        if not self.groq_key:
            raise DiscoveryError("GROQ_API_KEY is not set -- required for groq_tavily_search discovery.")

        context = "\n\n".join(
            f"[{r.get('title', '')}]({r.get('url', '')})\n{r.get('content', '')[:2000]}"
            for r in search_results
        )
        instruction = (
            f"{prompt}\n\nUse the following web search results to find real company names -- do not "
            f"invent companies not mentioned in them. The results often list company names without "
            f"their website domain; when a domain isn't given, infer the most likely one from the "
            f"company's real, publicly known website (e.g. a company called \"Acme Robotics\" is very "
            f"likely acmerobotics.com) rather than skipping the company. Skip a company only if you "
            f"have no reasonable basis for its domain.\n\n{context}"
        )

        with track("groq", "discovery_parse") as u:
            for attempt in range(self._RATE_LIMIT_RETRIES + 1):
                r = client.post(_GROQ_URL, headers={
                    "Authorization": f"Bearer {self.groq_key}",
                    "Content-Type": "application/json",
                }, json={
                    "model": _GROQ_MODEL,
                    "messages": [{"role": "user", "content": instruction}],
                    "temperature": 0.3,
                }, timeout=30.0)

                if r.status_code == 429 and attempt < self._RATE_LIMIT_RETRIES:
                    u.retry_count += 1
                    wait = self._parse_retry_after(r) or self._RATE_LIMIT_DEFAULT_WAIT
                    print(f"  [GroqTavilyProvider] rate limited, waiting {wait:.0f}s ({attempt + 1}/{self._RATE_LIMIT_RETRIES})")
                    time.sleep(wait)
                    continue

                if r.status_code == 429:
                    u.mark_failure(status_code=429, error_kind="rate_limited")
                r.raise_for_status()
                u.status_code = r.status_code
                body = r.json()
                if isinstance(body.get("usage"), dict):
                    usage = body["usage"]
                    u.set_tokens(
                        prompt=usage.get("prompt_tokens"),
                        completion=usage.get("completion_tokens"),
                        total=usage.get("total_tokens"),
                    )
                return body["choices"][0]["message"]["content"]

        raise DiscoveryError("Groq rate limit retries exhausted")

    @staticmethod
    def _parse_retry_after(r: httpx.Response) -> float | None:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            return None
        m = re.search(r"try again in ([\d.]+)s", detail)
        return float(m.group(1)) if m else None

    async def discover(self, region: str, limit: int = 100) -> List[CompanyCandidate]:
        cfg = self.cfg
        if cfg is None:
            from ..config import load_config
            cfg = load_config()

        prompts = build_discovery_prompts(cfg, region, limit)
        failures: list[str] = []

        print(f"[GroqTavilyProvider] Running {len(prompts)} Tavily+Groq discovery queries for region: {region}")

        seen_domains: set[str] = set()
        candidates: list[CompanyCandidate] = []

        with httpx.Client() as client:
            for i, prompt in enumerate(prompts):
                if len(candidates) >= limit:
                    break

                print(f"  [{i + 1}/{len(prompts)}] Searching Tavily + parsing with Groq...")
                try:
                    search_query = prompt.split(".")[0]  # first sentence is the concrete ask
                    results = self._tavily_search(client, search_query)
                    raw_text = self._groq_parse(client, prompt, results).strip()

                    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
                    if json_match:
                        raw_text = json_match.group(1).strip()
                    array_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
                    if array_match:
                        raw_text = array_match.group(0)

                    companies = json.loads(raw_text)
                    if not isinstance(companies, list):
                        print("  [WARN] Response was not a list, skipping.")
                        continue

                    batch_count = 0
                    for entry in companies:
                        if not isinstance(entry, dict):
                            continue
                        domain_raw = entry.get("domain") or entry.get("website") or entry.get("url") or ""
                        name = entry.get("name") or entry.get("company") or ""
                        domain = _clean_domain(domain_raw)
                        if not domain or domain in seen_domains:
                            continue
                        seen_domains.add(domain)
                        batch_count += 1
                        candidates.append(CompanyCandidate(
                            domain=domain,
                            name=name.strip() if name else domain.split(".")[0].capitalize(),
                            source=self.name,
                            source_url=f"https://{domain}",
                            location=region,
                            industry=entry.get("industry"),
                            discovered_at=datetime.now(timezone.utc),
                        ))

                    print(f"  [{i + 1}/{len(prompts)}] Found {batch_count} new companies (total: {len(candidates)})")

                except json.JSONDecodeError as e:
                    print(f"  [WARN] Could not parse Groq response as JSON: {e}")
                    try:
                        _fallback_parse(raw_text, seen_domains, candidates, region, self.name)
                    except Exception:
                        failures.append(f"unparseable response: {e}")
                except Exception as e:
                    detail = f"{type(e).__name__}: {e}"
                    print(f"  [WARN] Discovery query failed: {detail}")
                    failures.append(detail)

        if failures and not candidates:
            raise DiscoveryError(
                f"all {len(prompts)} discovery queries failed for region "
                f"{region!r}: " + "; ".join(failures[:3]))

        print(f"[GroqTavilyProvider] Discovered {len(candidates)} unique domains total.")
        return candidates[:limit]
