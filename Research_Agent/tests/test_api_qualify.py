"""Tests for the qualify API surface: POST /runs/{run_id}/qualify (including
the `domains` restriction used by a single company's "Qualify with AI"
button), GET /runs/{run_id}/qualify/failed, POST
/runs/{run_id}/qualify/retry-failed, and company_detail's
`qualification_attempt` field. Same TestClient + monkeypatch style as
test_api_export.py -- db.* and qualify_mod.* are stubbed so these lock in
the HTTP contract without a real Supabase project, LLM call, or background
thread actually running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from csg import api, db, qualify as qualify_mod
from csg.models import Company


class _FakeConn:
    def close(self):
        pass


@pytest.fixture
def client():
    # Job-starting routes (/qualify, /qualify/retry-failed) need _MAIN_LOOP
    # captured, which only happens via the app's lifespan -- that only runs
    # under the `with` context manager form, not a bare TestClient(app).
    with TestClient(api.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_db(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    # Background job runners are never exercised here -- these tests lock in
    # the synchronous "should a job be queued at all, and with what count"
    # contract, not qualify_run's own behavior (covered by test_qualify_run.py).
    monkeypatch.setattr(api, "_run_qualify_job", lambda job, run_id, req: None)
    monkeypatch.setattr(api, "_run_qualify_retry_job", lambda job, run_id: None)
    monkeypatch.setattr(qualify_mod, "backend_name", lambda: "gemini-3.6-flash")


# --- POST /runs/{run_id}/qualify ---------------------------------------------

def test_start_qualify_passes_domains_through_to_pending_rows(monkeypatch, client):
    """Regression: a single company's 'Qualify with AI' button must restrict
    to exactly that domain, not silently widen to every pending company in
    the tiers list (the bug: `domains` used to be accepted by nothing in the
    request/pending_rows call chain at all)."""
    captured = {}

    def fake_pending_rows(conn, run_id, tiers, force=False, domains=None):
        captured["tiers"] = tiers
        captured["force"] = force
        captured["domains"] = domains
        return [{"domain": d} for d in (domains or [])]

    monkeypatch.setattr(qualify_mod, "pending_rows", fake_pending_rows)

    r = client.post("/runs/t1/qualify", json={
        "tiers": ["A", "B", "C", "D"], "force": True, "domains": ["acme.com"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["companies"] == 1
    assert captured["domains"] == ["acme.com"]
    assert captured["force"] is True


def test_start_qualify_domains_omitted_means_no_restriction(monkeypatch, client):
    captured = {}

    def fake_pending_rows(conn, run_id, tiers, force=False, domains=None):
        captured["domains"] = domains
        return [{"domain": "a.com"}, {"domain": "b.com"}]

    monkeypatch.setattr(qualify_mod, "pending_rows", fake_pending_rows)

    r = client.post("/runs/t1/qualify", json={"tiers": ["A", "B"]})
    assert r.status_code == 200
    assert r.json()["companies"] == 2
    assert captured["domains"] is None


def test_start_qualify_nothing_pending_returns_null_job_id(monkeypatch, client):
    monkeypatch.setattr(qualify_mod, "pending_rows", lambda *a, **k: [])
    r = client.post("/runs/t1/qualify", json={"tiers": ["A", "B"]})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] is None
    assert "message" in body


def test_start_qualify_no_api_key_returns_400(monkeypatch, client):
    def raise_no_key(*a, **k):
        raise qualify_mod.QualificationError("no LLM API key found")
    monkeypatch.setattr(qualify_mod, "pending_rows", lambda *a, **k: [{"domain": "a.com"}])
    monkeypatch.setattr(qualify_mod, "backend_name", raise_no_key)

    r = client.post("/runs/t1/qualify", json={"tiers": ["A", "B"]})
    assert r.status_code == 400


# --- GET /runs/{run_id}/qualify/failed ---------------------------------------

def test_list_failed_qualifications_returns_failed_rows(monkeypatch, client):
    monkeypatch.setattr(qualify_mod, "failed_rows", lambda conn, run_id: [{"domain": "bad.com", "tier": "A"}])
    r = client.get("/runs/t1/qualify/failed")
    assert r.status_code == 200
    assert r.json() == [{"domain": "bad.com", "tier": "A"}]


# --- POST /runs/{run_id}/qualify/retry-failed --------------------------------

def test_start_qualify_retry_queues_a_job_for_failed_domains(monkeypatch, client):
    monkeypatch.setattr(qualify_mod, "failed_rows", lambda conn, run_id: [{"domain": "bad.com"}])
    r = client.post("/runs/t1/qualify/retry-failed")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] is not None
    assert body["companies"] == 1


def test_start_qualify_retry_nothing_failed_returns_null_job_id(monkeypatch, client):
    monkeypatch.setattr(qualify_mod, "failed_rows", lambda conn, run_id: [])
    r = client.post("/runs/t1/qualify/retry-failed")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] is None
    assert "message" in body


# --- company_detail's qualification_attempt field ----------------------------

def test_company_detail_includes_qualification_attempt(monkeypatch, client):
    monkeypatch.setattr(db, "get_company", lambda c, d: Company(domain="acme.com", name="Acme"))
    monkeypatch.setattr(db, "get_scores", lambda c, r: [])
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: None)
    monkeypatch.setattr(db, "get_qualification_attempt", lambda c, d, r: {
        "status": "failed", "error_reason": "timeout", "error_detail": "timed out",
        "attempt_count": 2, "last_attempted_at": "2026-09-20T00:00:00Z",
    })
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [])

    r = client.get("/runs/t1/companies/acme.com")
    assert r.status_code == 200
    body = r.json()
    assert body["qualification"] is None
    assert body["qualification_attempt"]["status"] == "failed"
    assert body["qualification_attempt"]["error_reason"] == "timeout"


def test_company_detail_qualification_attempt_none_when_never_attempted(monkeypatch, client):
    monkeypatch.setattr(db, "get_company", lambda c, d: Company(domain="acme.com", name="Acme"))
    monkeypatch.setattr(db, "get_scores", lambda c, r: [])
    monkeypatch.setattr(db, "get_postings", lambda c, d: [])
    monkeypatch.setattr(db, "get_qualification", lambda c, d, r: None)
    monkeypatch.setattr(db, "get_qualification_attempt", lambda c, d, r: None)
    monkeypatch.setattr(db, "get_contacts", lambda c, d: [])

    r = client.get("/runs/t1/companies/acme.com")
    assert r.status_code == 200
    assert r.json()["qualification_attempt"] is None
