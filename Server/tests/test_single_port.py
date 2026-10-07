"""Tests for the single-port composition layer (Server/app.py).

These lock in that mounting both agents' existing apps under one FastAPI
instance is pure wiring: no route is missing, both lifespans actually run
(so background jobs and WebSocket progress still work), each agent still
resolves its own separate database, and the Calendly webhook is reachable at
both its pre-existing path and its new prefixed one.

Fully offline, same style as each agent's own API tests: db.connect is
patched to a fake in-memory connection so no real Supabase project is
touched. `app` (the composed FastAPI instance) is imported once at module
level -- exactly how it's actually run -- and _configure_env() has already
run against this test process's real environment (see conftest.py's
`scoped_env`, applied via monkeypatch before most assertions that depend on
scoped resolution).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import app as server_app
from csg import api as research_api
from csg import db as research_db
from outreach import api as outreach_api
from outreach import db as outreach_db


class _FakeConn:
    """Minimal stand-in good enough for the read-only smoke routes exercised
    here -- these tests are about mounting/wiring, not each agent's DB
    logic (already covered by each agent's own test suite)."""

    def select(self, *a, **kw):
        return []

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _stub_both_dbs(monkeypatch):
    """Every route under test goes through db.connect() at some point (job
    starters, list endpoints, etc); this keeps the whole suite offline
    without touching either agent's actual Supabase project."""
    monkeypatch.setattr(research_db, "connect", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(research_api.db, "connect", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(outreach_db, "connect", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(outreach_api.db, "connect", lambda *a, **kw: _FakeConn())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pw")
    # `with` form is required for the lifespan (and therefore _MAIN_LOOP on
    # both sub-apps) to actually run -- bare TestClient(app) does not invoke
    # lifespan, same caveat as each agent's own API test suite documents.
    with TestClient(server_app.app) as c:
        c.headers["Authorization"] = f"Bearer {server_app._auth_token('test-pw')}"
        yield c


# ---------------------------------------------------------------------------
# mounting / route surface
# ---------------------------------------------------------------------------

def test_research_routes_reachable_under_prefix(client):
    resp = client.get("/research/regions")
    assert resp.status_code == 200


def test_outreach_routes_reachable_under_prefix(client):
    resp = client.get("/outreach/calendly/status")
    assert resp.status_code == 200


def test_research_route_not_reachable_unprefixed(client):
    """Guards against silently mounting at "/" instead of "/research" --
    the whole point of prefixing is that these two APIs' routes (both have
    /jobs/{id}, /jobs/{id}/ws) do not collide."""
    resp = client.get("/regions")
    assert resp.status_code == 404


def test_outreach_route_not_reachable_unprefixed(client):
    resp = client.get("/campaigns")
    assert resp.status_code == 404


def test_every_research_route_is_mounted_under_prefix():
    """No route lost in translation: every path the standalone Research app
    registers is reachable at the same path under /research on the composed
    app (except its own catch-all/docs internals, which starlette handles
    per-sub-app anyway)."""
    research_paths = {r.path for r in research_api.app.routes if hasattr(r, "path")}
    mounted = next(r for r in server_app.app.routes if getattr(r, "path", None) == "/research")
    mounted_paths = {r.path for r in mounted.app.routes if hasattr(r, "path")}
    assert research_paths == mounted_paths


def test_every_outreach_route_is_mounted_under_prefix():
    outreach_paths = {r.path for r in outreach_api.app.routes if hasattr(r, "path")}
    mounted = next(r for r in server_app.app.routes if getattr(r, "path", None) == "/outreach")
    mounted_paths = {r.path for r in mounted.app.routes if hasattr(r, "path")}
    assert outreach_paths == mounted_paths


# ---------------------------------------------------------------------------
# lifespan chaining -- _MAIN_LOOP must be captured on BOTH sub-apps, or every
# background job (discover/qualify/contacts/import/send) and WebSocket
# progress publish silently breaks.
# ---------------------------------------------------------------------------

def test_both_sub_app_lifespans_run(client):
    assert research_api._MAIN_LOOP is not None
    assert outreach_api._MAIN_LOOP is not None
    # Both point at the SAME running loop (the server's one loop) -- this is
    # what lets Job.publish() on either agent's jobs safely call
    # call_soon_threadsafe from a background thread.
    assert research_api._MAIN_LOOP is outreach_api._MAIN_LOOP


# ---------------------------------------------------------------------------
# WebSockets survive mounting
# ---------------------------------------------------------------------------

def test_research_websocket_unknown_job_closes_4404(client):
    with client.websocket_connect("/research/jobs/does-not-exist/ws") as ws:
        assert ws.receive() == {"type": "websocket.close", "code": 4404, "reason": ""}


def test_outreach_websocket_unknown_job_closes_4404(client):
    with client.websocket_connect("/outreach/jobs/does-not-exist/ws") as ws:
        assert ws.receive() == {"type": "websocket.close", "code": 4404, "reason": ""}


# ---------------------------------------------------------------------------
# CORS survives mounting (each sub-app keeps its own CORSMiddleware)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["/research/regions", "/outreach/calendly/status"])
def test_cors_preflight_allows_local_dev_origin(client, prefix):
    resp = client.options(
        prefix,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


@pytest.mark.parametrize("prefix", ["/research/regions", "/outreach/calendly/status"])
def test_cors_preflight_allows_vercel_origin(client, prefix):
    origin = "https://multi-agents-beta.vercel.app"
    resp = client.options(
        prefix,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == origin


# ---------------------------------------------------------------------------
# Calendly webhook: root alias + prefixed path both reach the same handler
# ---------------------------------------------------------------------------

def test_calendly_webhook_root_alias_reachable(client, monkeypatch):
    from outreach.calendly import routes as calendly_routes

    class _FakeCfg:
        calendly_webhook_signing_key = ""

    monkeypatch.setattr(calendly_routes, "load_config", lambda: _FakeCfg())
    resp = client.post("/calendly/webhook", json={"event": "ping"})
    # Signing key unset -> verify_signature's own behavior applies; either
    # way both paths must produce the IDENTICAL response, proving the alias
    # calls the exact same handler as the prefixed route (not a re-implementation).
    prefixed = client.post("/outreach/calendly/webhook", json={"event": "ping"})
    assert resp.status_code == prefixed.status_code
    assert resp.json() == prefixed.json()


# ---------------------------------------------------------------------------
# env configuration (Server/app.py's _configure_env, exercised directly --
# does not touch the already-imported agent modules, only verifies the
# derivation logic and the fallback each db.connect() got)
# ---------------------------------------------------------------------------

# These call the real connect() (not the autouse _stub_both_dbs fake)
# to verify env-var resolution order itself, so they import it fresh under
# a different name before the module-level `research_db.connect` /
# `outreach_db.connect` attributes get monkeypatched by the autouse fixture.
from csg.db import connect as _real_research_connect  # noqa: E402
from outreach.db import connect as _real_outreach_connect  # noqa: E402


def test_research_db_connect_prefers_scoped_var(monkeypatch):
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://research-only.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "research-key")
    monkeypatch.setenv("SUPABASE_URL", "https://unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")
    conn = _real_research_connect()
    assert conn.base.startswith("https://research-only.supabase.co")


def test_outreach_db_connect_prefers_scoped_var(monkeypatch):
    monkeypatch.setenv("OUTREACH_SUPABASE_URL", "https://outreach-only.supabase.co")
    monkeypatch.setenv("OUTREACH_SUPABASE_SERVICE_KEY", "outreach-key")
    monkeypatch.setenv("SUPABASE_URL", "https://unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")
    conn = _real_outreach_connect()
    assert conn.base.startswith("https://outreach-only.supabase.co")


def test_db_connect_rejects_unscoped_vars_when_scoped_vars_are_unset(monkeypatch):
    """Legacy shared names must never select a database for either agent."""
    for scoped in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
        "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
    ):
        monkeypatch.delenv(scoped, raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://only-unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")

    with pytest.raises(research_db.SupabaseError):
        _real_research_connect()
    with pytest.raises(outreach_db.SupabaseError):
        _real_outreach_connect()


def test_configure_env_sets_prefixed_cross_agent_bases(tmp_path, monkeypatch):
    """Exercises _configure_env() in isolation against fake .env files, so
    this doesn't depend on -- or mutate -- the developer's real
    Research_Agent/.env / Outreach_Agent/.env."""
    for var in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
        "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
        "RESEARCH_API_BASE", "OUTREACH_API_BASE", "CSG_API_BASE",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    research_env = tmp_path / "research.env"
    outreach_env = tmp_path / "outreach.env"
    research_env.write_text(
        "RESEARCH_SUPABASE_URL=https://r.supabase.co\n"
        "RESEARCH_SUPABASE_SERVICE_KEY=r-key\n"
        "OUTREACH_API_BASE=http://127.0.0.1:8001\n",  # hand-off WAS configured
        encoding="utf-8",
    )
    outreach_env.write_text(
        "OUTREACH_SUPABASE_URL=https://o.supabase.co\n"
        "OUTREACH_SUPABASE_SERVICE_KEY=o-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server_app, "RESEARCH_ENV", research_env)
    monkeypatch.setattr(server_app, "OUTREACH_ENV", outreach_env)

    server_app._configure_env()

    assert os.environ["RESEARCH_SUPABASE_URL"] == "https://r.supabase.co"
    assert os.environ["OUTREACH_SUPABASE_URL"] == "https://o.supabase.co"
    assert os.environ["RESEARCH_API_BASE"] == "http://127.0.0.1:8000/research"
    # Hand-off stays enabled (was configured in Research's .env), but now
    # points at this server's own prefix instead of the old standalone :8001.
    assert os.environ["OUTREACH_API_BASE"] == "http://127.0.0.1:8000/outreach"
    assert os.environ["RESEARCH_SUPABASE_SERVICE_KEY"] == "r-key"
    assert os.environ["OUTREACH_SUPABASE_SERVICE_KEY"] == "o-key"


def test_configure_env_leaves_handoff_disabled_when_unconfigured(tmp_path, monkeypatch):
    """When Research's own .env never set OUTREACH_API_BASE, the server must
    NOT silently enable the hand-off -- unset must keep meaning disabled."""
    for var in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
        "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
        "RESEARCH_API_BASE", "OUTREACH_API_BASE", "CSG_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)

    research_env = tmp_path / "research.env"
    outreach_env = tmp_path / "outreach.env"
    research_env.write_text("RESEARCH_SUPABASE_URL=https://r.supabase.co\nRESEARCH_SUPABASE_SERVICE_KEY=r-key\n", encoding="utf-8")
    outreach_env.write_text("OUTREACH_SUPABASE_URL=https://o.supabase.co\nOUTREACH_SUPABASE_SERVICE_KEY=o-key\n", encoding="utf-8")
    monkeypatch.setattr(server_app, "RESEARCH_ENV", research_env)
    monkeypatch.setattr(server_app, "OUTREACH_ENV", outreach_env)

    server_app._configure_env()

    assert "OUTREACH_API_BASE" not in os.environ


def test_configure_env_preserves_render_supabase_values(tmp_path, monkeypatch):
    for var in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
        "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
        "RESEARCH_API_BASE", "OUTREACH_API_BASE", "CSG_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://render-research.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "render-research-key")
    monkeypatch.setenv("OUTREACH_SUPABASE_URL", "https://render-outreach.supabase.co")
    monkeypatch.setenv("OUTREACH_SUPABASE_SERVICE_KEY", "render-outreach-key")

    research_env = tmp_path / "research.env"
    outreach_env = tmp_path / "outreach.env"
    research_env.write_text("RESEARCH_SUPABASE_URL=https://file-research.supabase.co\n", encoding="utf-8")
    outreach_env.write_text("OUTREACH_SUPABASE_URL=https://file-outreach.supabase.co\n", encoding="utf-8")
    monkeypatch.setattr(server_app, "RESEARCH_ENV", research_env)
    monkeypatch.setattr(server_app, "OUTREACH_ENV", outreach_env)

    server_app._configure_env()

    assert os.environ["RESEARCH_SUPABASE_URL"] == "https://render-research.supabase.co"
    assert os.environ["OUTREACH_SUPABASE_URL"] == "https://render-outreach.supabase.co"


def test_configure_env_warns_on_shared_key_mismatch(tmp_path, monkeypatch, caplog):
    for var in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
        "OUTREACH_SUPABASE_URL", "OUTREACH_SUPABASE_SERVICE_KEY",
        "RESEARCH_API_BASE", "OUTREACH_API_BASE", "CSG_API_BASE",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    research_env = tmp_path / "research.env"
    outreach_env = tmp_path / "outreach.env"
    research_env.write_text(
        "RESEARCH_SUPABASE_URL=https://r.supabase.co\nRESEARCH_SUPABASE_SERVICE_KEY=r-key\nGEMINI_API_KEY=key-a\n",
        encoding="utf-8",
    )
    outreach_env.write_text(
        "OUTREACH_SUPABASE_URL=https://o.supabase.co\nOUTREACH_SUPABASE_SERVICE_KEY=o-key\nGEMINI_API_KEY=key-b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server_app, "RESEARCH_ENV", research_env)
    monkeypatch.setattr(server_app, "OUTREACH_ENV", outreach_env)

    with caplog.at_level("WARNING", logger="csg_server"):
        server_app._configure_env()

    assert any("GEMINI_API_KEY" in rec.message for rec in caplog.records)


# --- dashboard password protection ---------------------------------------

def test_dashboard_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-for-test")
    client = TestClient(server_app.app)

    assert client.get("/research/runs").status_code == 401
    assert client.get("/outreach/campaigns", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/auth/login", json={"password": "wrong"}).status_code == 401

    token = client.post("/auth/login", json={"password": "pw-for-test"}).json()["token"]
    ok = client.get("/research/runs", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code != 401
    assert client.get(f"/research/runs?token={token}").status_code != 401
    # Unauthenticated websocket handshakes are refused.
    with pytest.raises(Exception):
        with client.websocket_connect("/research/jobs/x/ws"):
            pass
    # Third-party webhook stays reachable without the token.
    # (its own signature check may still answer; it just isn't *our* gate)
    hook = client.post("/calendly/webhook", content=b"{}")
    assert hook.status_code != 503 and hook.json().get("detail") != "Unauthorized"


def test_dashboard_auth_fails_closed_without_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    client = TestClient(server_app.app)
    assert client.get("/research/runs").status_code == 503
    assert client.post("/auth/login", json={"password": ""}).status_code == 503
