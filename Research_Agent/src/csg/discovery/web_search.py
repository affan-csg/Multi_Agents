"""Real autonomous company discovery using Gemini AI with grounded search.

Uses Google's Gemini API with the google_search tool to discover real
companies in a target geographic region that match CSG's ICP.  Results
are parsed into CompanyCandidate objects for deduplication and downstream
enrichment.
"""

from __future__ import annotations

import json
import re
import asyncio
import time
from datetime import datetime, timezone
from typing import List
from urllib.parse import urlparse

from .base import DiscoveryProvider, CompanyCandidate
from .gemini_keys import GeminiKeyPool, is_connection_error
from ..http import Fetcher
from ..usage_tracker import track

# Domains we never want to discover (job boards, social media, etc.)
_BLACKLIST_DOMAINS = {
    "google.com", "linkedin.com", "indeed.com", "glassdoor.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org",
    "reddit.com", "medium.com", "github.com", "crunchbase.com", "zoominfo.com",
    "bloomberg.com", "forbes.com", "techcrunch.com", "builtin.com", "wellfound.com",
    "angel.co", "lever.co", "greenhouse.io", "workday.com", "ziprecruiter.com",
    "monster.com", "simplyhired.com", "dice.com", "yelp.com", "bbb.org",
    "yahoo.com", "bing.com", "pinterest.com", "tiktok.com", "snap.com",
    "apple.com", "microsoft.com", "amazon.com", "amazonaws.com",
    "bizjournals.com", "inc.com", "entrepreneur.com", "wired.com",
    "news.ycombinator.com", "producthunt.com", "g2.com", "capterra.com",
    "trustpilot.com", "comparably.com", "theorg.com", "pitchbook.com",
    "cbinsights.com", "statista.com", "similarweb.com", "careersourcegroup.com",
    # Staffing competitors
    "roberthalf.com", "adeccousa.com", "randstadusa.com", "kellyservices.com",
    "manpowergroup.com", "teksystems.com", "actalentservices.com", "toptal.com",
    "turing.com", "andela.com", "crossover.com", "upwork.com", "fiverr.com",
    "deel.com", "remote.com", "revature.com",
}

_GEMINI_MODEL = "gemini-3.6-flash"

# Plain-English labels for the practice areas, used when describing the ICP to
# the search model. Keyed by the practice_areas keys in icp.yaml.
_AREA_PHRASES = {
    "ai_ml": "AI/ML",
    "mlops": "MLOps",
    "data_engineering": "data engineering",
    "devops": "DevOps",
    "devsecops_platform": "platform and security engineering",
    "cloud": "cloud infrastructure",
    "software_dev": "software engineering",
    "product_pm_ba_qa": "product and delivery",
    "cybersecurity_grc": "cybersecurity and GRC",
    "non_tech_ops": "revenue and back-office operations",
}


