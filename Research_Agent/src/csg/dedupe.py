"""Global company identity: one normalized key per company, shared by every run.

`companies.domain` is the primary key, so two rows can only coexist if their
domain *strings* differ -- `Foo.com`, `https://www.foo.com/careers` and
`foo.com:443` are the same company but different strings. Everything that
decides "is this the same company?" goes through here so there is exactly one
definition of it:

  1. normalized domain  (primary key; scheme/www/port/path/case/trailing dot
                         all stripped)
  2. normalized name    (fallback only: exact match after stripping case,
                         accents, punctuation and corporate suffixes, and only
                         for names long enough to be distinctive)

Pure functions plus a read-only index over `companies`; no writes happen here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional

_PORT = re.compile(r":\d+$")
_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "group", "holdings", "plc", "lp", "llp",
})
# A name key shorter than this is too generic to identify a company on its own.
MIN_NAME_KEY_LEN = 5


def normalize_domain(raw: Optional[str]) -> str:
    """Canonical bare host: lowercase, no scheme/userinfo/port/path/query/
    fragment/trailing dot/leading `www.`. Returns "" for empty input."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    s = s.rsplit("@", 1)[-1]
    s = _PORT.sub("", s)
    s = s.strip(".")
    if s.startswith("www."):
        s = s[4:]
    return s


def normalize_name(name: Optional[str]) -> str:
    """Comparison key for a company name, or "" if too short/generic to use."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("&", " and ")
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _NAME_SUFFIXES]
    key = "".join(tokens)
    return key if len(key) >= MIN_NAME_KEY_LEN else ""


@dataclass(frozen=True)
class Match:
    domain: str       # the domain as stored in `companies` (what FKs point at)
    matched_by: str   # "domain" | "name"


class ExistingCompanies:
    """Read-only snapshot of every stored company, indexed for lookup. Built
    once per discovery run (one query) rather than one query per candidate."""

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        self._by_domain: dict[str, str] = {}
        self._by_name: dict[str, str] = {}
        for r in sorted(rows, key=lambda r: r["domain"]):
            d = r["domain"]
            self._by_domain.setdefault(normalize_domain(d), d)
            key = normalize_name(r.get("name"))
            if key:
                self._by_name.setdefault(key, d)

    @classmethod
    def load(cls, conn: Any) -> "ExistingCompanies":
        return cls(conn.select("companies", select="domain,name"))

    def lookup(self, domain: Optional[str], name: Optional[str] = None) -> Optional[Match]:
        hit = self._by_domain.get(normalize_domain(domain))
        if hit is not None:
            return Match(hit, "domain")
        key = normalize_name(name)
        if key and key in self._by_name:
            return Match(self._by_name[key], "name")
        return None


def find_duplicate_groups(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read-only audit: groups of stored companies that look like the same
    company. `kind` is "domain" (same normalized domain, different stored
    string) or "name" (same normalized name, different domains)."""
    rows = list(rows)
    groups: list[dict[str, Any]] = []

    by_domain: dict[str, set[str]] = {}
    for r in rows:
        by_domain.setdefault(normalize_domain(r["domain"]), set()).add(r["domain"])
    for key, domains in sorted(by_domain.items()):
        if len(domains) > 1:
            groups.append({"kind": "domain", "key": key, "domains": sorted(domains)})

    by_name: dict[str, set[str]] = {}
    for r in rows:
        key = normalize_name(r.get("name"))
        if key:
            by_name.setdefault(key, set()).add(r["domain"])
    for key, domains in sorted(by_name.items()):
        if len({normalize_domain(d) for d in domains}) > 1:
            groups.append({"kind": "name", "key": key, "domains": sorted(domains)})
    return groups
