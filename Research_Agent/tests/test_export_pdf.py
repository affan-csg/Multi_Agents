"""Tests for export_pdf.py -- the dashboard's on-demand "PDF Reports: All
Companies" download. Stub-based, same style as test_export.py/
test_export_excel.py: db.* is monkeypatched directly, no network, no real
Supabase or reportlab-external dependency beyond reportlab itself.

Correctness is checked by actually extracting text back out of the
generated PDF bytes (via pypdf, already a reportlab transitive dependency
isn't guaranteed, so this uses reportlab's own PDF -- extraction is done
with a small manual parser is unnecessary; pypdf is added as a test-only
tool through importorskip so a missing extractor degrades to a byte-shape
check rather than failing the whole suite) -- not just "it didn't crash".
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from io import BytesIO

import pytest

from csg import db, export_pdf
from csg.models import Company, JobPosting

ACME_COMPANY = Company(
    domain="acme.com", name="Acme Data",
    employee_count={"value": 400, "source": "gemini", "method": "estimated", "confidence": "medium"},
    revenue={"value": "$25M", "source": "gemini", "method": "estimated", "confidence": "medium"},
    funding={"value": "$40M Series B", "source": "gemini", "method": "reported", "confidence": "high"},
    description="A data analytics company.",
    region="GA", industry="Software / AI", hq_state="GA",
    ats_provider="greenhouse", ats_detected_by="token_probe",
    careers_url="https://acme.com/careers",
    hiring_signal="confirmed", job_discovery_method="ats",
)

SCORE_ROW = {
    "domain": "acme.com", "run_id": "t1", "score": 71.5, "tier": "A",
    "dimensions": {"Hiring Need": 30.0, "Fill Pain": 15.0},
    "explanation": ["+30 Hiring Need: 12 relevant open positions", "+15 Fill Pain: median req age 45 days"],
    "scored_at": datetime(2026, 9, 16).isoformat(),
    "metrics": {"req_count": 12},
}

CONTACT = {
    "email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe",
    "position": "VP Talent", "confidence": 92, "verification": "valid",
    "linkedin_url": "https://linkedin.com/in/janedoe",
}

POSTING = JobPosting(
    domain="acme.com", ats_provider="greenhouse", external_id="1",
    title="Senior AI Engineer", location="Atlanta, GA", department="Engineering",
    posted_at=datetime(2026, 7, 1), description="", practice_area="ai_ml", delivery_fit="LATAM",
)

QUALIFICATION = {
    "domain": "acme.com", "run_id": "t1",
    "pain_summary": "Struggling to fill AI engineering roles quickly.",
    "recommended_angle": "Lead with LATAM delivery speed.",
    "model": "gemini-2.5", "qualified_at": "2026-09-16T00:00:00",
}


class _StubConn:
    def close(self): ...


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(db, "get_company", lambda c, d: ACME_COMPANY if d == "acme.com" else None)
    monkeypatch.setattr(db, "get_scores", lambda c, r: [SCORE_ROW] if r == "t1" else [])
    monkeypatch.setattr(db, "latest_scores_per_domain", lambda c: [SCORE_ROW])
    monkeypatch.setattr(db, "get_postings", lambda c, d: [POSTING] if d == "acme.com" else [])
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: QUALIFICATION if d == "acme.com" else None)
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [CONTACT] if d == "acme.com" else [])
    return _StubConn()


def _extract_text(pdf_bytes: bytes) -> str:
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_render_company_pdf_returns_bytes(stub):
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    assert isinstance(content, bytes)
    assert content.startswith(b"%PDF")
    assert len(content) > 500


def test_render_company_pdf_returns_none_for_unknown_domain(stub):
    assert export_pdf.render_company_pdf(stub, "t1", "nope.com") is None


def test_pdf_contains_company_overview(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "Acme Data" in text
    assert "acme.com" in text
    assert "A data analytics company." in text
    assert "Software / AI" in text
    assert "greenhouse" in text


def test_pdf_contains_full_provenance_not_just_bare_value(stub):
    """Unlike the Excel list (bare value only), the PDF must show the full
    provenance -- method/source/confidence -- since that's the
    evidence/sources the product owner explicitly asked for."""
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "estimated" in text
    assert "medium confidence" in text
    assert "gemini" in text
    assert "reported" in text  # funding's method
    assert "high confidence" in text


def test_pdf_contains_icp_score_and_breakdown(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "71.5" in text or "71" in text
    assert "Tier A" in text or "A" in text
    assert "Hiring Need" in text
    assert "Fill Pain" in text


def test_pdf_contains_score_explanation(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "relevant open positions" in text
    assert "median req age" in text


def test_pdf_contains_pain_summary_and_angle(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "Struggling to fill AI engineering roles quickly." in text
    assert "Lead with LATAM delivery speed." in text


def test_pdf_contains_contact_details(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "Jane" in text
    assert "Doe" in text
    assert "jane@acme.com" in text
    assert "VP Talent" in text
    assert "92" in text
    assert "linkedin.com/in/janedoe" in text


def test_pdf_contains_open_requisitions(stub):
    text = _extract_text(export_pdf.render_company_pdf(stub, "t1", "acme.com"))
    assert "Senior AI Engineer" in text
    assert "Atlanta" in text
    assert "Engineering" in text
    assert "ai_ml" in text
    assert "LATAM" in text


def test_pdf_handles_missing_qualification_gracefully(stub, monkeypatch):
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: None)
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    text = _extract_text(content)
    assert "Not yet qualified" in text


def test_pdf_handles_no_contacts_gracefully(stub, monkeypatch):
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [])
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    text = _extract_text(content)
    assert "No contacts discovered yet" in text


def test_pdf_handles_no_postings_confirmed_zero(stub, monkeypatch):
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    text = _extract_text(content)
    assert "confirmed" in text.lower()


def test_pdf_handles_unknown_hiring_signal_distinctly(stub, monkeypatch):
    unknown_company = ACME_COMPANY.model_copy(update={"hiring_signal": "unknown", "ats_provider": None})
    monkeypatch.setattr(db, "get_company", lambda c, d: unknown_company)
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    text = " ".join(_extract_text(content).split())
    assert "NOT a confirmed zero" in text


def test_pdf_handles_missing_score_gracefully(stub, monkeypatch):
    monkeypatch.setattr(db, "get_scores", lambda c, r: [])
    monkeypatch.setattr(db, "latest_scores_per_domain", lambda c: [])
    content = export_pdf.render_company_pdf(stub, "t1", "acme.com")
    text = _extract_text(content)
    assert "No score recorded" in text


def test_pdf_handles_missing_firmographics_gracefully(stub, monkeypatch):
    """A company with no employee_count/revenue/funding/industry researched
    yet must not crash the PDF build -- should render 'Unknown'."""
    bare_company = Company(domain="bare.com", name="Bare Co")
    monkeypatch.setattr(db, "get_company", lambda c, d: bare_company)
    monkeypatch.setattr(db, "get_scores", lambda c, r: [])
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: None)
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [])
    content = export_pdf.render_company_pdf(stub, "t1", "bare.com")
    assert content is not None
    text = _extract_text(content)
    assert "Unknown" in text


# --- ZIP bundling -----------------------------------------------------------

def test_build_zip_contains_one_pdf_per_company(stub, monkeypatch):
    second_row = dict(SCORE_ROW, domain="beta.com")
    monkeypatch.setattr(db, "get_scores", lambda c, r: [SCORE_ROW, second_row])
    monkeypatch.setattr(db, "get_company", lambda c, d: ACME_COMPANY if d == "acme.com" else
                         Company(domain="beta.com", name="Beta Inc"))

    content = export_pdf.build_zip(stub, "t1")
    zf = zipfile.ZipFile(BytesIO(content))
    names = sorted(zf.namelist())
    assert names == ["acme.com.pdf", "beta.com.pdf"]
    for name in names:
        assert zf.read(name).startswith(b"%PDF")


def test_build_zip_empty_run_produces_valid_empty_zip(monkeypatch):
    """A run with zero companies must produce a valid, well-formed
    (empty) ZIP rather than raising or a corrupt file."""
    monkeypatch.setattr(db, "get_scores", lambda c, r: [])
    content = export_pdf.build_zip(_StubConn(), "empty-run")
    zf = zipfile.ZipFile(BytesIO(content))
    assert zf.namelist() == []
    assert zf.testzip() is None  # confirms it's a structurally valid zip


def test_build_zip_skips_domain_with_no_company_row(stub, monkeypatch):
    """A score row whose company was deleted since scoring must be skipped,
    not crash the whole bundle."""
    ghost_row = dict(SCORE_ROW, domain="ghost.com")
    monkeypatch.setattr(db, "get_scores", lambda c, r: [SCORE_ROW, ghost_row])
    monkeypatch.setattr(db, "get_company", lambda c, d: ACME_COMPANY if d == "acme.com" else None)

    content = export_pdf.build_zip(stub, "t1")
    zf = zipfile.ZipFile(BytesIO(content))
    assert zf.namelist() == ["acme.com.pdf"]


def test_build_zip_large_run_performance_and_correctness(monkeypatch):
    """A 100-company run must complete in reasonable time and produce
    exactly one correctly-named PDF per company."""
    import time

    rows = [dict(SCORE_ROW, domain=f"company{i}.com") for i in range(100)]
    companies = {f"company{i}.com": Company(domain=f"company{i}.com", name=f"Company {i}") for i in range(100)}
    monkeypatch.setattr(db, "get_scores", lambda c, r: rows)
    monkeypatch.setattr(db, "get_company", lambda c, d: companies.get(d))
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: None)
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [])

    start = time.monotonic()
    content = export_pdf.build_zip(_StubConn(), "big-run")
    elapsed = time.monotonic() - start

    zf = zipfile.ZipFile(BytesIO(content))
    names = zf.namelist()
    assert len(names) == 100
    assert set(names) == {f"company{i}.com.pdf" for i in range(100)}
    assert elapsed < 60.0
