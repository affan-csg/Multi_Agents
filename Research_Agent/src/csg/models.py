"""Domain models.

`Prospect` is the frozen boundary between the Research Agent and the future
Outreach / Coordination agents. Those agents import THIS module and read the
`scores` + `companies` tables -- they never import pipeline, scoring, or ats.
When they arrive, this file moves to csg/core/models.py unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Tier = Literal["A", "B", "C", "D"]
AtsProvider = Literal["greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "bamboohr", "rippling"]

# Where a JobPosting's data actually came from when no ATS adapter is
# involved -- careers-page HTML scraping or the external Tavily+Groq search
# fallback (see jobs_discovery.py). JobPosting.ats_provider stays an
# AtsProvider|None (None for these two, since no ATS was ever resolved);
# `source` is what scoring/UI/provenance actually key off to know how a
# posting was found.
JobSource = Literal["ats", "careers_page", "web_search"]

# How job discovery for a company concluded -- recorded on Company so a
# missing/empty job signal is never confused with "confirmed zero jobs".
# See jobs_discovery.py's module docstring for the full fallback chain this
# tracks: ats -> careers_page -> web_search -> unknown.
JobDiscoveryMethod = Literal["ats", "careers_page", "web_search", "unknown"]

# Whether the job signal backing a company's score is real evidence or an
# absence of it. "confirmed" -- a source was actually read (an ATS board, a
# careers page, or a search result), whether or not it had zero postings.
# "unknown" -- every discovery method was exhausted without reading anything
# usable; the true job count could be zero OR nonzero, we simply don't know.
# Scoring must never treat "unknown" as "confirmed zero" (see scoring.py).
HiringSignal = Literal["confirmed", "unknown"]

Method = Literal["reported", "estimated", "unknown"]


class ProvenanceValue(BaseModel):
    """A researched fact plus where it came from.

    Financial and headcount figures are never stored bare: §3 of the ICP brief
    requires every one to carry source, date, and confidence so an estimate is
    never mistaken for a reported figure.
    """

    value: Any
    source: str
    url: Optional[str] = None
    fetched_at: Optional[datetime] = None
    method: Method = "estimated"
    confidence: Literal["high", "medium", "low"] = "medium"

    @classmethod
    def coerce(cls, v: Any, *, source: str = "manual") -> Optional["ProvenanceValue"]:
        """Accept a bare scalar, a dict, or an existing instance.

        Bare scalars come from operator-supplied input, which is first-hand
        and so recorded as `reported`.
        """
        if v is None or isinstance(v, cls):
            return v
        if isinstance(v, dict):
            return cls(**v)
        return cls(value=v, source=source, method="reported", confidence="high")


def provenance_value(v: Any) -> Any:
    """Unwrap a ProvenanceValue (or legacy dict) down to its raw value."""
    if isinstance(v, ProvenanceValue):
        return v.value
    if isinstance(v, dict):
        return v.get("value")
    return v


class Company(BaseModel):
    domain: str
    name: Optional[str] = None
    employee_count: Optional[ProvenanceValue] = None
    revenue: Optional[ProvenanceValue] = None
    funding: Optional[ProvenanceValue] = None
    description: Optional[str] = None
    region: Optional[str] = None
    industry: Optional[str] = None
    hq_state: Optional[str] = None
    ats_provider: Optional[AtsProvider] = None
    ats_token: Optional[str] = None
    ats_detected_by: Optional[str] = None
    careers_url: Optional[str] = None
    status: str = "discovered"
    dq_reason: Optional[str] = None

    # --- job discovery provenance --------------------------------------
    # Populated by jobs_discovery.discover_jobs() every run, independent of
    # whether an ATS was ever found -- see that module's docstring for the
    # full ats -> careers_page -> web_search -> unknown fallback chain and
    # why "no ATS" must never collapse into "no jobs" or "no hiring need".
    job_discovery_method: Optional[JobDiscoveryMethod] = None
    job_source: Optional[str] = None  # human-readable, e.g. "greenhouse", "careers page scrape", "tavily search"
    hiring_signal: Optional[HiringSignal] = None
    job_discovery_notes: Optional[str] = None  # failure reason when hiring_signal == "unknown"

    @field_validator("employee_count", "revenue", "funding", mode="before")
    @classmethod
    def _wrap_provenance(cls, v: Any) -> Any:
        # A bare scalar (hand-entered value, older DB row) is
        # promoted to a ProvenanceValue rather than rejected -- callers should
        # not have to construct provenance by hand for first-hand input.
        return ProvenanceValue.coerce(v)

    @property
    def employees(self) -> Optional[int]:
        """Headcount as a plain int, or None when unknown."""
        v = provenance_value(self.employee_count)
        return v if isinstance(v, int) and v > 0 else None


class JobPosting(BaseModel):
    domain: str
    # None for a posting found via careers_page scraping or web_search --
    # those never resolve to a specific ATS. `source` (below) is what
    # distinguishes how a posting was found; this field stays a real
    # AtsProvider only when one was actually detected.
    ats_provider: Optional[AtsProvider] = None
    source: JobSource = "ats"
    external_id: str
    title: str = ""
    location: Optional[str] = None
    department: Optional[str] = None
    employment_type: Optional[str] = None
    url: Optional[str] = None
    posted_at: Optional[datetime] = None
    first_seen: Optional[datetime] = None
    description: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    delivery_fit: Optional[str] = None
    practice_area: Optional[str] = None

    def age_days(self, now: datetime) -> Optional[float]:
        """Age from the ATS posting date; falls back to our first observation.

        Returns None when neither is known -- such reqs are EXCLUDED from the
        median rather than silently treated as fresh.
        """
        ref = self.posted_at or self.first_seen
        if ref is None:
            return None
        if ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        return max(0.0, (now - ref).total_seconds() / 86400.0)


class ScoreReason(BaseModel):
    dimension: str
    points: float
    detail: str

    def __str__(self) -> str:
        sign = "+" if self.points >= 0 else ""
        return f"{sign}{self.points:g} {self.dimension}: {self.detail}"


class ScoreResult(BaseModel):
    domain: str
    score: float = 0.0
    tier: Tier = "D"
    dimensions: dict[str, float] = Field(default_factory=dict)
    reasons: list[ScoreReason] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    disqualified: bool = False
    dq_reason: Optional[str] = None


class Prospect(BaseModel):
    """Handoff contract v2 -- what the Outreach Agent consumes.

    v2 adds the firmographics and delivery recommendation that outreach needs
    to personalise on the actual company rather than send generic staffing
    copy: region, size, revenue/funding (with provenance intact), industry,
    and which CSG delivery geography fits the open roles.
    """

    schema_version: Literal["v2"] = "v2"
    domain: str
    name: Optional[str] = None
    tier: Tier
    score: float
    dimensions: dict[str, float]
    explanation: list[str]
    metrics: dict[str, Any]
    open_reqs: list[dict[str, Any]] = Field(default_factory=list)
    researched_at: datetime

    # --- firmographics (v2) -------------------------------------------------
    # Provenance envelopes are kept whole here, unlike the flattened CSV: an
    # outreach message that cites a revenue figure needs to know whether it
    # was reported or estimated.
    region: Optional[str] = None
    industry: Optional[str] = None
    hq_state: Optional[str] = None
    description: Optional[str] = None
    employee_count: Optional[ProvenanceValue] = None
    revenue: Optional[ProvenanceValue] = None
    funding: Optional[ProvenanceValue] = None

    # Which CSG delivery geography the open roles suit, e.g.
    # {"US": 3, "LATAM": 8, "Pakistan": 1}.
    delivery_fit: dict[str, int] = Field(default_factory=dict)

    # Populated by the LLM layer in step 4; empty in the deterministic MVP.
    pain_summary: Optional[str] = None
    recommended_angle: Optional[str] = None
    contacts: list[dict[str, Any]] = Field(default_factory=list)

    # --- outreach readiness --------------------------------------------------
    # Gate for handoff to the Outreach Agent: a company needs at least one
    # verified contact with an email (Hunter.io never returns a contact
    # without one -- see contacts._parse_emails -- so `contacts` being
    # non-empty already implies this). LinkedIn is a bonus signal, not a
    # requirement: `has_linkedin_contact` records whether any contact also
    # carries a linkedin_url, purely informational. Plain fields (not
    # @property) so they survive model_dump()/model_dump_json() -- the
    # Outreach Agent reads the exported JSONL, not live Python objects.
    outreach_ready: bool = False
    has_linkedin_contact: bool = False

    @classmethod
    def compute_outreach_flags(cls, contacts: list[dict[str, Any]]) -> dict[str, bool]:
        """Derive outreach_ready/has_linkedin_contact from a contacts list.
        Single source of truth for the rule, called by export.py so the two
        booleans can never drift out of sync with the `contacts` field."""
        return {
            "outreach_ready": len(contacts) > 0,
            "has_linkedin_contact": any(c.get("linkedin_url") for c in contacts),
        }
