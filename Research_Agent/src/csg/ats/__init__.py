"""ATS detection and req fetching.

Eight providers: greenhouse, lever, ashby, workday, smartrecruiters,
workable, bamboohr, rippling. Adding a new one means: a provider module with
verify()/fetch(), an entry in ADAPTERS, a BOARD_LINK_PATTERNS regex (if it
has a stable board URL to recognize in HTML), and PROBEABLE_PROVIDERS only
if a guessed token can be verified with a trustworthy "not found" signal
(see smartrecruiters.py for why it is deliberately excluded from probing
despite having an adapter).

Detection runs three-pronged, highest precision first:
  1. careers_page -- fetch the site (apex domain, common /careers-style
     paths) and look for a real ATS board link. Reliable because the token
     is read, not guessed.
       1a. Some sites' top-level careers page is only a landing page (e.g. a
           big "Explore open positions" button) with the actual board link
           one click deeper -- when no board link is found directly,
           same-domain links out of the page whose path looks like a
           listing ("open-positions", "current-openings", ...) are followed
           one hop and scanned the same way (see _candidate_subpages).
       1c. If nothing on the apex domain (or its one-hop subpages) has a
           board link, a few common careers subdomains (careers.{domain},
           jobs.{domain}) are tried the same way -- some companies host
           their whole careers site there instead of under an apex-domain
           path, which 1/1a never reaches (see CAREERS_SUBDOMAINS).
  2. token_probe  -- derive candidate tokens from the domain/name and probe each
     provider API. Lower hit rate, catches sites that render careers via JS.
     Not applicable to Workday (its shard, e.g. wd1/wd3, cannot be guessed
     from the domain/name, only read off an actual career-site link) or
     SmartRecruiters (its API has no reliable "not found" response -- see
     smartrecruiters.py).

Expect roughly 30-40% of an arbitrary company list to resolve on GH/Lever/
Ashby/Workable/BambooHR alone; Workday and SmartRecruiters add coverage for
larger enterprises but only via a real board link (careers_page), never a
guess. Some sites render careers entirely via client-side JS with no static
link to find anywhere -- those remain undetectable without a JS-rendering
fetcher, which this project deliberately does not run (see http.py); this is
why several other major providers remain unsupported despite being common:
  - iCIMS, Taleo, Jobvite: no reliable public, unauthenticated API found on
    research, and (for Taleo/Jobvite) no confirmable live tenant to even
    validate a URL pattern against -- unlike every supported provider here,
    none of these three could be verified against real data.
  - Phenom (a large, growing share of enterprise career sites, e.g. CVS
    Health, United Airlines): entirely client-side JS-rendered with no
    static HTML or API signal, confirmed by inspecting a real live tenant.
  - Paycom: same "no stable public job API" conclusion as the others (see
    the ats-adapter research note in project history).

NOTE ON TERMS OF SERVICE: Workday's Site Terms explicitly prohibit automated
scraping of Workday-hosted sites. This adapter is included as a deliberate
scope decision, not an oversight -- see workday.py's module docstring.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from ..http import Fetcher
from ..models import AtsProvider, JobPosting
from . import ashby, bamboohr, greenhouse, lever, rippling, smartrecruiters, workable, workday

ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "bamboohr": bamboohr,
    "rippling": rippling,
}

# Providers whose token can be guessed from the domain/name and probed
# directly against the provider's API. Excluded:
#   - workday: its shard (wd1, wd3, ...) has no derivable relationship to the
#     domain/company name.
#   - smartrecruiters: its API returns 200 for ANY token, real or not (see
#     smartrecruiters.py's module docstring) -- there is no reliable
#     "not found" signal to probe against, so it is only ever detected via a
#     board link actually found on the company's own site (careers_page).
PROBEABLE_PROVIDERS = ("greenhouse", "lever", "ashby", "workable", "bamboohr", "rippling")

# Board links as they appear in page HTML. Single capture group = token.
BOARD_LINK_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "greenhouse": [
        re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)", re.I),
        re.compile(r"boards\.eu\.greenhouse\.io/([a-zA-Z0-9_-]+)", re.I),
    ],
    "lever": [re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-zA-Z0-9_-]+)", re.I)],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_.-]+)", re.I)],
    "smartrecruiters": [
        re.compile(r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I),
        re.compile(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I),
    ],
    "workable": [
        re.compile(r"apply\.workable\.com/([a-zA-Z0-9_-]+)", re.I),
        re.compile(r"([a-zA-Z0-9_-]+)\.workable\.com", re.I),
    ],
    "bamboohr": [re.compile(r"([a-zA-Z0-9_-]+)\.bamboohr\.com", re.I)],
    "rippling": [re.compile(r"ats\.rippling\.com/([a-zA-Z0-9_-]+)/jobs", re.I)],
}

CAREERS_PATHS = ["", "/careers", "/careers/", "/jobs", "/about/careers", "/company/careers", "/join-us"]

# Some companies host their entire careers site off the apex domain rather
# than under a path on it (e.g. careers.acme.com instead of acme.com/careers)
# -- CAREERS_PATHS alone never reaches these. Tried only as a fallback (see
# detect()) since most companies use a path, not a subdomain, and this adds
# extra requests per miss.
CAREERS_SUBDOMAINS = ["careers", "jobs"]

# Tokens that show up in board URLs but are never a company board.
TOKEN_BLOCKLIST = {"embed", "job_board", "for", "www", "api", "jobs", "careers"}

# Some sites' top-level careers page is just a landing page with a button to a
# *second* page (e.g. datarobot.com/careers -> /careers/open-positions/) where
# the actual ATS link/postings live -- the board is never linked from the
# landing page itself. Rather than hardcode every such second-hop path per
# company, follow same-domain links out of an already-fetched careers page
# whose own path/text looks like it leads to a listing, and scan those pages
# too. Generic (any company with this landing-page-then-listing pattern
# benefits, not just Workday sites), one hop deep to keep this bounded.
_SUBPAGE_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_SUBPAGE_HINT_RE = re.compile(r"(open[-_]?positions|job[-_]?listings?|current[-_]?openings|all[-_]?jobs|view[-_]?jobs)", re.I)
MAX_SUBPAGES = 3


def _candidate_subpages(base_url: str, body: str) -> list[str]:
    """Same-domain links out of `body` whose path looks like a job listing
    page, not the board link itself (that's what BOARD_LINK_PATTERNS/
    workday.LINK_RE already catch) -- e.g. "/careers/open-positions/"."""
    domain = urlparse(base_url).netloc
    out: list[str] = []
    seen: set[str] = set()
    for href in _SUBPAGE_LINK_RE.findall(body):
        if not _SUBPAGE_HINT_RE.search(href):
            continue
        url = href if "://" in href else f"https://{domain}{href if href.startswith('/') else '/' + href}"
        if urlparse(url).netloc != domain:
            continue  # off-domain link (e.g. straight to the ATS already) -- nothing to follow
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= MAX_SUBPAGES:
            break
    return out


def normalize_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = urlparse(s).netloc
    s = s.split("/")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def candidate_tokens(domain: str, name: Optional[str]) -> list[str]:
    """Plausible board tokens, most likely first."""
    root = domain.split(".")[0] if domain else ""
    out: list[str] = []
    if root:
        out += [root, root.replace("-", "")]
    if name:
        slug = re.sub(r"[^a-z0-9]+", "", name.lower())
        hyph = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        # Drop common corporate suffixes before slugging.
        stripped = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings)\b", "", name.lower())
        stripped_slug = re.sub(r"[^a-z0-9]+", "", stripped)
        out += [slug, hyph, stripped_slug]
    seen: set[str] = set()
    result: list[str] = []
    for t in out:
        if t and t not in seen and t not in TOKEN_BLOCKLIST:
            seen.add(t)
            result.append(t)
    return result


async def _scan_body_for_board(
    fetcher: Fetcher, url: str, body: str
) -> Optional[tuple[AtsProvider, str, str, Optional[str]]]:
    """Look for a verifiable ATS board link directly in one fetched page's
    HTML. Shared by the top-level careers pages and the one-hop subpages
    found within them (see _candidate_subpages) so both scan identically."""
    for provider, patterns in BOARD_LINK_PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(body):
                token = m.group(1)
                if token.lower() in TOKEN_BLOCKLIST:
                    continue
                if await ADAPTERS[provider].verify(fetcher, token):
                    return provider, token, "careers_page", url

    for m in workday.LINK_RE.finditer(body):
        tenant, shard, site = m.group(1), m.group(2), m.group(3)
        token = workday.encode_token(tenant, shard, site)
        if await workday.verify(fetcher, token):
            return "workday", token, "careers_page", url

    return None


async def _scan_urls(
    fetcher: Fetcher, urls: list[str]
) -> tuple[Optional[tuple[AtsProvider, str, str, Optional[str]]], list[str]]:
    """Fetch and scan each URL for a board link; on a miss, collect
    same-domain listing-shaped subpages found in it. Returns as soon as one
    URL resolves, so a later URL's subpages are never collected once found."""
    subpages: list[str] = []
    for url in urls:
        status, body = await fetcher.get(url)
        if status != 200 or not body:
            continue

        found = await _scan_body_for_board(fetcher, url, body)
        if found:
            return found, subpages

        for sub in _candidate_subpages(url, body):
            if sub not in subpages:
                subpages.append(sub)
    return None, subpages


