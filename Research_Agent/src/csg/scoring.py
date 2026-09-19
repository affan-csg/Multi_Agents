"""Deterministic ICP scoring. No LLM, no network -- pure function of config +
company + postings, so it is fully reproducible and unit-testable.

Every point awarded produces a ScoreReason, so `csg show <domain>` can explain
the entire score arithmetically. If you cannot read why a company scored what it
did, that is a bug in this file.

Missing-data policy: a component whose input is unknown is marked UNAVAILABLE
rather than scored zero. The dimension is then rescaled over the components that
were available -- but the rescale is capped (MAX_RESCALE) so a company we know
little about cannot reach the same ceiling as one we know well. Unknown data
lowers the achievable score; it never inflates it.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any, Optional

from .config import Config
from .models import Company, JobPosting, ScoreReason, ScoreResult, provenance_value

MAX_RESCALE = 1.35
RECENT_WINDOW_DAYS = 30

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
US_MARKERS = ("united states", "usa", "u.s.", "remote - us", "remote (us", "us remote")
FOREIGN_MARKERS = (
    "canada", "united kingdom", "england", "london", "ireland", "dublin", "germany",
    "berlin", "france", "paris", "spain", "netherlands", "amsterdam", "india",
    "bangalore", "bengaluru", "australia", "sydney", "singapore", "japan", "tokyo",
    "brazil", "mexico", "poland", "warsaw", "portugal", "lisbon", "sweden", "israel",
    "tel aviv", "china", "shanghai", "philippines", "argentina", "colombia", "toronto",
    "vancouver", "ontario", "emea", "apac", "latam",
)


# --------------------------------------------------------------------------
# component bookkeeping
# --------------------------------------------------------------------------

class _Dimension:
    """Accumulates components, tracks which were available, rescales at the end."""

    def __init__(self, name: str, weight: float) -> None:
        self.name = name
        self.weight = weight
        self.raw = 0.0
        self.available = 0.0
        self.reasons: list[ScoreReason] = []
        self.skipped: list[str] = []

    def add(self, points: float, maximum: float, detail: str) -> None:
        self.raw += points
        self.available += maximum
        if points:
            self.reasons.append(ScoreReason(dimension=self.name, points=round(points, 1), detail=detail))

    def skip(self, label: str) -> None:
        self.skipped.append(label)

    def finalize(self) -> tuple[float, list[ScoreReason], float]:
        if self.available <= 0:
            return 0.0, [], 0.0
        completeness = self.available / self.weight
        scale = min(self.weight / self.available, MAX_RESCALE)
        score = self.raw * scale
        reasons = list(self.reasons)
        if self.skipped:
            reasons.append(
                ScoreReason(
                    dimension=self.name,
                    points=0,
                    detail=f"unavailable: {', '.join(self.skipped)} "
                           f"(scaled {scale:.2f}x, cap {MAX_RESCALE})",
                )
            )
        return round(min(score, self.weight), 2), reasons, round(completeness, 2)


def _band(bands: list[list[float]], value: float) -> float:
    for lo, hi, pts in bands:
        if lo <= value <= hi:
            return float(pts)
    return 0.0


def _location_class(loc: Optional[str]) -> str:
    """'us' | 'foreign' | 'unknown' -- deliberately conservative."""
    if not loc:
        return "unknown"
    low = loc.lower()
    if any(m in low for m in FOREIGN_MARKERS):
        return "foreign"
    if any(m in low for m in US_MARKERS):
        return "us"
    for chunk in low.replace("/", ",").split(","):
        tok = chunk.strip().upper()
        if tok in US_STATES:
            return "us"
    return "unknown"


def _states(postings: list[JobPosting]) -> set[str]:
    found: set[str] = set()
    for p in postings:
        if not p.location:
            continue
        for chunk in p.location.replace("/", ",").split(","):
            tok = chunk.strip().upper()
            if tok in US_STATES:
                found.add(tok)
    return found


# --------------------------------------------------------------------------
# disqualification
# --------------------------------------------------------------------------

def disqualify(cfg: Config, company: Company, postings: list[JobPosting]) -> Optional[str]:
    """Hard filters. Zero postings (no ATS found, or a board with no open
    reqs) is deliberately NOT a disqualifier here -- that used to drop a
    company before it ever got a score row. It is now just an empty-input
    case: postings-driven checks below simply have nothing to evaluate and
    are skipped, so the company still gets scored (thin, low-tier) rather
    than vanishing from the pipeline. See pipeline.py's module docstring."""
    dq = cfg.dq

    name_blob = f"{company.name or ''} {company.domain}"
    if cfg.is_competitor_name(name_blob):
        return f"competitor: staffing/recruiting firm (matched name/domain '{name_blob.strip()}')"

    if postings:
        recruiter_reqs = sum(1 for p in postings if cfg.is_recruiter_title(p.title))
        share = recruiter_reqs / len(postings)
        if share >= float(dq["competitor_recruiter_req_share"]):
            return f"competitor: {share:.0%} of reqs are recruiting roles ({recruiter_reqs}/{len(postings)})"

        relevant = [p for p in postings if cfg.classify(p.title)]
        if len(relevant) < int(dq["min_relevant_reqs"]):
            return f"no reqs in enabled practice areas (0 of {len(postings)})"

    size = cfg.size
    emp = company.employees
    if emp is not None:
        if emp < int(size["hard_min"]):
            return f"below size floor ({emp} < {size['hard_min']} employees)"
        if emp > int(size["hard_max"]):
            return f"above size ceiling ({emp} > {size['hard_max']} employees)"
    elif bool(size.get("disqualify_when_unknown")):
        return "employee count unknown and disqualify_when_unknown=true"

    if cfg.geo.get("require_us_presence"):
        classes = [_location_class(p.location) for p in postings]
        if classes and all(c == "foreign" for c in classes):
            return "no US presence detected across any req location"

    return None


