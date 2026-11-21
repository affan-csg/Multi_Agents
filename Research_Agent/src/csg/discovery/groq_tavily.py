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
import email.utils
import random
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
    """DISABLED legacy discovery provider using Tavily search + Groq parsing."""

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
        raise DiscoveryError("Tavily/Groq fallback is disabled")

    def _groq_parse(self, client: httpx.Client, prompt: str, search_results: list[dict]) -> str:
        raise DiscoveryError("Tavily/Groq fallback is disabled")

    @staticmethod
    def _parse_retry_after(r: httpx.Response) -> float | None:
        header = r.headers.get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                try:
                    return max(0.0, email.utils.parsedate_to_datetime(header).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            return None
        m = re.search(r"try again in ([\d.]+)s", detail)
        return float(m.group(1)) if m else None

    @classmethod
    def _retry_delay(cls, response: httpx.Response, attempt: int) -> float:
        exponential = cls._RATE_LIMIT_DEFAULT_WAIT * (2 ** attempt)
        advertised = cls._parse_retry_after(response)
        base = advertised if advertised is not None else exponential
        return base + random.uniform(0.0, base * 0.25)

    async def discover(self, region: str, limit: int = 100) -> List[CompanyCandidate]:
        raise DiscoveryError("Tavily/Groq fallback is disabled; use Gemini-only discovery")
