"""Export qualified prospects to CSV + JSONL.

The JSONL rows are `Prospect` v1 -- the contract the Outreach Agent will read.
The CSV is for you: one row per company, with the full scoring explanation in a
single column so a spot check needs no tooling beyond a spreadsheet.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import db
from .config import PROJECT_ROOT
from .db import SupabaseConn
from .models import Prospect, provenance_value

EXPORT_DIR = PROJECT_ROOT / "data" / "exports"

CSV_COLUMNS = [
    "tier", "score", "domain", "name", "region", "employee_count",
    "revenue", "funding", "industry", "hq_state",
    "hiring_need", "fill_pain", "firmographic_fit", "financial_strength",
    "buying_signals", "csg_delivery_fit",
    "req_count", "relevant_reqs", "relevant_share", "median_age_days",
    "stale_count", "urgency_hits", "contract_hits", "distinct_locations",
    "recent_30d", "states", "top_practice_areas", "top_titles",
    "delivery_fit_us", "delivery_fit_latam", "delivery_fit_pakistan",
    "ats_provider", "ats_token", "dated_req_share", "explanation",
    "pain_summary", "recommended_angle", "contacts",
    "outreach_ready", "has_linkedin_contact",
    "job_discovery_method", "job_source", "hiring_signal",
]


def _rows(conn: SupabaseConn, run_id: str, tiers: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    rows = db.get_scores(conn, run_id)
    if tiers:
        want = {t.upper() for t in tiers}
        rows = [r for r in rows if r["tier"] in want]
    return rows


def to_csv(conn: SupabaseConn, run_id: str, path: Path | None = None,
           tiers: Optional[Iterable[str]] = None) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or EXPORT_DIR / f"prospects_{run_id}.csv"
    rows = _rows(conn, run_id, tiers)
    qualifications = db.get_qualifications(conn, run_id)
    contacts_by_domain = db.contacts_by_domain(conn, [r["domain"] for r in rows])

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            m, d = r["metrics"], r["dimensions"]
            share = m.get("relevant_share")
            qual = qualifications.get(r["domain"])
            people = contacts_by_domain.get(r["domain"], [])
            delivery = m.get("delivery_fit_counts") or {}
            w.writerow({
                "tier": r["tier"],
                "score": r["score"],
                "domain": r["domain"],
                "name": r.get("name") or "",
                "region": r.get("region") or "",
                # Provenance envelopes are unwrapped to their value for the
                # spreadsheet; full provenance stays in the JSONL export.
                "employee_count": provenance_value(r.get("employee_count")) or "",
                "revenue": provenance_value(r.get("revenue")) or "",
                "funding": provenance_value(r.get("funding")) or "",
                "industry": r.get("industry") or "",
                "hq_state": r.get("hq_state") or "",
                "hiring_need": d.get("Hiring Need", ""),
                "fill_pain": d.get("Fill Pain", ""),
                "firmographic_fit": d.get("Firmographic Fit", ""),
                "financial_strength": d.get("Financial Strength", ""),
                "buying_signals": d.get("Buying Signals", ""),
                "csg_delivery_fit": d.get("CSG Delivery Fit", ""),
                "delivery_fit_us": delivery.get("US", ""),
                "delivery_fit_latam": delivery.get("LATAM", ""),
                "delivery_fit_pakistan": delivery.get("Pakistan", ""),
                "req_count": m.get("req_count", ""),
                "relevant_reqs": m.get("relevant_reqs", ""),
                "relevant_share": f"{share:.0%}" if isinstance(share, (int, float)) else "",
                "median_age_days": m.get("median_age_days") if m.get("median_age_days") is not None else "",
                "stale_count": m.get("stale_count", ""),
                "urgency_hits": m.get("urgency_hits", ""),
                "contract_hits": m.get("contract_hits", ""),
                "distinct_locations": m.get("distinct_locations", ""),
                "recent_30d": m.get("recent_30d", ""),
                "states": ", ".join(m.get("states") or []),
                "top_practice_areas": ", ".join(m.get("top_practice_areas") or []),
                "top_titles": " | ".join(m.get("top_titles") or []),
                "ats_provider": r.get("ats_provider") or "",
                "ats_token": r.get("ats_token") or "",
                "dated_req_share": m.get("dated_req_share", ""),
                "explanation": " ; ".join(r["explanation"]),
                "pain_summary": qual["pain_summary"] if qual else "",
                "recommended_angle": qual["recommended_angle"] if qual else "",
                "contacts": " | ".join(
                    f"{p.get('first_name') or ''} {p.get('last_name') or ''} "
                    f"<{p['email']}> ({p.get('position') or 'unknown role'})".strip()
                    for p in people
                ),
                **Prospect.compute_outreach_flags(people),
                "job_discovery_method": r.get("job_discovery_method") or "",
                "job_source": r.get("job_source") or "",
                "hiring_signal": r.get("hiring_signal") or "",
            })
    return path


def to_jsonl(conn: SupabaseConn, run_id: str, path: Path | None = None,
             tiers: Optional[Iterable[str]] = None, include_reqs: int = 10) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or EXPORT_DIR / f"prospects_{run_id}.jsonl"
    rows = _rows(conn, run_id, tiers)
    qualifications = db.get_qualifications(conn, run_id)
    contacts_by_domain = db.contacts_by_domain(conn, [r["domain"] for r in rows])

    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            postings = db.get_postings(conn, r["domain"])[:include_reqs]
            qual = qualifications.get(r["domain"])
            people = contacts_by_domain.get(r["domain"], [])
            p = Prospect(
                domain=r["domain"],
                name=r.get("name"),
                tier=r["tier"],
                score=r["score"],
                dimensions=r["dimensions"],
                explanation=r["explanation"],
                metrics=r["metrics"],
                open_reqs=[{
                    "title": jp.title,
                    "location": jp.location,
                    "url": jp.url,
                    "posted_at": jp.posted_at.isoformat() if jp.posted_at else None,
                    "practice_area": jp.practice_area,
                    "delivery_fit": jp.delivery_fit,
                } for jp in postings],
                researched_at=datetime.fromisoformat(r["scored_at"]),
                region=r.get("region"),
                industry=r.get("industry"),
                hq_state=r.get("hq_state"),
                description=r.get("description"),
                employee_count=r.get("employee_count"),
                revenue=r.get("revenue"),
                funding=r.get("funding"),
                delivery_fit=(r["metrics"] or {}).get("delivery_fit_counts") or {},
                pain_summary=qual["pain_summary"] if qual else None,
                recommended_angle=qual["recommended_angle"] if qual else None,
                contacts=(contact_rows := [{
                    "name": " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or None,
                    "email": c["email"],
                    "position": c.get("position"),
                    "seniority": c.get("seniority"),
                    "linkedin_url": c.get("linkedin_url"),
                    "confidence": c.get("confidence"),
                    "verification": c.get("verification"),
                } for c in people]),
                **Prospect.compute_outreach_flags(contact_rows),
            )
            fh.write(p.model_dump_json() + "\n")
    return path