# --------------------------------------------------------------------------
# dimensions
# --------------------------------------------------------------------------

def _hiring_need(cfg: Config, company: Company, postings: list[JobPosting],
                 m: dict[str, Any]) -> _Dimension:
    s = cfg.scoring["hiring_need"]
    d = _Dimension("Hiring Need", cfg.weights["hiring_need"])

    n = len(postings)
    pts = _band(s["req_count_bands"], n)
    d.add(pts, 24, f"{n} open reqs")

    share = m["relevant_share"]
    max_share = float(s["relevant_share_max"])
    d.add(share * max_share, max_share,
          f"{m['relevant_reqs']}/{n} reqs ({share:.0%}) in enabled practice areas "
          f"[{', '.join(m['top_practice_areas']) or 'none'}]")

    emp = company.employees
    if emp:
        per100 = n / emp * 100
        m["reqs_per_100_emp"] = round(per100, 2)
        d.add(_band(s["reqs_per_100_emp_bands"], per100), 6,
              f"{per100:.1f} reqs per 100 employees ({emp} emp)")
    else:
        d.skip("reqs-per-100-employees (headcount unknown)")

    return d


def _fill_pain(cfg: Config, postings: list[JobPosting], m: dict[str, Any]) -> _Dimension:
    s = cfg.scoring["fill_pain"]
    d = _Dimension("Fill Pain", cfg.weights["fill_pain"])

    ages = m["ages"]
    if ages:
        med = statistics.median(ages)
        m["median_age_days"] = round(med, 1)
        d.add(_band(s["median_age_bands"], med), 16,
              f"median req age {med:.0f}d across {len(ages)} dated reqs")

        stale_days = float(s["stale_share_days"])
        stale = [a for a in ages if a > stale_days]
        stale_share = len(stale) / len(ages)
        m["stale_share"] = round(stale_share, 2)
        m["stale_count"] = len(stale)
        max_stale = float(s["stale_share_max"])
        d.add(stale_share * max_stale, max_stale,
              f"{len(stale)}/{len(ages)} reqs ({stale_share:.0%}) open >{stale_days:.0f}d")
    else:
        d.skip("req age (no posting dates yet -- resolves on next run via first_seen)")

    hits = m["urgency_hits"]
    max_urg = float(s["urgency_max"])
    urg_pts = min(hits, 3) / 3 * max_urg
    d.add(urg_pts, max_urg, f"urgency language in {hits} reqs" if hits else "no urgency language")

    return d


