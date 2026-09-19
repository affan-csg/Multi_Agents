"""Export is the handoff boundary -- these run against stub data, no network.

Regression cover for the bug class where a field was added to the model and
schema but never made it through the read/write path to the actual output.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from csg import db, export
from csg.models import JobPosting

SCORE_ROW = {
    "domain": "acme.com", "run_id": "t1", "score": 71.5, "tier": "A",
    "dimensions": {
        "Hiring Need": 30.0, "Fill Pain": 15.0, "Firmographic Fit": 12.0,
        "Financial Strength": 9.0, "Buying Signals": 6.0, "CSG Delivery Fit": 7.0,
    },
    "explanation": ["+30 Hiring Need: 12 relevant open positions"],
    "scored_at": datetime(2026, 9, 16).isoformat(),
    "metrics": {
        "req_count": 12, "relevant_reqs": 9, "relevant_share": 0.75,
        "delivery_fit_counts": {"US": 3, "LATAM": 8, "Pakistan": 1},
        "states": ["GA"], "top_practice_areas": ["ai_ml"], "top_titles": ["AI Engineer"],
    },
    "name": "Acme Data", "region": "GA", "industry": "Software / AI", "hq_state": "GA",
    "employee_count": {"value": 400, "source": "gemini", "method": "estimated",
                       "confidence": "medium"},
    "revenue": {"value": "$25M", "source": "gemini", "method": "estimated",
                "confidence": "medium"},
    "funding": {"value": "$40M Series B", "source": "gemini", "method": "reported",
                "confidence": "high"},
    "ats_provider": "greenhouse", "ats_token": "acme",
    "hiring_signal": "confirmed", "job_discovery_method": "ats",
}


class _StubConn:
    def commit(self): ...
    def close(self): ...


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(db, "get_scores", lambda c, r: [SCORE_ROW])
    monkeypatch.setattr(db, "get_qualifications", lambda c, r: {})
    monkeypatch.setattr(db, "contacts_by_domain", lambda c, d: {})
    monkeypatch.setattr(db, "get_postings", lambda c, d: [JobPosting(
        domain="acme.com", ats_provider="greenhouse", external_id="1",
        title="Senior AI Engineer", location="Atlanta, GA",
        posted_at=datetime(2026, 7, 1), description="",
        practice_area="ai_ml", delivery_fit="LATAM")])
    return _StubConn()


def _csv_row(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(zip(lines[0].split(","), lines[1].split(",")))


def test_csv_carries_firmographics(stub, tmp_path):
    """Regression: revenue/funding/region were researched but never exported."""
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["region"] == "GA"
    assert row["revenue"] == "$25M"
    assert row["funding"] == "$40M Series B"
    assert row["hq_state"] == "GA"


def test_csv_unwraps_provenance_envelopes(stub, tmp_path):
    """A spreadsheet cell gets the value, not a serialized dict."""
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["employee_count"] == "400"
    assert "{" not in row["employee_count"]


def test_csv_carries_every_scoring_dimension(stub, tmp_path):
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["financial_strength"] == "9.0"
    assert row["csg_delivery_fit"] == "7.0"
    assert row["delivery_fit_latam"] == "8"


def test_jsonl_is_the_v2_handoff_contract(stub, tmp_path):
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["schema_version"] == "v2"
    assert p["region"] == "GA"
    assert p["industry"] == "Software / AI"
    assert p["delivery_fit"] == {"US": 3, "LATAM": 8, "Pakistan": 1}


def test_jsonl_keeps_provenance_intact(stub, tmp_path):
    """Outreach citing a revenue figure must know if it was reported or estimated."""
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["revenue"]["value"] == "$25M"
    assert p["revenue"]["method"] == "estimated"
    assert p["funding"]["method"] == "reported"


def test_jsonl_reqs_carry_classification(stub, tmp_path):
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    req = p["open_reqs"][0]
    assert req["practice_area"] == "ai_ml"
    assert req["delivery_fit"] == "LATAM"


def test_csv_outreach_ready_false_with_no_contacts(stub, tmp_path):
    """No contacts discovered yet -- not ready for Outreach handoff."""
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["outreach_ready"] == "False"
    assert row["has_linkedin_contact"] == "False"


def test_csv_outreach_ready_true_with_email_only_contact(stub, tmp_path, monkeypatch):
    """The actual rule: email alone is enough, LinkedIn is not required."""
    monkeypatch.setattr(db, "contacts_by_domain", lambda c, d: {
        "acme.com": [{"email": "jane@acme.com", "linkedin_url": None}],
    })
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["outreach_ready"] == "True"
    assert row["has_linkedin_contact"] == "False"


def test_csv_has_linkedin_contact_true_when_present(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "contacts_by_domain", lambda c, d: {
        "acme.com": [{"email": "jane@acme.com", "linkedin_url": "https://linkedin.com/in/jane"}],
    })
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["outreach_ready"] == "True"
    assert row["has_linkedin_contact"] == "True"


def test_jsonl_outreach_ready_false_with_no_contacts(stub, tmp_path):
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["outreach_ready"] is False
    assert p["has_linkedin_contact"] is False


def test_jsonl_outreach_ready_true_with_email_only_contact(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "contacts_by_domain", lambda c, d: {
        "acme.com": [{"email": "jane@acme.com", "linkedin_url": None}],
    })
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["outreach_ready"] is True
    assert p["has_linkedin_contact"] is False
    assert len(p["contacts"]) == 1


# --- hiring signal ---------------------------------------------------------
# Regression cover for exactly the bug class this file's docstring calls
# out: hiring_signal/job_discovery_method were added to Company/scoring but
# never made it into the Outreach handoff contract (Prospect), so a
# zero-open-reqs prospect with an unknown job signal was indistinguishable
# from one confirmed to have no openings once it reached Outreach.

def test_csv_carries_hiring_signal(stub, tmp_path):
    row = _csv_row(export.to_csv(stub, "t1", path=tmp_path / "p.csv"))
    assert row["hiring_signal"] == "confirmed"
    assert row["job_discovery_method"] == "ats"


def test_jsonl_carries_hiring_signal(stub, tmp_path):
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["hiring_signal"] == "confirmed"
    assert p["job_discovery_method"] == "ats"


def test_jsonl_carries_unknown_hiring_signal_distinctly(stub, tmp_path, monkeypatch):
    """The exact Inmar-style case: zero postings AND hiring_signal=unknown
    must round-trip as 'unknown', never silently become 'confirmed' or be
    dropped."""
    unknown_row = dict(SCORE_ROW, hiring_signal="unknown", job_discovery_method="unknown")
    monkeypatch.setattr(db, "get_scores", lambda c, r: [unknown_row])
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    path = export.to_jsonl(stub, "t1", path=tmp_path / "p.jsonl")
    p = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert p["hiring_signal"] == "unknown"
    assert p["job_discovery_method"] == "unknown"
    assert p["open_reqs"] == []