def build_discovery_prompts(cfg, region: str, target: int) -> list[str]:
    """Generate search queries from the embedded ICP.

    The size band, priority practice areas, and geography all come from
    icp.yaml -- so changing the ICP changes what gets searched, with no edit
    here. Two complementary queries are issued: one anchored on the region's
    named hubs, one on hiring activity, because they surface different
    companies.
    """
    region_key = cfg.resolve_region(region)
    spec = cfg.regions.get(region_key) or {}
    label = spec.get("label") or region_key
    hubs = cfg.region_hubs(region_key)
    where = f"{label} ({', '.join(hubs[:6])})" if hubs else f"{label}, USA"

    size = cfg.size
    band = f"{int(size.get('sweet_min', 30))}-{int(size.get('sweet_max', 3000))} employees"

    # Highest-weighted practice areas describe what "relevant hiring" means.
    top_areas = sorted(cfg.enabled_areas, key=lambda a: -a.weight)[:6]
    areas = ", ".join(_AREA_PHRASES.get(a.key, a.key.replace("_", " ")) for a in top_areas)

    fin = cfg.raw.get("financials") or {}
    revenue_min = float(fin.get("revenue_sweet_min", 0) or 0)
    money = (f" Prefer companies with roughly ${revenue_min / 1e6:,.0f}M+ in revenue "
             f"or meaningful venture funding." if revenue_min else "")

    per_query = max(target // 2 + 5, 10)
    shape = ('\n\nReturn ONLY a valid JSON array, no prose: '
             '[{"name": "CompanyName", "domain": "company.com", "industry": "..."}]')

    return [
        (f"List {per_query} technology companies headquartered in {where}. "
         f"Target roughly {band}. Prioritise companies working in {areas}.{money} "
         f"Exclude staffing agencies, recruiting firms, and consultancies."
         + shape),
        (f"List {per_query} companies in {where} that are actively hiring for "
         f"{areas} roles right now. Roughly {band}. "
         f"Exclude staffing agencies, recruiting firms, and job boards."
         + shape),
    ]




def _clean_domain(domain: str) -> str | None:
    """Normalize and validate a domain string."""
    if not domain:
        return None
    domain = domain.lower().strip().rstrip("/")
    # Remove protocol
    if "://" in domain:
        domain = urlparse(domain).netloc or domain.split("://")[1].split("/")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    domain = domain.split("/")[0].split(":")[0]  # Remove paths and ports
    if not domain or "." not in domain:
        return None
    if domain in _BLACKLIST_DOMAINS:
        return None
    for bl in _BLACKLIST_DOMAINS:
        if domain.endswith(f".{bl}"):
            return None
    return domain


class DiscoveryError(RuntimeError):
    """Discovery could not run at all (missing key, every query failed).

    Distinct from "ran and found nothing": an empty result is a legitimate
    answer, a failed run is not, and the caller must be able to tell them
    apart instead of silently exporting zero prospects.
    """


class WebSearchProvider(DiscoveryProvider):
    """Real discovery provider using Gemini AI with grounded search."""

    def __init__(self, fetcher: Fetcher, api_key: str = None, cfg=None):
        self.fetcher = fetcher
        self.cfg = cfg
        # A single explicit api_key (tests, callers that want one fixed key)
        # bypasses the pool entirely. Otherwise pull every GEMINI_API_KEY*
        # var from the environment so a quota-exhausted key can fail over to
        # the next one instead of failing the whole run.
        self._explicit_key = api_key
        self._pool = None if api_key else GeminiKeyPool.from_env()

    @property
    def name(self) -> str:
        return "gemini_grounded_search"

    # Transient connection failures (TLS handshake drops, connection resets)
    # are unrelated to key quota and resolve on a same-key retry -- distinct
    # from _pool.should_advance(), which only fires for daily quota exhaustion.
    _NETWORK_RETRIES = 2
    _NETWORK_RETRY_DELAY = 3.0

    def _generate(self, prompt: str):
        """One generate_content call, rotating through the key pool on daily
        quota exhaustion and retrying the same key on transient network
        errors. Raises the last error if every key is exhausted, every
        network retry is spent, or no pool is configured."""
        from .gemini_keys import make_gemini_client

        contents = (prompt + '\n\nReturn ONLY a valid JSON array like: '
                    '[{"name": "CompanyName", "domain": "company.com"}, ...]')
        config = {"temperature": 0.3, "tools": [{"google_search": {}}]}

        def call(key: str):
            client = make_gemini_client(key)
            with track("gemini", "discovery_search") as u:
                response = client.models.generate_content(model=_GEMINI_MODEL, contents=contents, config=config)
                meta = getattr(response, "usage_metadata", None)
                if meta is not None:
                    u.set_tokens(
                        prompt=getattr(meta, "prompt_token_count", None),
                        completion=getattr(meta, "candidates_token_count", None),
                        total=getattr(meta, "total_token_count", None),
                    )
                return response

        def call_with_network_retry(key: str):
            for i in range(self._NETWORK_RETRIES + 1):
                try:
                    return call(key)
                except Exception as exc:
                    if is_connection_error(exc) and i < self._NETWORK_RETRIES:
                        print(f"  [WebSearchProvider] transient network error "
                              f"({type(exc).__name__}), retrying ({i + 1}/{self._NETWORK_RETRIES})")
                        time.sleep(self._NETWORK_RETRY_DELAY)
                        continue
                    raise

        if self._explicit_key is not None:
            return call_with_network_retry(self._explicit_key)

        last_exc: Exception | None = None
        for attempt in self._pool.attempts():
            try:
                response = call_with_network_retry(attempt.key)
                if attempt.index > 1:
                    print(f"  [WebSearchProvider] key {attempt.index}/{attempt.total} succeeded")
                return response
            except Exception as exc:
                last_exc = exc
                if self._pool.should_advance(exc):
                    print(f"  [WebSearchProvider] key {attempt.index}/{attempt.total} "
                          f"quota exhausted, trying next key")
                    self._pool.mark_exhausted(attempt.key)
                    continue
                raise
        raise DiscoveryError(self._pool.exhausted_message()) from last_exc

    async def discover(self, region: str, limit: int = 100) -> List[CompanyCandidate]:
        """Discover real companies via Gemini AI grounded search."""
        if self._explicit_key is None and not self._pool:
            raise DiscoveryError(self._pool.exhausted_message())

        cfg = self.cfg
        if cfg is None:
            from ..config import load_config
            cfg = load_config()

        prompts = build_discovery_prompts(cfg, region, limit)
        failures: list[str] = []

        print(f"[WebSearchProvider] Running {len(prompts)} Gemini discovery queries for region: {region}")

        seen_domains: set[str] = set()
        candidates: list[CompanyCandidate] = []

        for i, prompt in enumerate(prompts):
            if len(candidates) >= limit:
                break

            print(f"  [{i+1}/{len(prompts)}] Querying Gemini for companies...")

            try:
                response = self._generate(prompt)
                raw_text = response.text.strip()

                # Extract JSON array from response
                json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw_text, re.DOTALL)
                if json_match:
                    raw_text = json_match.group(1).strip()

                # Try to find JSON array in text
                array_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
                if array_match:
                    raw_text = array_match.group(0)

                companies = json.loads(raw_text)

                if not isinstance(companies, list):
                    print(f"  [WARN] Response was not a list, skipping.")
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

                print(f"  [{i+1}/{len(prompts)}] Found {batch_count} new companies (total: {len(candidates)})")

            except json.JSONDecodeError as e:
                print(f"  [WARN] Could not parse Gemini response as JSON: {e}")
                # Try line-by-line fallback parsing
                try:
                    _fallback_parse(raw_text, seen_domains, candidates, region, self.name)
                except Exception:
                    failures.append(f"unparseable response: {e}")
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                print(f"  [WARN] Discovery query failed: {detail}")
                failures.append(detail)

            # Brief delay between queries
            if i < len(prompts) - 1:
                await asyncio.sleep(2)

        # Every query erroring is an infrastructure failure, not "no matches" --
        # surface it so a run cannot silently export zero prospects.
        if failures and not candidates:
            raise DiscoveryError(
                f"all {len(prompts)} discovery queries failed for region "
                f"{region!r}: " + "; ".join(failures[:3]))

        print(f"[WebSearchProvider] Discovered {len(candidates)} unique domains total.")
        return candidates[:limit]


def _fallback_parse(text: str, seen: set, candidates: list, region: str, source: str) -> None:
    """Try to extract company names and domains from unstructured text."""
    # Look for domain-like patterns
    domain_pattern = re.compile(r'([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|io|co|ai|dev|tech|net|org))')
    for match in domain_pattern.finditer(text):
        domain = _clean_domain(match.group(1))
        if domain and domain not in seen:
            seen.add(domain)
            candidates.append(CompanyCandidate(
                domain=domain,
                name=domain.split(".")[0].capitalize(),
                source=source,
                source_url=f"https://{domain}",
                location=region,
                discovered_at=datetime.now(timezone.utc),
            ))