def _firmographic(cfg: Config, company: Company, postings: list[JobPosting],
                  m: dict[str, Any]) -> _Dimension:
    s = cfg.scoring["firmographic_fit"]
    size = cfg.size
    d = _Dimension("Firmographic Fit", cfg.weights["firmographic_fit"])

    emp = company.employees
    sweet_pts = float(s["size_sweet_points"])
    if emp is not None:
        if int(size["sweet_min"]) <= emp <= int(size["sweet_max"]):
            d.add(sweet_pts, sweet_pts, f"{emp} employees -- inside sweet band "
                                        f"({size['sweet_min']}-{size['sweet_max']})")
        else:
            d.add(float(s["size_acceptable_points"]), sweet_pts,
                  f"{emp} employees -- acceptable but outside sweet band")
    else:
        d.skip("size band (headcount unknown)")

    targets = [t.lower() for t in (cfg.raw.get("target_industries") or [])]
    ind_pts = float(s["industry_match_points"])
    if targets and company.industry:
        hit = any(t in company.industry.lower() for t in targets)
        d.add(ind_pts if hit else 0.0, ind_pts,
              f"industry '{company.industry}' {'matches' if hit else 'not in'} target list")
    else:
        d.skip("industry match (no target_industries configured)" if not targets
               else "industry match (company industry unknown)")

    geo_pts = float(s["geo_match_points"])
    states = m["states"]
    wanted = {x.upper() for x in (cfg.geo.get("states") or [])}
    if states:
        if not wanted:
            d.add(geo_pts, geo_pts, f"US presence in {len(states)} state(s): "
                                    f"{', '.join(sorted(states)[:6])}")
        else:
            overlap = states & wanted
            d.add(geo_pts if overlap else 0.0, geo_pts,
                  f"coverage overlap {sorted(overlap) or 'none'} vs target {sorted(wanted)}")
    elif any(_location_class(p.location) == "us" for p in postings):
        d.add(geo_pts * 0.5, geo_pts, "US presence detected but no state parsed")
    else:
        d.skip("geo (no parseable US location)")

    return d


_MONEY = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(b(?:illion|n)?|m(?:illion|m)?|k|thousand)?\b", re.I)
_MULTIPLIER = {"b": 1e9, "bn": 1e9, "billion": 1e9,
               "m": 1e6, "mm": 1e6, "million": 1e6,
               "k": 1e3, "thousand": 1e3}


