"""Excel export for the dashboard's "Export -- Companies & Contacts" download.

This is a DIFFERENT export from export.py's to_csv/to_jsonl:
  - export.py's exports are the Outreach-handoff artifacts (CSV for a human
    spot check, JSONL as the Prospect v2 contract) and cover every scored
    company in a run, written to disk under EXPORT_DIR.
  - This module is the user-facing, on-demand download described by the
    product owner: one lean spreadsheet, one row per company, list-level
    fields only -- no full research report, no pain summary paragraphs, no
    per-req detail. It exists purely so someone can filter/sort the list in
    Excel. It is generated in-memory (BytesIO) and streamed straight back
    over HTTP by api.py's GET /runs/{run_id}/export/excel -- nothing is
    written to disk.

Columns mirror exactly what GET /runs/{run_id}/companies (Companies.tsx) and
GET /runs/{run_id}/companies/{domain} (CompanyDetail.tsx) already show at the
list level: company identity, ICP score/tier, firmographics, ATS/hiring
signal, and contact name/email/title/LinkedIn -- nothing invented, nothing
duplicated from the full report.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import db
from .db import SupabaseConn
from .models import provenance_value

# One row per company. Contacts are joined onto the row -- a company with
# multiple contacts repeats its firmographics once per contact row (the
# simplest, most spreadsheet-friendly shape for filtering/sorting), and a
# company with zero contacts still gets exactly one row with blank contact
# columns so it is never silently dropped from the list.
COLUMNS = [
    ("company_name", "Company"),
    ("domain", "Website / Domain"),
    ("tier", "ICP Tier"),
    ("score", "ICP Score"),
    ("qualification_status", "Qualification Status"),
    ("industry", "Industry"),
    ("region", "Region"),
    ("hq_state", "HQ State"),
    ("employee_count", "Employees"),
    ("revenue", "Revenue"),
    ("funding", "Funding"),
    ("ats_provider", "ATS Provider"),
    ("open_reqs", "Open Requisitions"),
    ("hiring_signal", "Hiring Signal"),
    ("careers_url", "Careers Page"),
    ("outreach_ready", "Outreach Ready"),
    ("contact_name", "Contact Name"),
    ("contact_title", "Contact Title"),
    ("contact_email", "Contact Email"),
    ("contact_linkedin", "Contact LinkedIn"),
]

# Qualification status is derived from tier -- the same A/B/C/D thresholds
# the dashboard already renders as `tier-badge tier-A` etc. (see
# Companies.tsx / CompanyDetail.tsx); this just spells the tier out for a
# reader who has never seen the app.
_TIER_LABEL = {
    "A": "Highly Qualified (Tier A)",
    "B": "Qualified (Tier B)",
    "C": "Marginal (Tier C)",
    "D": "Not Qualified (Tier D)",
}


def _tier_label(tier: str | None) -> str:
    return _TIER_LABEL.get((tier or "").upper(), tier or "")


def _contact_name(c: dict[str, Any]) -> str:
    return " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)


def _rows_for_run(conn: SupabaseConn, run_id: str) -> list[dict[str, Any]]:
    """Same read path export.py/api.py already use: db.get_scores for the
    run's companies, db.contacts_by_domain for their contacts, and
    db.companies_by_domain for the one firmographic field (careers_url)
    that get_scores' company join (db._SCORE_JOIN_FIELDS) doesn't carry --
    no new queries beyond what run_companies() already does for the same
    reason (see api.run_companies' `state` filter branch)."""
    scores = db.latest_scores_per_domain(conn) if run_id == "all" else db.get_scores(conn, run_id)
    domains = [r["domain"] for r in scores]
    contacts_by_domain = db.contacts_by_domain(conn, domains)
    companies_by_domain = db.companies_by_domain(conn, domains)

    out: list[dict[str, Any]] = []
    for r in scores:
        metrics = r.get("metrics") or {}
        req_count = metrics.get("req_count")
        people = contacts_by_domain.get(r["domain"], [])
        company = companies_by_domain.get(r["domain"])
        base = {
            "company_name": r.get("name") or "",
            "domain": r["domain"],
            "tier": r.get("tier") or "",
            "score": r.get("score"),
            "qualification_status": _tier_label(r.get("tier")),
            "industry": r.get("industry") or "",
            "region": r.get("region") or "",
            "hq_state": r.get("hq_state") or "",
            "employee_count": provenance_value(r.get("employee_count")) or "",
            "revenue": provenance_value(r.get("revenue")) or "",
            "funding": provenance_value(r.get("funding")) or "",
            "ats_provider": r.get("ats_provider") or "",
            "open_reqs": req_count if isinstance(req_count, (int, float)) else "",
            "hiring_signal": r.get("hiring_signal") or "",
            "careers_url": (company.careers_url if company else None) or "",
            "outreach_ready": bool(people),
        }
        if not people:
            out.append({
                **base,
                "contact_name": "", "contact_title": "", "contact_email": "", "contact_linkedin": "",
            })
        else:
            for c in people:
                out.append({
                    **base,
                    "contact_name": _contact_name(c),
                    "contact_title": c.get("position") or c.get("seniority") or "",
                    "contact_email": c.get("email") or "",
                    "contact_linkedin": c.get("linkedin_url") or "",
                })
    return out


def _autosize(ws: Worksheet, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
    for idx, (key, header) in enumerate(columns, start=1):
        longest = len(header)
        for row in rows:
            v = row.get(key)
            if v is not None:
                longest = max(longest, len(str(v)))
        ws.column_dimensions[get_column_letter(idx)].width = min(max(longest + 2, 10), 60)


def build_workbook(conn: SupabaseConn, run_id: str) -> Workbook:
    """Build the workbook in memory. Careful to still produce a valid,
    well-formed (headers-only) workbook when the run has zero companies or
    zero companies with contacts -- see api.py's GET /export/excel, which
    never 500s on an empty run."""
    rows = _rows_for_run(conn, run_id)

    wb = Workbook()
    ws = wb.active
    ws.title = "Companies & Contacts"

    header_font = Font(bold=True)
    for col_idx, (_, header) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
    ws.freeze_panes = "A2"

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, (key, _) in enumerate(COLUMNS, start=1):
            ws.cell(row=row_idx, column=col_idx, value=row.get(key))

    _autosize(ws, COLUMNS, rows)
    return wb


def to_bytes(conn: SupabaseConn, run_id: str) -> bytes:
    """Serialize build_workbook()'s result to raw .xlsx bytes for a
    StreamingResponse -- no temp file, no disk I/O."""
    wb = build_workbook(conn, run_id)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
