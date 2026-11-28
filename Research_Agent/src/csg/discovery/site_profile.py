"""Deterministic, first-party company profiling.

This module reads a company's own public pages over HTTP and extracts facts
without asking a model. It exists because grounded model calls can return a
usable answer with no grounding metadata at all -- in that case strict
validation correctly rejects every claim, and the company ends up with no
research even though its own website states most of what we need.

Design boundaries:

* No model, no queue, no provider: this is a separate, additive evidence
  source. Gemini enrichment still runs first; this module only fills fields
  the model did not accept, and records how each fact was verified
  (``official_site_fetch``) beside the model's provenance.
* A fact is accepted only when the retrieved source states that exact value:
  a verbatim page line, a structured JSON-LD node, or the page's own canonical
  URL/meta text. The same whole-phrase, non-negated check the model path uses
  is applied to prose (``research._text_supports_value``), and malformed/CTA
  text is rejected here too, so an official page cannot smuggle in a weaker
  claim.
* Persistence reuses the existing ``companies`` JSONB columns
  (``research_evidence`` and ``research_outcomes``). No new table or column is
  required: evidence carries source, excerpt, retrieval time and method, and
  ``research_outcomes["_site_profile"]`` carries per-stage retrieval status.
* Fetching is deliberately conservative: only public IPs, http/https on
  standard ports, redirects re-validated hop by hop, response-size caps,
  per-host serialization with a delay, and robots.txt enforcement.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from ..models import Company, ProvenanceValue
from .research import (
    _FIELD_CRITERIA, _claim_is_call_to_action, _evidence_key, _has_malformed_text,
    _text_is_malformed, _text_supports_value, _utc_now_iso,
)

EXTRACTOR_VERSION = "official_site_v1"
SOURCE_NAME = "official_site_fetch"

USER_AGENT = (
    "CSGResearchBot/1.0 (company research; contact: research@csg.example)"
)
MAX_BYTES = 1_500_000
MAX_ROBOTS_BYTES = 256_000
MAX_REDIRECTS = 4
REQUEST_TIMEOUT_SECONDS = 12.0
PER_HOST_DELAY_SECONDS = 1.0
MAX_PAGES = 6
MAX_RESPONSE_SECONDS = 45.0

_HTML_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "application/xml", "text/xml", "text/plain",
)

_TECHNOLOGIES: dict[str, tuple[str, ...]] = {
    "WordPress": (r"wp-content/", r'name=["\']generator["\'][^>]*wordpress'),
    "Shopify": (r"cdn\.shopify\.com", r"shopify\.theme"),
    "Webflow": (r"webflow\.(?:js|com)", r'content=["\']Webflow'),
    "Wix": (r"wixstatic\.com", r"wix\.com"),
    "Squarespace": (r"squarespace\.com", r"static1\.squarespace"),
    "HubSpot": (r"hs-scripts\.com", r"hubspot\.com"),
    "Marketo": (r"marketo\.(?:com|net)", r"munchkin\.js"),
    "Salesforce": (r"salesforce\.com", r"pardot"),
    "Segment": (r"cdn\.segment\.(?:com|io)",),
    "Google Analytics": (r"googletagmanager\.com/gtag", r"google-analytics\.com/analytics\.js"),
    "Google Tag Manager": (r"googletagmanager\.com/gtm",),
    "Intercom": (r"widget\.intercom\.io",),
    "Drift": (r"js\.driftt\.com",),
    "Zendesk": (r"zdassets\.com", r"zendesk\.com"),
    "HubSpot Forms": (r"forms\.hsforms\.net",),
    "React": (r"react(?:\.production)?(?:\.min)?\.js", r"data-reactroot"),
    "Next.js": (r"__NEXT_DATA__", r"/_next/static/"),
    "Vue.js": (r"vue(?:\.min)?\.js", r"data-v-[0-9a-f]{6}"),
    "Cloudflare": (r"cdnjs\.cloudflare\.com", r"cloudflare\.com"),
}

_ROLE_PATTERNS = (
    r"chief\s+[a-z]+\s+officer",
    r"ceo", r"cto", r"cfo", r"coo", r"cro", r"cpo", r"ciso",
    r"co[- ]?founder", r"founder", r"owner", r"president",
    r"vice\s+president", r"vp\b", r"head\s+of\s+[a-z ]{2,30}",
    r"managing\s+director", r"general\s+manager", r"director",
    r"principal", r"partner",
)
_ROLE_RE = re.compile(r"\b(?:" + "|".join(_ROLE_PATTERNS) + r")\b", re.I)

_NON_NAME_WORDS = frozenset({
    "team", "our", "about", "leadership", "contact", "services", "solutions",
    "company", "careers", "products", "customers", "partners", "news", "blog",
    "privacy", "terms", "home", "menu", "search", "login", "support", "sales",
    "marketing", "operations", "engineering", "security", "platform", "data",
    "cloud", "digital", "global", "group", "inc", "llc", "ltd", "corp",
    "read", "more", "learn", "view", "click", "submit", "download", "watch",
    "request", "book", "schedule",
})

_LEADERSHIP_FALSE_POSITIVE = re.compile(
    r"\b(?:former|formerly|previous|retired|testimonial|customer story|case study|"
    r"reviewed by|read more|learn more|all rights reserved|privacy policy|"
    r"terms of service)\b",
    re.I,
)
_DEPARTMENT_WORDS = frozenset({
    "engineering", "product", "sales", "marketing", "operations", "finance",
    "people", "human resources", "hr", "legal", "security", "data", "design",
    "technology", "customer success", "revenue", "research", "strategy",
})
_ORG_SCHEMA_TYPES = frozenset({
    "organization", "corporation", "localbusiness", "ngo", "governmentorganization",
    "educationalorganization", "medicalorganization", "sportsorganization",
    "professionalservice", "store", "restaurant",
})
_LEADERSHIP_RELATION_KEYS = frozenset({
    "employee", "employees", "founder", "founders", "member", "members",
})

_HEADING_SERVICES = re.compile(
    r"^(?:our\s+)?(?:services|what we do|solutions|capabilities|offerings)"
    r"(?:\s+(?:we offer|we provide|overview|include))?\s*:?\s*$",
    re.I,
)

_TEAM_SEGMENTS = ("team", "leadership", "people", "our-team", "management", "founders")
_ABOUT_SEGMENTS = ("about", "about-us", "company", "who-we-are", "our-story", "story")
_CONTACT_SEGMENTS = ("contact", "contact-us", "locations", "offices")
_CAREERS_SEGMENTS = ("careers", "jobs", "join-us", "work-with-us", "open-positions")

_STANDARD_PATHS = {
    "about": ("/about", "/about-us", "/company", "/who-we-are"),
    "team": ("/team", "/leadership", "/about/team", "/people", "/our-team"),
    "contact": ("/contact", "/contact-us", "/locations"),
    "careers": ("/careers", "/jobs", "/join-us"),
}

_JOB_LINK_RE = re.compile(
    r'href=["\']([^"\']*/(?:careers?|jobs?|openings?|positions?)/[a-zA-Z0-9][a-zA-Z0-9_-]{2,}/?)["\']',
    re.I,
)
_NAV_LINK_RE = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>')


class UnsafeURLError(RuntimeError):
    """The target is not a public HTTP(S) destination."""


@dataclass
class FetchedPage:
    url: str
    status: int = 0
    body: str = ""
    content_type: str = ""
    error: str = ""


@dataclass
class SiteFact:
    field: str
    value: Any
    url: str
    excerpt: str
    confidence: str = "high"
    method: str = "reported"


@dataclass
class SiteProfile:
    status: str = "unreachable"
    base_url: str = ""
    facts: list[SiteFact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_domain(value: str) -> str:
    """Bare lowercase host, without scheme, path or leading www."""
    raw = (value or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    raw = raw.split("/")[0].split(":")[0]
    return raw[4:] if raw.startswith("www.") else raw


def _host_allowed(host: str, domain: str) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return bool(host and (host == domain or host.endswith("." + domain)))


def _ip_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    return address.is_global


def _url_with_ip(parsed: Any, ip: str) -> str:
    """The same URL, with its host replaced by the validated connection IP."""
    host = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=host))


def _resolve_public_ips_sync(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURLError(f"dns_error: {host}") from exc
    ips = sorted({info[4][0] for info in infos}, key=lambda ip: (":" in ip, ip))
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise UnsafeURLError(f"non_public_address: {host}")
    return ips


def _validated_target(url: str, domain: str = "") -> tuple[str, str, list[str]]:
    """Validate one target and return its normalized URL, host, and public IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"unsupported_scheme: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise UnsafeURLError("credentials_in_url")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("invalid_port") from exc
    if port not in (None, 80, 443):
        raise UnsafeURLError(f"unsupported_port: {port}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        raise UnsafeURLError(f"invalid_host: {host!r}")
    # IP literals are decided directly: a resolver cannot make 127.0.0.1 or
    # 169.254.169.254 public, and no request may target them.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        # IPv4-mapped IPv6 addresses inherit the safety classification
        # of their embedded IPv4 address.
        mapped = literal.ipv4_mapped if isinstance(literal, ipaddress.IPv6Address) else None
        checked_ip = mapped if mapped is not None else literal
        if not _ip_is_public(str(checked_ip)):
            raise UnsafeURLError(f"non_public_address: {host}")
        ips = [str(literal)]
    else:
        ips = _resolve_public_ips_sync(host)
    if domain and not _host_allowed(host, domain):
        raise UnsafeURLError(f"host_outside_domain: {host}")
    return urlunparse(parsed), host, ips


def validate_url(url: str, *, domain: str = "") -> str:
    """Validate one request target; returns the normalized URL.

    Rejects credentials, non-HTTP schemes, non-standard ports, hosts outside
    ``domain`` (when given), and any hostname that resolves to a non-public
    address. Every redirect hop goes through this again.
    """
    return _validated_target(url, domain)[0]


def _robots_rules(text: str, user_agent: str = "csgresearchbot") -> list[tuple[str, bool]]:
    """Retain the Allow/Disallow rules that apply to this agent."""
    rules: list[tuple[str, bool]] = []
    agents: list[str] = []
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            in_rules = True
            if not any(agent == "*" or agent in user_agent for agent in agents):
                continue
            if value:
                rules.append((value, key == "allow"))
    return rules


def _robots_allows(rules: list[tuple[str, bool]], path: str) -> bool:
    best_len = -1
    allowed = True
    for pattern, is_allow in rules:
        if path.startswith(pattern) and len(pattern) > best_len:
            best_len = len(pattern)
            allowed = is_allow
    return allowed


def parse_robots(text: str, path: str = "/", user_agent: str = "csgresearchbot") -> bool:
    """True when ``path`` is allowed for our agent (conservative rule set)."""
    return _robots_allows(_robots_rules(text, user_agent), path)


def _strip_noncontent(html: str) -> str:
    return re.sub(
        r"(?is)<(script|style|noscript|svg|template|blockquote|figure)\b[^>]*>.*?</\1>",
        " ", html or "",
    )


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"(?s)<[^>]+>", " ", html or "")).split())


