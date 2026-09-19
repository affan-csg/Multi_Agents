"""Config loading + compiled regex taxonomy.

Patterns in icp.yaml are compiled once here. A bare phrase like
"software engineer" is matched literally; a token wrapped in \\b...\\b is
treated as a regex, which is how short ambiguous acronyms (RN, CNA, SRE)
avoid matching inside unrelated words.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "icp.yaml"

load_dotenv(PROJECT_ROOT / ".env")


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for p in patterns:
        if "\\b" in p or p.startswith("^"):
            out.append(re.compile(p, re.I))
        else:
            out.append(re.compile(r"\b" + re.escape(p) + r"\b", re.I))
    return out


def _any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


@dataclass
class PracticeArea:
    key: str
    enabled: bool
    weight: float
    patterns: list[re.Pattern[str]]

    def matches(self, title: str) -> bool:
        return _any(self.patterns, title)


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    practice_areas: list[PracticeArea] = field(default_factory=list)
    competitor_patterns: list[re.Pattern[str]] = field(default_factory=list)
    recruiter_patterns: list[re.Pattern[str]] = field(default_factory=list)
    urgency_patterns: list[re.Pattern[str]] = field(default_factory=list)
    contract_patterns: list[re.Pattern[str]] = field(default_factory=list)
    growth_signal_patterns: list[re.Pattern[str]] = field(default_factory=list)

    # -- convenience accessors -------------------------------------------
    @property
    def size(self) -> dict[str, Any]:
        return self.raw["size"]

    @property
    def geo(self) -> dict[str, Any]:
        return self.raw["geo"]

    @property
    def dq(self) -> dict[str, Any]:
        return self.raw["disqualifiers"]

    @property
    def weights(self) -> dict[str, float]:
        return self.raw["weights"]

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw["scoring"]

    @property
    def tiers(self) -> dict[str, float]:
        return self.raw["tiers"]

    @property
    def http(self) -> dict[str, Any]:
        return self.raw["http"]

    @property
    def outreach(self) -> dict[str, Any]:
        """Optional -- older icp.yaml files (and tests) without this block
        still work, just with the hand-off capped at the hardcoded default
        below rather than a configured one."""
        return self.raw.get("outreach") or {}

    @property
    def auto_qualify_top_n(self) -> int:
        return int(self.outreach.get("auto_qualify_top_n", 25))

    @property
    def outreach_api_base(self) -> Optional[str]:
        """Where the Outreach Agent's API is running -- env var wins over
        icp.yaml, same convention as Outreach's own RESEARCH_API_BASE. None
        (unset) means no Outreach Agent is configured; the hand-off is then
        skipped entirely rather than failing the research run."""
        return os.environ.get("OUTREACH_API_BASE") or self.outreach.get("api_base")

    @property
    def enabled_areas(self) -> list[PracticeArea]:
        return [a for a in self.practice_areas if a.enabled]

    # -- regions ----------------------------------------------------------
    # The geographic target is the only per-run input; everything else about
    # the ICP is embedded. These accessors are the single source of truth --
    # discovery, scoring, and the API all read region data from here.

    @property
    def regions(self) -> dict[str, Any]:
        return (self.geo.get("search_regions") or {}).get("available") or {}

    @property
    def active_region(self) -> str:
        """Default region for a run when the caller does not override it."""
        active = (self.geo.get("search_regions") or {}).get("active")
        if isinstance(active, list):  # tolerate the older list-valued form
            active = active[0] if active else None
        return active or next(iter(self.regions), "GA")

    def resolve_region(self, name: str | None = None) -> str:
        """Canonical region key, case-insensitively matched. Falls back to the
        configured active region when `name` is empty.

        An unknown name is returned as-is rather than rejected: discovery can
        still search an ad-hoc region, it just gets no configured state list.
        """
        if not name:
            return self.active_region
        wanted = name.strip().lower()
        for key in self.regions:
            if key.lower() == wanted:
                return key
        return name.strip()

    def region_states(self, name: str | None = None) -> set[str]:
        """US state codes covered by a region; empty when unknown."""
        spec = self.regions.get(self.resolve_region(name)) or {}
        return {s.upper() for s in (spec.get("states") or [])}

    def region_hubs(self, name: str | None = None) -> list[str]:
        spec = self.regions.get(self.resolve_region(name)) or {}
        return list(spec.get("hubs") or [])

    @property
    def area_weight(self) -> dict[str, float]:
        return {a.key: a.weight for a in self.enabled_areas}

    def classify(self, title: str) -> list[str]:
        """Return the practice-area keys a req title matches (enabled only)."""
        return [a.key for a in self.enabled_areas if a.matches(title)]

    def match_weight(self, title: str) -> float:
        """Highest practice-area weight among matches; 0.0 if no match.

        Used so a req in a CSG-emphasized practice (e.g. AI/ML, weight 1.3)
        counts for more of the "relevant share" than one in a lower-priority
        practice (e.g. non_tech_ops, weight 0.5) -- see scoring.py.
        """
        keys = self.classify(title)
        if not keys:
            return 0.0
        aw = self.area_weight
        return max(aw[k] for k in keys)

    def is_competitor_name(self, text: str) -> bool:
        return _any(self.competitor_patterns, text or "")

    def is_recruiter_title(self, title: str) -> bool:
        return _any(self.recruiter_patterns, title or "")

    def has_urgency(self, text: str) -> bool:
        return _any(self.urgency_patterns, text or "")

    def has_contract_language(self, text: str) -> bool:
        return _any(self.contract_patterns, text or "")

    def growth_signal_hits(self, text: str) -> int:
        """Count of distinct growth-signal patterns (funding/expansion/new
        office language) found in `text` -- see scoring.py's _buying_signals,
        which only scores this when icp.yaml's buying_signals block sets
        growth_signal_points (it does, by default)."""
        if not self.growth_signal_patterns:
            return 0
        return sum(1 for p in self.growth_signal_patterns if p.search(text or ""))


# --- safe, targeted edits from the Dashboard's Settings page ---------------
# icp.yaml is hand-annotated with extensive comments explaining every value's
# provenance -- a full yaml.safe_dump() rewrite would silently destroy all of
# that. Instead, each editable field is patched in place via a line-anchored
# regex that swaps only its own value, leaving every comment, blank line, and
# the rest of the file byte-for-byte untouched. Deliberately NOT a general
# "write any YAML path" mechanism -- the Settings page's Built-in ICP card
# shows every ICP criterion, but only these two (the size band and revenue
# target) are actually editable there; region, tier thresholds, weights,
# practice areas, and disqualifier patterns are displayed read-only and stay
# file-only edits.
ICP_BASIC_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "sweet_min": re.compile(r"^(\s*sweet_min:\s*)\d+(.*)$", re.MULTILINE),
    "sweet_max": re.compile(r"^(\s*sweet_max:\s*)\d+(.*)$", re.MULTILINE),
    "revenue_sweet_min": re.compile(r"^(\s*revenue_sweet_min:\s*)\d+(.*)$", re.MULTILINE),
}


def update_icp_basics(fields: dict[str, Any], path: str | Path | None = None) -> Config:
    """Patch the two editable icp.yaml values (see ICP_BASIC_FIELD_PATTERNS)
    in place and return the reloaded Config: size.sweet_min/sweet_max (the
    Employee Size Band field) and financials.revenue_sweet_min (the Revenue
    Target field). Everything else the Built-in ICP card displays -- region,
    tier thresholds, weights, practice areas, disqualifier patterns -- can
    silently break scoring in ways a small UI form can't validate (e.g.
    weights must sum to exactly 100, tier thresholds must stay strictly
    descending), so those stay file-only edits.

    Unknown keys in `fields` raise immediately rather than being silently
    ignored -- a typo in the caller should fail loud, not no-op. sweet_min
    and sweet_max are checked together, since load_config() itself would
    happily accept a file where sweet_min > sweet_max.
    """
    unknown = set(fields) - set(ICP_BASIC_FIELD_PATTERNS)
    if unknown:
        raise ValueError(f"Unknown or non-editable icp.yaml field(s): {', '.join(sorted(unknown))}")

    p = Path(path) if path else DEFAULT_CONFIG
    current = load_config(path=p)

    coerced: dict[str, int] = {}
    for key, raw_value in fields.items():
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number, got {raw_value!r}") from None
        if value <= 0:
            raise ValueError(f"{key} must be a positive number")
        coerced[key] = value

    sweet_min = coerced.get("sweet_min", current.size.get("sweet_min"))
    sweet_max = coerced.get("sweet_max", current.size.get("sweet_max"))
    if sweet_min is not None and sweet_max is not None and int(sweet_min) > int(sweet_max):
        raise ValueError(f"sweet_min ({sweet_min}) cannot be greater than sweet_max ({sweet_max})")

    text = p.read_text(encoding="utf-8")
    for key, value in coerced.items():
        pattern = ICP_BASIC_FIELD_PATTERNS[key]
        new_text, n = pattern.subn(rf"\g<1>{value}\g<2>", text, count=1)
        if n == 0:
            raise ValueError(f"Could not find {key!r}'s line in {p} -- has the file's structure changed?")
        text = new_text

    # Validate the edited text is still well-formed and internally consistent
    # (weights still total 100, at least one practice area enabled, etc. --
    # see load_config) BEFORE writing anything, so a bug in this function can
    # never leave icp.yaml in a broken state on disk.
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_config(path=tmp)  # raises if the edited file doesn't parse/validate
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Edit would leave icp.yaml invalid: {exc}") from exc
    tmp.replace(p)
    return load_config(path=p)


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    total = sum(raw["weights"].values())
    if round(total, 6) != 100:
        raise ValueError(f"icp.yaml weights must total 100, got {total}")

    areas = [
        PracticeArea(
            key=key,
            enabled=bool(spec.get("enabled", True)),
            weight=float(spec.get("weight", 1.0)),
            patterns=_compile(spec.get("patterns", [])),
        )
        for key, spec in raw["practice_areas"].items()
    ]
    if not any(a.enabled for a in areas):
        raise ValueError("icp.yaml: at least one practice area must be enabled")

    return Config(
        raw=raw,
        path=p,
        practice_areas=areas,
        competitor_patterns=_compile(raw["disqualifiers"]["competitor_name_patterns"]),
        recruiter_patterns=_compile(raw["disqualifiers"]["recruiter_title_patterns"]),
        urgency_patterns=_compile(raw["scoring"]["fill_pain"]["urgency_patterns"]),
        contract_patterns=_compile(raw["scoring"]["buying_signals"]["contract_patterns"]),
        growth_signal_patterns=_compile(raw["scoring"]["buying_signals"].get("growth_signal_patterns", [])),
    )