async def detect(
    fetcher: Fetcher, domain: str, name: Optional[str] = None
) -> Optional[tuple[AtsProvider, str, str, Optional[str]]]:
    """Return (provider, token, detected_by, careers_url) or None."""
    # --- 1. careers page scan, on the apex domain ---
    apex_urls = [f"https://{domain}{path}" for path in CAREERS_PATHS]
    found, subpages = await _scan_urls(fetcher, apex_urls)
    if found:
        return found

    # --- 1b. one hop into whatever open-positions-looking subpages we found ---
    found, _ = await _scan_urls(fetcher, subpages[:MAX_SUBPAGES])
    if found:
        return found

    # --- 1c. some companies host their whole careers site off the apex
    # domain (careers.acme.com, jobs.acme.com) rather than under a path on
    # it -- CAREERS_PATHS alone never reaches these. Only tried once the
    # apex domain has come up empty, since most companies use a path.
    # `domain` here is always the registrable domain a company was
    # discovered under (normalize_domain strips "www."), so prepending is
    # correct even for a multi-label public suffix like "acme.co.uk" --
    # there is no second label to (wrongly) strip off first. ---
    subdomain_urls = [f"https://{sub}.{domain}" for sub in CAREERS_SUBDOMAINS if not domain.startswith(f"{sub}.")]
    found, subdomain_subpages = await _scan_urls(fetcher, subdomain_urls)
    if found:
        return found
    found, _ = await _scan_urls(fetcher, subdomain_subpages[:MAX_SUBPAGES])
    if found:
        return found

    # --- 2. token probe (not applicable to Workday -- shard is unguessable) ---
    for token in candidate_tokens(domain, name):
        for provider in PROBEABLE_PROVIDERS:
            if await ADAPTERS[provider].verify(fetcher, token):
                return provider, token, "token_probe", None

    return None


async def fetch_postings(
    fetcher: Fetcher, domain: str, provider: AtsProvider, token: str
) -> list[JobPosting]:
    return await ADAPTERS[provider].fetch(fetcher, domain, token)
