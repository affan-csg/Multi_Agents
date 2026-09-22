"""PDF export for the dashboard's "PDF Reports -- All Companies" download.

One PDF per company, reproducing everything CompanyDetail.tsx renders for
that company (Dashboard/src/pages/research/CompanyDetail.tsx): overview +
firmographics with provenance, ICP score ring + per-dimension breakdown +
"why this score" explanation, pain summary + recommended outreach angle,
contacts with their details, open requisitions, and (unlike the lean Excel
list) the full provenance -- method/source/confidence -- for every
researched fact, since that is the "evidence/sources" the product owner
explicitly asked the PDF to carry.

Reads exactly what api.company_detail() reads (db.get_company,
db.get_scores, db.get_postings, db.get_qualification, db.get_contacts) --
no new queries, no re-running research. Built with reportlab (pure Python,
no external binary dependency) directly to a BytesIO buffer; for a whole
run, build_zip() bundles one such PDF per company into a single ZIP, which
the product owner explicitly said is fine for a large run.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import db
from .db import SupabaseConn
from .models import ProvenanceValue

_STYLES = getSampleStyleSheet()
_TITLE = ParagraphStyle("ReportTitle", parent=_STYLES["Title"], fontSize=20, spaceAfter=4)
_SUBTITLE = ParagraphStyle("ReportSubtitle", parent=_STYLES["Normal"], fontSize=11, textColor=colors.grey, spaceAfter=16)
_H2 = ParagraphStyle("H2", parent=_STYLES["Heading2"], fontSize=13, spaceBefore=14, spaceAfter=6)
_BODY = ParagraphStyle("Body", parent=_STYLES["BodyText"], fontSize=9.5, leading=13)
_MUTED = ParagraphStyle("Muted", parent=_BODY, textColor=colors.grey)
_SMALL = ParagraphStyle("Small", parent=_BODY, fontSize=8.5, leading=11)

_TABLE_HEADER_BG = colors.HexColor("#eef0f6")
_GRID_COLOR = colors.HexColor("#d8dae3")


def _esc(v: Any) -> str:
    """Plain text -> safe reportlab Paragraph markup: escape the few chars
    Paragraph treats as markup, so a company name/description containing
    '<', '>', or '&' can never corrupt the layout or raise inside reportlab."""
    if v is None:
        return ""
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_provenance(pv: Optional[dict[str, Any]] | ProvenanceValue) -> str:
    """Full provenance -- value + method + source + confidence + fetched_at
    -- unlike the Excel list view, which unwraps to the bare value only."""
    if pv is None:
        return "Unknown"
    if isinstance(pv, ProvenanceValue):
        pv = pv.model_dump(mode="json")
    if not isinstance(pv, dict):
        return _esc(pv)
    value = pv.get("value")
    if value is None:
        return "Unknown"
    method = pv.get("method") or "unknown"
    confidence = pv.get("confidence") or "unknown"
    source = pv.get("source") or "unknown"
    fetched = pv.get("fetched_at")
    bits = [f"{_esc(value)} ({method}, {confidence} confidence, source: {_esc(source)}"]
    if fetched:
        bits[0] += f", fetched {_esc(str(fetched)[:10])}"
    bits[0] += ")"
    return bits[0]


def _section_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{_esc(k)}</b>", _SMALL), Paragraph(v, _SMALL)] for k, v in rows]
    t = Table(data, colWidths=[1.6 * inch, 5.1 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, _GRID_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), _TABLE_HEADER_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _company_detail(conn: SupabaseConn, run_id: str, domain: str) -> Optional[dict[str, Any]]:
    """Same assembly as api.company_detail() -- duplicated rather than
    imported so this module never depends on FastAPI's request/response
    plumbing, only on db.py's read functions."""
    company = db.get_company(conn, domain)
    if not company:
        return None
    if run_id == "all":
        matches = [r for r in db.latest_scores_per_domain(conn) if r["domain"] == domain]
        effective_run_id = matches[0]["run_id"] if matches else None
    else:
        effective_run_id = run_id
    scores = [r for r in db.get_scores(conn, effective_run_id) if r["domain"] == domain] if effective_run_id else []
    postings = db.get_postings(conn, domain)
    qual = db.get_qualification(conn, domain, effective_run_id) if effective_run_id else None
    people = db.get_contacts(conn, domain)
    return {
        "company": company,
        "score": scores[0] if scores else None,
        "postings": postings,
        "qualification": qual,
        "contacts": people,
    }