def parse_money(text: Any) -> Optional[float]:
    """Parse '$25M', '1.2 billion', '$500,000' to a float. None if unparseable.

    Research text is free-form, so this is best-effort: a figure we cannot read
    is treated as unknown (component skipped), never as zero.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _MONEY.search(str(text))
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    return amount * _MULTIPLIER.get(suffix, 1.0)


def _financial_strength(cfg: Config, company: Company, m: dict[str, Any]) -> _Dimension:
    """Revenue and funding as a buying-capacity signal.

    A company that cannot fund a staffing engagement is a poor prospect however
    urgently it is hiring. Both inputs are researched estimates, so an unknown
    figure is skipped (marking the dimension incomplete) rather than scored 0 --
    absence of evidence is not evidence of weakness.
    """
    fin = cfg.raw.get("financials") or {}
    d = _Dimension("Financial Strength", cfg.weights.get("financial_strength", 10.0))

    rev_pts = float(fin.get("revenue_points", 6))
    threshold = float(fin.get("revenue_sweet_min", 10_000_000))
    revenue = parse_money(provenance_value(company.revenue))
    if revenue is not None:
        m["revenue_usd"] = revenue
        if revenue >= threshold:
            d.add(rev_pts, rev_pts,
                  f"revenue ~${revenue / 1e6:,.0f}M meets the "
                  f"${threshold / 1e6:,.0f}M+ target")
        else:
            # Partial credit scaled to how close it is -- a $8M company is a far
            # better prospect than a $200k one.
            share = max(revenue / threshold, 0.0)
            d.add(rev_pts * share, rev_pts,
                  f"revenue ~${revenue / 1e6:,.1f}M below the "
                  f"${threshold / 1e6:,.0f}M target")
    else:
        d.skip("revenue (unknown)")

    fund_pts = float(fin.get("funding_points", 4))
    funding_raw = provenance_value(company.funding)
    if funding_raw:
        funding = parse_money(funding_raw)
        m["funding_usd"] = funding
        if funding is not None and funding > 0:
            d.add(fund_pts, fund_pts, f"funding on record: {funding_raw}")
        else:
            # Funding is named but the amount is unreadable ("Series B") -- that
            # is still a real backing signal, just a weaker one.
            d.add(fund_pts * 0.5, fund_pts, f"funding on record: {funding_raw}")
    else:
        d.skip("funding (unknown)")

    return d


def _buying_signals(cfg: Config, company: Company, postings: list[JobPosting],
                    m: dict[str, Any]) -> _Dimension:
    s = cfg.scoring["buying_signals"]
    d = _Dimension("Buying Signals", cfg.weights["buying_signals"])

    ml_pts = float(s["multi_location_points"])
    n_loc = m["distinct_locations"]
    d.add(ml_pts if n_loc >= int(s["multi_location_min"]) else 0.0, ml_pts,
          f"hiring across {n_loc} distinct locations")

    c_pts = float(s["contract_language_points"])
    c_hits = m["contract_hits"]
    d.add(c_pts if c_hits else 0.0, c_pts,
          f"contract/temp language in {c_hits} reqs -- already buys contingent labor"
          if c_hits else "no contingent-labor language")

    b_pts = float(s["recent_burst_points"])
    if m["dated_reqs"] > 0:
        recent = m["recent_30d"]
        d.add(b_pts if recent >= int(s["recent_burst_min"]) else 0.0, b_pts,
              f"{recent} reqs posted in last {RECENT_WINDOW_DAYS}d")
    else:
        d.skip("recent posting burst (no posting dates)")

    # Optional, profile-gated: only profiles that define growth_signal_points
    # score this, so a profile without it (e.g. default.yaml) keeps the exact
    # 3-component behavior above -- see cfg.has_growth_signals.
    g_pts = s.get("growth_signal_points")
    if g_pts is not None:
        hits = cfg.growth_signal_hits(f"{company.description or ''} " +
                                       " ".join(p.description for p in postings))
        d.add(float(g_pts) if hits else 0.0, float(g_pts),
              f"growth language detected ({hits} hit{'s' if hits != 1 else ''}): "
              "funding/expansion/new-office signals" if hits else "no growth language detected")

    return d


def _csg_delivery_fit(cfg: Config, postings: list[JobPosting], m: dict[str, Any]) -> _Dimension:
    """How much of the req book CSG could staff, and from where.

    Offshore/nearshore capacity is weighted above onshore: those are CSG's
    margin advantage and its differentiator against a purely domestic agency.
    Reqs we could not classify are excluded from the denominator rather than
    counted against the company.
    """
    weight = cfg.weights.get("csg_delivery_fit", 10.0)
    d = _Dimension("CSG Delivery Fit", weight)

    classified = [p.delivery_fit for p in postings if p.delivery_fit]
    if not classified:
        d.skip("delivery fit (no reqs could be classified)")
        return d

    counts = {
        "LATAM": sum(1 for f in classified if f == "LATAM"),
        "Pakistan": sum(1 for f in classified if f == "Pakistan"),
        "US": sum(1 for f in classified if f == "US"),
    }
    m["delivery_fit_counts"] = counts

    total = len(classified)
    nearshore_offshore = counts["LATAM"] + counts["Pakistan"]
    offshore_pts = weight * 0.6
    onshore_pts = weight * 0.4

    d.add(nearshore_offshore / total * offshore_pts, offshore_pts,
          f"{nearshore_offshore}/{total} reqs deliverable nearshore/offshore "
          f"(LATAM {counts['LATAM']}, Pakistan {counts['Pakistan']})")
    d.add(counts["US"] / total * onshore_pts, onshore_pts,
          f"{counts['US']}/{total} reqs require US onshore delivery")
    return d

# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def compute_metrics(cfg: Config, postings: list[JobPosting], now: datetime) -> dict[str, Any]:
    ages = [a for a in (p.age_days(now) for p in postings) if a is not None]
    relevant = [p for p in postings if cfg.classify(p.title)]

    area_counts: dict[str, int] = {}
    weighted_relevant = 0.0
    for p in postings:
        keys = cfg.classify(p.title)
        for key in keys:
            area_counts[key] = area_counts.get(key, 0) + 1
        if keys:
            # Highest-weight matched practice counts toward relevant share, so a
            # req in a CSG-emphasized practice (AI/ML, weight 1.3) contributes
            # more than one in a lower-priority practice (non_tech_ops, 0.5).
            weighted_relevant += cfg.match_weight(p.title)

    locations = {p.location.strip() for p in postings if p.location and p.location.strip()}

    return {
        "req_count": len(postings),
        "relevant_reqs": len(relevant),
        "relevant_share": (min(weighted_relevant, len(postings)) / len(postings)) if postings else 0.0,
        "top_practice_areas": [k for k, _ in sorted(area_counts.items(), key=lambda kv: -kv[1])[:3]],
        "practice_area_counts": area_counts,
        "ages": ages,
        "dated_reqs": len(ages),
        "dated_req_share": round(len(ages) / len(postings), 2) if postings else 0.0,
        "median_age_days": None,
        "stale_share": None,
        "stale_count": 0,
        "urgency_hits": sum(1 for p in postings if cfg.has_urgency(f"{p.title} {p.description}")),
        "contract_hits": sum(1 for p in postings
                             if cfg.has_contract_language(f"{p.title} {p.employment_type or ''} {p.description}")),
        "distinct_locations": len(locations),
        "states": _states(postings),
        "recent_30d": sum(1 for a in ages if a <= RECENT_WINDOW_DAYS),
        "top_titles": [p.title for p in relevant[:5]] or [p.title for p in postings[:5]],
    }


def _assign_tier(cfg: Config, company: Company, total: float,
                 dim_scores: dict[str, float], m: dict[str, Any]) -> str:
    """Map a score to A/B/C/D, then apply CSG's accessibility preference.

    The score thresholds decide the band. Within the top band, startups and
    SMBs are promoted to A because they are materially easier to penetrate,
    while large enterprises settle at B -- still valuable, but a longer
    procurement cycle (see the ICP brief, "Tiering").

    Unknown signals never demote. A company that clears the A threshold keeps
    A unless it is positively known to be an enterprise: missing headcount
    means "not established as hard to penetrate", not "assume the worst".
    """
    t = cfg.tiers
    if total < t.get("C", 35):
        return "D"
    if total < t.get("B", 50):
        return "C"
    if total < t.get("A", 70):
        return "B"

    emp = company.employees
    sweet_max = int(cfg.size.get("sweet_max", 3000))
    hiring_weight = float(cfg.weights.get("hiring_need", 35.0))
    strong_hiring = dim_scores.get("Hiring Need", 0.0) >= hiring_weight * 0.4

    # Known enterprise + no standout hiring urgency -> B. Everything else that
    # cleared the A threshold stays A.
    known_enterprise = emp is not None and emp > sweet_max
    if known_enterprise and not strong_hiring:
        return "B"
    return "A"


def evaluate(cfg: Config, company: Company, postings: list[JobPosting],
             now: datetime) -> ScoreResult:
    dq_reason = disqualify(cfg, company, postings)
    if dq_reason:
        return ScoreResult(domain=company.domain, score=0.0, tier="D",
                           disqualified=True, dq_reason=dq_reason,
                           reasons=[ScoreReason(dimension="Disqualified", points=0, detail=dq_reason)])

    m = compute_metrics(cfg, postings, now)

    dims = [
        _hiring_need(cfg, company, postings, m),
        _fill_pain(cfg, postings, m),
        _firmographic(cfg, company, postings, m),
        _financial_strength(cfg, company, m),
        _buying_signals(cfg, company, postings, m),
        _csg_delivery_fit(cfg, postings, m),
    ]

    dim_scores: dict[str, float] = {}
    reasons: list[ScoreReason] = []
    completeness: dict[str, float] = {}
    total = 0.0
    blind_weight = 0.0   # weight of dimensions with no usable input at all
    for d in dims:
        score, rs, comp = d.finalize()
        dim_scores[d.name] = score
        completeness[d.name] = comp
        reasons.extend(rs)
        total += score
        if comp == 0.0:
            blind_weight += d.weight

    # A dimension we could not evaluate at all (e.g. no public financials) must
    # not read as a confirmed zero -- that would rank an unresearched company
    # below one we know to be weak. Redistribute the unscorable weight across
    # the dimensions we could measure, capped so ignorance never outscores
    # evidence. Scoring `out of` a smaller denominator is the same treatment
    # _Dimension already applies to its own components, one level up.
    scorable = 100.0 - blind_weight
    if blind_weight and scorable > 0:
        total *= min(100.0 / scorable, MAX_RESCALE)
        reasons.append(ScoreReason(
            dimension="Data Completeness", points=0,
            detail=f"{blind_weight:.0f} of 100 points unscorable (no data); "
                   f"scored out of {scorable:.0f}, capped at {MAX_RESCALE}x"))

    total = round(min(total, 100.0), 1)
    tier = _assign_tier(cfg, company, total, dim_scores, m)

    m.pop("ages", None)
    m["states"] = sorted(m["states"])
    m["data_completeness"] = completeness

    return ScoreResult(domain=company.domain, score=total, tier=tier,
                       dimensions=dim_scores, reasons=reasons, metrics=m)