def _lines(html: str) -> list[str]:
    return [
        " ".join(unescape(re.sub(r"(?s)<[^>]+>", " ", block)).split())
        for block in re.findall(r"(?is)<(?:h[1-6]|p|li|div|span|td|address)\b[^>]*>.*?</(?:h[1-6]|p|li|div|span|td|address)>", _strip_noncontent(html))
    ]


def _meta_content(html: str, name: str) -> str:
    patterns = (
        rf'(?is)<meta\b[^>]*\bname=["\']{re.escape(name)}["\'][^>]*\bcontent=["\']([^"\']*)["\']',
        rf'(?is)<meta\b[^>]*\bproperty=["\']{re.escape(name)}["\'][^>]*\bcontent=["\']([^"\']*)["\']',
        rf'(?is)<meta\b[^>]*\bcontent=["\']([^"\']*)["\'][^>]*\b(?:name|property)=["\']{re.escape(name)}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html or "")
        if match:
            return " ".join(unescape(match.group(1)).split())
    return ""


def _meta_tag(html: str, name: str) -> str:
    """The verbatim meta tag for ``name`` (name or property), unescaped."""
    for match in re.finditer(r"(?is)<meta\b[^>]*>", html or ""):
        tag = match.group(0)
        if not re.search(
            rf'(?i)\b(?:name|property)=["\']{re.escape(name)}["\']', tag,
        ):
            continue
        return unescape(tag)
    return ""


def _link_href(html: str, rel: str) -> str:
    for match in re.finditer(r"(?is)<link\b[^>]*>", html or ""):
        tag = match.group(0)
        rel_match = re.search(r'(?i)\brel=["\']([^"\']+)["\']', tag)
        href_match = re.search(r'(?i)\bhref=["\']([^"\']+)["\']', tag)
        if rel_match and href_match and rel.lower() in rel_match.group(1).lower().split():
            return unescape(href_match.group(1)).strip()
    return ""


def _json_ld(html: str) -> list[Any]:
    blocks: list[Any] = []
    for match in re.finditer(
        r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html or ""
    ):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    return blocks


def _walk_json(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return " ".join(unescape(match.group(1)).split()) if match else ""


def _detect_technologies(html: str) -> list[str]:
    found: list[str] = []
    for name, patterns in _TECHNOLOGIES.items():
        if any(re.search(pattern, html or "", re.I) for pattern in patterns):
            found.append(name)
    return sorted(found)


def _tech_excerpt(html: str, technologies: list[str]) -> str:
    if not technologies:
        return ""
    fragments = []
    for name, patterns in _TECHNOLOGIES.items():
        if name not in technologies:
            continue
        for pattern in patterns:
            match = re.search(pattern, html or "", re.I)
            if match:
                start = max(0, match.start() - 60)
                fragments.append((html or "")[start:match.end() + 60])
                break
    return " ".join(" ".join(fragment.split()) for fragment in fragments)[:500]


def _is_person_name(value: str) -> bool:
    tokens = [token for token in re.split(r"\s+", (value or "").strip()) if token]
    if not 2 <= len(tokens) <= 3:
        return False
    for token in tokens:
        cleaned = token.strip(",.")
        if not re.fullmatch(r"[A-Z][a-zA-Z'’.-]{1,24}", cleaned):
            return False
        if cleaned.lower().strip(".") in _NON_NAME_WORDS:
            return False
    return True


def _clean_title(value: str) -> str:
    title = " ".join((value or "").split()).strip(" ,;|-–—:")
    return title[:90]


def _excerpt_contains(excerpt: str, *values: str) -> bool:
    text = " ".join((excerpt or "").split()).casefold()
    return bool(text) and all(
        " ".join(value.split()).casefold() in text for value in values if value
    )


def _source_states(text: str, value: str) -> bool:
    """Whole-phrase check for a verbatim source line or structured value."""
    haystack = " ".join((text or "").split()).casefold()
    needle = " ".join((value or "").split()).casefold()
    if not haystack or not needle:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _is_leadership_source(page: "FetchedPage") -> bool:
    """True for team/about/leadership pages, never a general blog or home page."""
    segments = [segment.lower() for segment in urlparse(page.url).path.split("/") if segment]
    if any(segment in _TEAM_SEGMENTS or segment in _ABOUT_SEGMENTS for segment in segments):
        return True
    title = _title(page.body).lower()
    return bool(re.search(
        r"\b(?:leadership|our team|meet the team|management|founders)\b", title,
    ))


def _is_leadership_line_source(page: "FetchedPage") -> bool:
    """Line pairs need an explicit team/leadership context, not just /about."""
    segments = [segment.lower() for segment in urlparse(page.url).path.split("/") if segment]
    if any(segment in _TEAM_SEGMENTS for segment in segments):
        return True
    headings = " ".join(_lines(page.body)[:3])
    text = f"{_title(page.body)} {headings}".lower()
    return bool(re.search(
        r"\b(?:leadership|our team|meet the team|management|founders)\b", text,
    ))


def _is_hq_source(page: "FetchedPage") -> bool:
    path = urlparse(page.url).path.rstrip("/")
    if not path:
        return True
    segments = [segment.lower() for segment in path.split("/") if segment]
    return any(
        segment in _ABOUT_SEGMENTS or segment in _CONTACT_SEGMENTS
        for segment in segments
    )


def _is_nav_or_cta(text: str) -> bool:
    normalized = " ".join((text or "").split()).strip(" \t\n,;|-–—:").lower()
    if not normalized or _claim_is_call_to_action(normalized):
        return True
    return normalized in {
        "read more", "learn more", "view all", "see all", "get started",
        "contact us", "book a demo", "request a demo", "sign up", "subscribe",
        "about", "about us", "careers", "contact", "team", "leadership",
        "privacy", "privacy policy", "terms", "terms of service",
    }


def _company_tokens(company: Company) -> set[str]:
    name = (company.name or "").casefold()
    domain = normalize_domain(company.domain or "").split(".", 1)[0]
    return {
        token for token in re.findall(r"[a-z0-9]+", f"{name} {domain}")
        if len(token) > 2
    }


def _leadership_title_is_invalid(title: str, company: Company) -> bool:
    if not title or _is_nav_or_cta(title) or _LEADERSHIP_FALSE_POSITIVE.search(title):
        return True
    # "CEO, Northwind Traders" is a testimonial attribution when Northwind is a
    # third party. A department ("Director, Engineering") or another role
    # ("CEO, Co-Founder") remains a normal title.
    attribution = re.match(r"^[^,]{2,60},\s*(.+)$", title)
    if attribution:
        suffix = attribution.group(1).strip()
        suffix_l = suffix.casefold()
        if (
            not _ROLE_RE.search(suffix)
            and suffix_l not in _DEPARTMENT_WORDS
            and not (_company_tokens(company) & set(re.findall(r"[a-z0-9]+", suffix_l)))
        ):
            return True
    return False


def _is_organization_node(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(str(value or "").casefold() in _ORG_SCHEMA_TYPES for value in types)


def _walk_json_context(node: Any, parent: Any = None, parent_key: str = ""):
    """Yield every JSON node with the dict that contains it."""
    if isinstance(node, dict):
        yield node, parent, parent_key
        for key, value in node.items():
            yield from _walk_json_context(value, node, str(key))
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json_context(value, parent, parent_key)


def _person_has_org_context(
    person: dict[str, Any], parent: Any, parent_key: str,
) -> bool:
    if isinstance(parent, dict) and _is_organization_node(parent) and parent_key in _LEADERSHIP_RELATION_KEYS:
        return True
    for key in ("worksFor", "affiliation", "memberOf"):
        value = person.get(key)
        values = value if isinstance(value, list) else [value]
        if any(isinstance(item, dict) and _is_organization_node(item) for item in values):
            return True
    return False


def _address_parts(address: dict[str, Any]) -> dict[str, str]:
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("addressCountry")
    return {
        key: str(address.get(key) or "").strip()
        for key in ("addressLocality", "addressRegion", "addressCountry")
    } | ({"country_name": str(country or "").strip()} if country else {})


def _fact(
    field: str, value: Any, url: str, excerpt: str, *,
    structured: bool = False, **kwargs: Any,
) -> Optional[SiteFact]:
    """Build a fact only when the retrieved source states the value.

    ``structured`` is used for JSON-LD/verbatim line pairs where a legitimate
    short value ("Austin, TX") must still be exact. General prose keeps the
    model path's sentence-length negation-aware check.
    """
    if not url or not excerpt or _text_is_malformed(excerpt):
        return None
    if _has_malformed_text(value):
        return None
    values = value.values() if isinstance(value, dict) else [value]
    if any(
        _claim_is_call_to_action(str(part))
        for part in values if isinstance(part, str) and part
    ):
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        if structured:
            if not _source_states(excerpt, value):
                return None
        elif not _text_supports_value(value, excerpt):
            return None
    elif isinstance(value, dict):
        if not value:
            return None
        if structured:
            if not all(_source_states(excerpt, str(part)) for part in values if part):
                return None
        elif not all(
            _text_supports_value(str(part), excerpt) for part in values if part
        ):
            return None
    elif isinstance(value, list):
        if not value:
            return None
    return SiteFact(field=field, value=value, url=url, excerpt=excerpt[:1200], **kwargs)


class SafeSiteFetcher:
    """HTTP client for public company sites.

    Every hop is DNS-validated and the request connects to that exact public
    IP, while HTTP Host and TLS SNI keep the original hostname. This closes the
    validation-to-connect DNS rebinding window. Redirects are followed
    manually so each target is validated; bodies are capped; a host is
    serialized with a delay.
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        per_host_delay: float = PER_HOST_DELAY_SECONDS,
        max_concurrency: int = 4,
        transport: Any = None,
    ) -> None:
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self._sem = asyncio.Semaphore(max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )

    async def __aenter__(self) -> "SafeSiteFetcher":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, *, max_bytes: int = MAX_BYTES, domain: str = "") -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                current, hostname, ips = await asyncio.to_thread(
                    _validated_target, current, domain,
                )
            except UnsafeURLError as exc:
                return FetchedPage(url=current, error=str(exc))
            host = urlparse(current).netloc.lower()
            parsed = urlparse(current)
            pinned = _url_with_ip(parsed, ips[0])
            lock = self._host_locks.setdefault(host, asyncio.Lock())
            async with self._sem:
                async with lock:
                    gap = time.monotonic() - self._host_last.get(host, 0.0)
                    if gap < self.per_host_delay:
                        await asyncio.sleep(self.per_host_delay - gap)
                    try:
                        extensions = (
                            {"sni_hostname": hostname}
                            if parsed.scheme == "https" else {}
                        )
                        request = self._client.build_request(
                            "GET", pinned, headers={"Host": hostname},
                            extensions=extensions or None,
                        )
                        response = await self._client.send(request, stream=True)
                        try:
                            if response.status_code in (301, 302, 303, 307, 308):
                                location = response.headers.get("location", "")
                                if not location:
                                    return FetchedPage(url=current, status=response.status_code)
                                current = urljoin(current, location)
                                continue
                            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                            if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
                                return FetchedPage(url=current, status=response.status_code,
                                                   content_type=content_type, error="invalid_content_type")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body += chunk
                                if len(body) > max_bytes:
                                    return FetchedPage(url=current, status=response.status_code,
                                                       content_type=content_type, error="too_large")
                            encoding = response.charset_encoding or "utf-8"
                            try:
                                text = bytes(body).decode(encoding, errors="replace")
                            except LookupError:
                                text = bytes(body).decode("utf-8", errors="replace")
                            return FetchedPage(url=current, status=response.status_code,
                                               body=text, content_type=content_type)
                        finally:
                            await response.aclose()
                    except (httpx.HTTPError, ValueError) as exc:
                        return FetchedPage(url=current, error=f"transport_error: {type(exc).__name__}")
                    finally:
                        self._host_last[host] = time.monotonic()
        return FetchedPage(url=current, error="too_many_redirects")


def _challenged(page: FetchedPage) -> bool:
    if page.status in (401, 403, 429, 503):
        return True
    lowered = (page.body or "").lower()
    markers = ("just a moment", "attention required", "cf-chl", "captcha", "access denied", "sgcaptcha")
    return any(marker in lowered for marker in markers)


def _pick_links(html: str, base_url: str, domain: str) -> dict[str, str]:
    """Navigate the site's own menu, returning one link per section."""
    found: dict[str, str] = {}
    for match in _NAV_LINK_RE.finditer(html or ""):
        href, anchor = match.group(1).strip(), _text(match.group(2))
        absolute = urljoin(base_url + "/", href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not _host_allowed(parsed.hostname or "", domain):
            continue
        segments = [segment.lower() for segment in parsed.path.split("/") if segment]
        anchor_l = anchor.lower()
        if any(segment in _TEAM_SEGMENTS for segment in segments) or anchor_l in {
            "team", "leadership", "our team", "people", "management", "founders",
        }:
            found.setdefault("team", absolute)
        elif any(segment in _CONTACT_SEGMENTS for segment in segments) or anchor_l.startswith("contact"):
            found.setdefault("contact", absolute)
        elif any(segment in _CAREERS_SEGMENTS for segment in segments) or "career" in anchor_l:
            found.setdefault("careers", absolute)
        elif any(segment in _ABOUT_SEGMENTS for segment in segments) or anchor_l in {
            "about", "about us", "company", "who we are", "our story",
        }:
            found.setdefault("about", absolute)
    return found


def _extract_description(home: FetchedPage) -> Optional[SiteFact]:
    for key in ("description", "og:description", "twitter:description"):
        content = _meta_content(home.body, key)
        source = _meta_tag(home.body, key)
        if 40 <= len(content) <= 400 and source:
            fact = _fact("description", content, home.url, source)
            if fact:
                return fact
    return None


def _extract_website(home: FetchedPage, domain: str) -> Optional[SiteFact]:
    canonical = _link_href(home.body, "canonical")
    source = canonical
    if not canonical:
        canonical = _meta_content(home.body, "og:url")
        source = _meta_tag(home.body, "og:url")
    if not canonical or not source:
        return None
    absolute = urljoin(home.url, canonical)
    parsed = urlparse(absolute)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username or parsed.password
        or port not in (None, 80, 443)
        or not _host_allowed(parsed.hostname or "", domain)
        or _text_is_malformed(canonical)
    ):
        return None
    value = urlunparse(parsed._replace(fragment=""))
    # The canonical/og:url attribute is the verbatim source; no DNS lookup is
    # needed or wanted for a value that is only stored, not fetched.
    return SiteFact(
        field="official_website", value=value, url=home.url, excerpt=source[:1200],
    )


def _extract_headquarters(pages: list[FetchedPage]) -> Optional[SiteFact]:
    for page in pages:
        if not _is_hq_source(page):
            continue
        for node in _walk_json(_json_ld(page.body)):
            if not _is_organization_node(node):
                continue
            for key in ("address", "location"):
                address = node.get(key)
                if not isinstance(address, dict):
                    continue
                parts = _address_parts(address)
                city, region, country = parts.get("addressLocality", ""), parts.get("addressRegion", ""), parts.get("country_name", "")
                if not city or not region:
                    continue
                value = {"city": city, "state": region}
                if country:
                    value["country"] = country
                excerpt = json.dumps(address, ensure_ascii=False, sort_keys=True, default=str)
                fact = _fact(
                    "headquarters", value, page.url, excerpt, structured=True,
                )
                if fact:
                    return fact
        for match in re.finditer(r"(?is)<address\b[^>]*>(.*?)</address>", page.body):
            address_text = _text(match.group(1))
            city_match = re.search(r"([A-Z][A-Za-z .'-]{2,40}),\s*([A-Z]{2})\b", address_text)
            if not city_match:
                continue
            fact = _fact(
                "headquarters",
                {"city": city_match.group(1).strip(), "state": city_match.group(2)},
                page.url,
                address_text,
                structured=True,
            )
            if fact:
                return fact
    return None


def _people_from_schema(pages: list[FetchedPage], company: Company) -> list[SiteFact]:
    facts: list[SiteFact] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        if not _is_leadership_source(page):
            continue
        for block in _json_ld(page.body):
            for node, parent, parent_key in _walk_json_context(block):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "Person" not in types:
                    continue
                if not _person_has_org_context(node, parent, parent_key):
                    continue
                name = str(node.get("name") or "").strip()
                title = _clean_title(str(node.get("jobTitle") or ""))
                if not _is_person_name(name) or not title or not _ROLE_RE.search(title):
                    continue
                if _leadership_title_is_invalid(title, company):
                    continue
                key = (name.casefold(), title.casefold())
                if key in seen:
                    continue
                seen.add(key)
                fact = _fact(
                    "leadership", {"name": name, "title": title}, page.url,
                    json.dumps(node, ensure_ascii=False, sort_keys=True, default=str),
                    structured=True, confidence="high",
                )
                if fact:
                    facts.append(fact)
    return facts


def _people_from_lines(pages: list[FetchedPage], company: Company) -> list[SiteFact]:
    facts: list[SiteFact] = []
    seen: set[str] = set()
    for page in pages:
        if not _is_leadership_line_source(page):
            continue
        lines = [line for line in _lines(page.body) if line]
        for index, line in enumerate(lines):
            if (
                not _is_person_name(line) or line.casefold() in seen
                or _is_nav_or_cta(line)
            ):
                continue
            for candidate in lines[index + 1:index + 4]:
                title = _clean_title(candidate)
                if not title or len(title) > 90 or not _ROLE_RE.search(title):
                    continue
                if (
                    re.search(r"\b(?:said|says|testimonial|customer|client)\b", title, re.I)
                    or _leadership_title_is_invalid(title, company)
                ):
                    break
                seen.add(line.casefold())
                fact = _fact(
                    "leadership", {"name": line, "title": title}, page.url,
                    f"{line}\n{title}", structured=True, confidence="medium",
                )
                if fact:
                    facts.append(fact)
                break
    return facts


def _extract_services(pages: list[FetchedPage]) -> Optional[SiteFact]:
    for page in pages:
        if any(
            segment.lower() in _CAREERS_SEGMENTS
            for segment in urlparse(page.url).path.split("/") if segment
        ):
            continue
        lines = [line for line in _lines(page.body) if 2 <= len(line) <= 120]
        for index, line in enumerate(lines):
            if not _HEADING_SERVICES.match(line):
                continue
            items: list[str] = []
            for candidate in lines[index + 1:index + 9]:
                if _HEADING_SERVICES.match(candidate):
                    break
                if (
                    3 <= len(candidate) <= 90
                    and not _ROLE_RE.search(candidate)
                    and not _is_nav_or_cta(candidate)
                    and not _text_is_malformed(candidate)
                ):
                    items.append(candidate)
                if len(items) >= 4:
                    break
            if len(items) < 2:
                continue
            value = "; ".join(items[:3])[:240]
            excerpt = "\n".join([line, *items])
            if all(_source_states(excerpt, item) for item in items[:3]):
                return SiteFact(
                    field="services", value=value, url=page.url, excerpt=excerpt[:1200],
                )
    return None


def _extract_careers(html_pages: list[FetchedPage], domain: str) -> dict[str, Any]:
    careers_url = ""
    links = _pick_links(html_pages[0].body if html_pages else "", html_pages[0].url if html_pages else "", domain)
    careers_url = links.get("careers", "")
    job_links: list[str] = []
    for page in html_pages:
        for href in _JOB_LINK_RE.findall(page.body):
            absolute = urljoin(page.url, href)
            if _host_allowed(urlparse(absolute).hostname or "", domain) and absolute not in job_links:
                job_links.append(absolute)
    return {
        "careers_url": careers_url,
        "job_link_count": len(job_links),
        "job_links": job_links[:5],
    }


def extract_facts(company: Company, domain: str, pages: list[FetchedPage]) -> list[SiteFact]:
    """Extract every fact the official pages state directly."""
    if not pages:
        return []
    home = pages[0]
    facts: list[SiteFact] = []
    for fact in (
        _extract_website(home, domain),
        _extract_description(home),
        _extract_headquarters(pages),
        _extract_services(pages),
    ):
        if fact:
            facts.append(fact)
    facts.extend(_people_from_schema(pages, company))
    facts.extend(_people_from_lines(pages, company))
    return facts


def _dedupe_facts(facts: list[SiteFact]) -> list[SiteFact]:
    unique: list[SiteFact] = []
    seen: set[tuple] = set()
    for fact in facts:
        key = (fact.field, json.dumps(fact.value, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)
    return unique


async def _resolve_site(
    fetcher: Any, domain: str,
) -> tuple[Optional[FetchedPage], str, list[dict[str, Any]], list[tuple[str, bool]]]:
    attempts: list[dict[str, Any]] = []
    for base in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        robots = await fetcher.fetch(base + "/robots.txt", max_bytes=MAX_ROBOTS_BYTES, domain=domain)
        robots_status = "missing" if robots.status == 404 else "error" if robots.error else "present"
        rules = _robots_rules(robots.body) if robots.status == 200 and robots.body else []
        if rules and not _robots_allows(rules, "/"):
            attempts.append({"url": base, "status": "blocked_by_robots"})
            continue
        page = await fetcher.fetch(base, domain=domain)
        attempts.append({"url": base, "status": page.status or page.error or "error"})
        if page.error:
            continue
        if _challenged(page):
            attempts[-1]["status"] = "challenged"
            return None, "challenged", attempts, rules
        if page.status == 200 and page.body:
            return page, robots_status, attempts, rules
    return None, "unreachable", attempts, []


async def profile_official_site(
    company: Company,
    *,
    fetcher: Any = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Fetch and extract official-site facts, appending evidence to ``company``.

    Returns the retrieval metadata that callers store under
    ``research_outcomes["_site_profile"]``. Returns ``None`` only when the
    company has no usable domain.
    """
    domain = normalize_domain(company.domain)
    if not domain or "." not in domain:
        return None
    now = now or datetime.now(timezone.utc)
    retrieved_at = now.isoformat()
    own_fetcher = fetcher is None
    fetcher = fetcher or SafeSiteFetcher()

    metadata: dict[str, Any] = {
        "status": "unreachable",
        "extractor": EXTRACTOR_VERSION,
        "retrieved_at": retrieved_at,
        "pages": [],
        "fields": {},
    }
    try:
        home, robots_status, attempts, robots_rules = await _resolve_site(fetcher, domain)
        blocked = bool(attempts) and all(
            attempt.get("status") == "blocked_by_robots" for attempt in attempts
        )
        metadata["robots"] = "disallowed" if blocked else robots_status
        metadata["attempts"] = attempts
        if home is None:
            metadata["status"] = "challenged" if robots_status == "challenged" else (
                "blocked_by_robots" if blocked else "unreachable"
            )
            for field_name in ("official_website", "description", "headquarters", "leadership", "services"):
                metadata["fields"][field_name] = "site_unreachable"
            return metadata

        base_url = home.url
        metadata["base_url"] = base_url
        links = _pick_links(home.body, base_url, domain)
        pages: list[FetchedPage] = [home]
        metadata["pages"].append({"url": home.url, "status": home.status, "kind": "home"})

        wanted: list[tuple[str, str]] = []
        for kind in ("about", "team", "contact", "careers"):
            if links.get(kind):
                wanted.append((kind, links[kind]))
        for kind in ("about", "team", "contact"):
            if not any(existing == kind for existing, _ in wanted):
                for path in _STANDARD_PATHS[kind]:
                    candidate = base_url.rstrip("/") + path
                    if not any(url == candidate for _, url in wanted):
                        wanted.append((kind, candidate))
                        break
        for kind, url in wanted:
            if len(pages) >= MAX_PAGES:
                break
            if not _robots_allows(robots_rules, urlparse(url).path or "/"):
                metadata["pages"].append({
                    "url": url, "status": "blocked_by_robots", "kind": kind,
                })
                continue
            page = await fetcher.fetch(url, domain=domain)
            metadata["pages"].append({"url": url, "status": page.status or page.error or "error", "kind": kind})
            if page.status == 200 and page.body and not _challenged(page):
                pages.append(page)

        facts = _dedupe_facts(extract_facts(company, domain, pages))
        careers = _extract_careers(pages, domain)
        if careers.get("careers_url") and not company.careers_url:
            # A first-party careers URL discovered on the official site is
            # operational provenance for the ATS/jobs stage, not a researched
            # field; it must never overwrite a link already supplied by a
            # stronger source.
            company.careers_url = careers["careers_url"]
        technologies = _detect_technologies(home.body)
        metadata.update({
            "status": "ok",
            "careers_url": careers["careers_url"],
            "job_link_count": careers["job_link_count"],
            "technologies": technologies,
        })
        applied = _apply_facts(company, facts, retrieved_at=retrieved_at)
        _append_informational_evidence(
            company, home.url, careers, technologies, home.body, retrieved_at
        )

        metadata["fields"] = {
            fact.field: ("accepted" if fact.field in applied else "already_verified")
            for fact in facts
        }
        for field_name in ("official_website", "description", "headquarters", "leadership", "services"):
            metadata["fields"].setdefault(field_name, "missing_on_site")
        return metadata
    except Exception as exc:  # a site failure must never break enrichment
        metadata["status"] = "error"
        metadata["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return metadata
    finally:
        if own_fetcher:
            await fetcher.__aexit__(None, None, None)


def _outcome_is_accepted(company: Company, field_name: str) -> bool:
    outcome = company.research_outcomes.get(field_name)
    return isinstance(outcome, dict) and outcome.get("status") == "accepted_and_mapped"


def _value_present(company: Company, field_name: str) -> bool:
    if field_name == "official_website":
        return bool(company.website and company.website.value)
    if field_name == "headquarters":
        return bool(company.headquarters and company.headquarters.value)
    if field_name == "description":
        return bool((company.description or "").strip())
    # leadership and services are stored as evidence only, so an accepted
    # outcome (checked by the caller) is the only thing that reserves them.
    return False


def _apply_facts(company: Company, facts: list[SiteFact], *, retrieved_at: str) -> set[str]:
    applied: set[str] = set()
    for fact in facts:
        criterion = _FIELD_CRITERIA.get(fact.field)
        if criterion is None:
            continue
        if _outcome_is_accepted(company, fact.field) or _value_present(company, fact.field):
            continue
        claim = {fact.field: fact.value}
        evidence_key = _evidence_key(fact.field, criterion, claim, fact.url, fact.excerpt)
        if not any(
            isinstance(item, dict) and item.get("evidence_key") == evidence_key
            for item in company.research_evidence
        ):
            company.research_evidence.append({
                "evidence_key": evidence_key,
                "field": fact.field,
                "criterion": criterion,
                "claim": claim,
                "source_name": f"{company.name or company.domain} — official website",
                "source_url": fact.url,
                "excerpt": fact.excerpt,
                "evidence_type": "direct",
                "confidence": fact.confidence,
                "source_tier": 1,
                "verification_method": SOURCE_NAME,
                "retrieved_at": retrieved_at,
            })
        acceptance = {
            "status": "accepted_and_mapped",
            "criterion": criterion,
            "checked_at": retrieved_at,
            "reason": "stated on the company's own official page",
            "verification": SOURCE_NAME,
        }
        previous = company.research_outcomes.get(fact.field)
        if isinstance(previous, dict):
            acceptance = {**previous, **acceptance}
            previous_keys = previous.get("evidence_keys")
            keys = list(previous_keys) if isinstance(previous_keys, list) else []
            if evidence_key not in keys:
                keys.append(evidence_key)
            acceptance["evidence_keys"] = keys
            if previous.get("status") == "found_but_rejected" and previous.get("reason"):
                acceptance["previous_rejection"] = previous["reason"]
        company.research_outcomes[fact.field] = acceptance
        if fact.field == "official_website" and not company.website:
            company.website = ProvenanceValue(
                value=fact.value, source=SOURCE_NAME, url=fact.url,
                fetched_at=retrieved_at, method="reported", confidence="high",
            )
        elif fact.field == "headquarters" and not company.headquarters:
            company.headquarters = ProvenanceValue(
                value=fact.value, source=SOURCE_NAME, url=fact.url,
                fetched_at=retrieved_at, method="reported", confidence="high",
            )
        elif fact.field == "description" and not (company.description or "").strip():
            company.description = str(fact.value)
        applied.add(fact.field)
    return applied


def _append_informational_evidence(
    company: Company,
    home_url: str,
    careers: dict[str, Any],
    technologies: list[str],
    home_html: str,
    retrieved_at: str,
) -> None:
    """Store non-criteria facts (careers, tech) without claiming an outcome."""
    additions = []
    if careers.get("careers_url"):
        additions.append({
            "field": "careers_page",
            "claim": {"careers_page": careers["careers_url"]},
            "source_url": careers["careers_url"],
            "excerpt": careers["careers_url"],
        })
    if careers.get("job_link_count"):
        additions.append({
            "field": "job_links",
            "claim": {"job_link_count": careers["job_link_count"]},
            "source_url": careers.get("careers_url") or home_url,
            "excerpt": "; ".join(careers.get("job_links") or [])[:500],
        })
    if technologies:
        excerpt = _tech_excerpt(home_html, technologies)
        if excerpt:
            additions.append({
                "field": "technology_stack",
                "claim": {"technologies": technologies},
                "source_url": home_url,
                "excerpt": excerpt,
            })
    for addition in additions:
        if any(
            isinstance(item, dict)
            and item.get("field") == addition["field"]
            and item.get("claim") == addition["claim"]
            for item in company.research_evidence
        ):
            continue
        company.research_evidence.append({
            **addition,
            "evidence_key": _evidence_key(
                addition["field"], "informational", addition["claim"],
                addition["source_url"], addition["excerpt"],
            ),
            "criterion": "informational",
            "source_name": f"{company.name or company.domain} — official website",
            "evidence_type": "direct",
            "confidence": "high",
            "source_tier": 1,
            "verification_method": SOURCE_NAME,
            "informational": True,
            "retrieved_at": retrieved_at,
        })