def _build_story(detail: dict[str, Any], run_id: str) -> list:
    company = detail["company"]
    score = detail["score"]
    qual = detail["qualification"]
    contacts = detail["contacts"]
    postings = detail["postings"]

    name = company.name or company.domain
    story: list = []

    story.append(Paragraph(_esc(name), _TITLE))
    subtitle_bits = [company.domain]
    if score:
        subtitle_bits.append(f"Tier {score['tier']} · Score {score['score']:.0f}")
    subtitle_bits.append(f"Research run: {run_id}")
    subtitle_bits.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    story.append(Paragraph(_esc(" | ".join(subtitle_bits)), _SUBTITLE))

    # --- Company overview / firmographics -----------------------------
    story.append(Paragraph("Company Overview", _H2))
    if company.description:
        story.append(Paragraph(_esc(company.description), _BODY))
        story.append(Spacer(1, 6))
    overview_rows = [
        ("Domain", _esc(company.domain)),
        ("Industry", _esc(company.industry) or "Unknown"),
        ("Region", _esc(company.region) or "Unknown"),
        ("HQ State", _esc(company.hq_state) or "Unknown"),
        ("Employee Count", _fmt_provenance(company.employee_count)),
        ("Revenue", _fmt_provenance(company.revenue)),
        ("Funding", _fmt_provenance(company.funding)),
        ("ATS Provider", _esc(company.ats_provider) or "Not detected"),
        ("ATS Detected By", _esc(company.ats_detected_by) or "—"),
        ("Careers Page", _esc(company.careers_url) or "—"),
        ("Status", _esc(company.status)),
        ("Job Discovery Method", _esc(company.job_discovery_method) or "unknown"),
        ("Hiring Signal", _esc(company.hiring_signal) or "unknown"),
    ]
    if company.dq_reason:
        overview_rows.append(("Disqualification Reason", _esc(company.dq_reason)))
    if company.job_discovery_notes:
        overview_rows.append(("Job Discovery Notes", _esc(company.job_discovery_notes)))
    story.append(_section_table(overview_rows))

    # --- ICP score + dimension breakdown + explanation -----------------
    story.append(Paragraph("ICP Score & Qualification", _H2))
    if score:
        score_rows = [("Overall Score", f"{score['score']:.1f}"), ("Tier", score["tier"])]
        story.append(_section_table(score_rows))
        story.append(Spacer(1, 8))

        dims = score.get("dimensions") or {}
        if dims:
            story.append(Paragraph("Score breakdown by dimension:", _BODY))
            dim_data = [["Dimension", "Points"]] + [
                [_esc(k), f"{v:.1f}" if isinstance(v, (int, float)) else _esc(v)]
                for k, v in dims.items()
            ]
            dim_table = Table(dim_data, colWidths=[3.5 * inch, 1.5 * inch])
            dim_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, _GRID_COLOR),
                ("BACKGROUND", (0, 0), (-1, 0), _TABLE_HEADER_BG),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(Spacer(1, 4))
            story.append(dim_table)

        explanation = score.get("explanation") or []
        if explanation:
            story.append(Spacer(1, 8))
            story.append(Paragraph("Why this score:", _BODY))
            story.append(ListFlowable(
                [ListItem(Paragraph(_esc(line), _SMALL)) for line in explanation],
                bulletType="bullet", leftIndent=14,
            ))
    else:
        story.append(Paragraph("No score recorded for this company in this run.", _MUTED))

    # --- Pain summary / recommended angle -------------------------------
    story.append(Paragraph("Pain Summary & Recommended Outreach Angle", _H2))
    if qual:
        story.append(Paragraph(f"<b>Pain summary:</b> {_esc(qual['pain_summary'])}", _BODY))
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<b>Recommended angle:</b> {_esc(qual['recommended_angle'])}", _BODY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"Generated by {_esc(qual.get('model'))} on {_esc(str(qual.get('qualified_at'))[:10])}", _MUTED,
        ))
    else:
        story.append(Paragraph("Not yet qualified with AI -- no pain summary available.", _MUTED))

    # --- Contacts ---------------------------------------------------------
    story.append(Paragraph(f"Contacts ({len(contacts)})", _H2))
    if not contacts:
        story.append(Paragraph("No contacts discovered yet for this company.", _MUTED))
    else:
        contact_data = [["Name", "Title", "Email", "Confidence", "LinkedIn"]]
        for c in contacts:
            contact_name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or "—"
            contact_data.append([
                _esc(contact_name),
                _esc(c.get("position") or c.get("seniority") or "—"),
                _esc(c.get("email")),
                f"{c['confidence']}%" if c.get("confidence") is not None else "—",
                _esc(c.get("linkedin_url")) or "—",
            ])
        contact_table = Table(contact_data, colWidths=[1.3 * inch, 1.3 * inch, 1.7 * inch, 0.8 * inch, 1.6 * inch])
        contact_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, _GRID_COLOR),
            ("BACKGROUND", (0, 0), (-1, 0), _TABLE_HEADER_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(contact_table)

    # --- Open requisitions --------------------------------------------
    story.append(Paragraph(f"Open Requisitions ({len(postings)})", _H2))
    if not postings:
        if company.hiring_signal == "unknown":
            story.append(Paragraph(
                "Job data unavailable -- every ATS/careers-page/search discovery method was exhausted for "
                "this company. This is NOT a confirmed zero; actual hiring status is unknown.", _MUTED,
            ))
        else:
            story.append(Paragraph(
                "No open requisitions found (confirmed -- a job board or careers page was checked).", _MUTED,
            ))
    else:
        req_data = [["Title", "Location", "Department", "Posted", "Practice Area", "Delivery Fit"]]
        for p in postings[:100]:
            posted = p.posted_at.strftime("%Y-%m-%d") if p.posted_at else "—"
            req_data.append([
                _esc(p.title) or "—",
                _esc(p.location) or "—",
                _esc(p.department) or "—",
                posted,
                _esc(p.practice_area) or "—",
                _esc(p.delivery_fit) or "—",
            ])
        req_table = Table(req_data, colWidths=[1.7 * inch, 1.2 * inch, 1.0 * inch, 0.8 * inch, 1.0 * inch, 1.0 * inch])
        req_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, _GRID_COLOR),
            ("BACKGROUND", (0, 0), (-1, 0), _TABLE_HEADER_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(req_table)
        if len(postings) > 100:
            story.append(Spacer(1, 4))
            story.append(Paragraph(f"...and {len(postings) - 100} more requisitions not shown.", _MUTED))

    return story


def render_company_pdf(conn: SupabaseConn, run_id: str, domain: str) -> Optional[bytes]:
    """One company's full report as PDF bytes, or None if the domain has no
    company row (never researched / already deleted)."""
    detail = _company_detail(conn, run_id, domain)
    if detail is None:
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Research Report - {detail['company'].name or domain}",
    )
    story = _build_story(detail, run_id)
    doc.build(story)
    return buf.getvalue()


def _safe_filename(domain: str) -> str:
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in domain)


def build_zip(conn: SupabaseConn, run_id: str) -> bytes:
    """One PDF per company scored in this run, bundled into a single ZIP --
    the product owner explicitly said a ZIP of many PDFs is an acceptable
    (in fact preferred) shape for a large run. Returns a valid, well-formed
    (but empty) ZIP when the run has zero companies, rather than raising or
    producing a corrupt file -- see api.py's GET /export/pdf."""
    scores = db.latest_scores_per_domain(conn) if run_id == "all" else db.get_scores(conn, run_id)
    domains = sorted({r["domain"] for r in scores})

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for domain in domains:
            pdf_bytes = render_company_pdf(conn, run_id, domain)
            if pdf_bytes is None:
                continue
            zf.writestr(f"{_safe_filename(domain)}.pdf", pdf_bytes)
    return buf.getvalue()
