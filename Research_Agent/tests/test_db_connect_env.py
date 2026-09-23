"""db.connect()'s env-var resolution -- specifically the RESEARCH_SUPABASE_*
scoped fallback added for the single-port server (see Server/app.py's
module docstring). Standalone use (only SUPABASE_URL/SUPABASE_SERVICE_KEY
set, as in every other test file / the real .env) must keep working exactly
as before; this only locks in that the new scoped names, when present, win.
"""

from __future__ import annotations

import pytest

from csg import db


@pytest.fixture(autouse=True)
def _clear_supabase_env(monkeypatch):
    for var in (
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_connect_falls_back_to_unscoped_supabase_vars(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")
    conn = db.connect()
    assert conn.base.startswith("https://unscoped.supabase.co")


def test_connect_prefers_research_scoped_vars_over_unscoped(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://research-scoped.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "research-scoped-key")
    conn = db.connect()
    assert conn.base.startswith("https://research-scoped.supabase.co")


def test_connect_raises_when_nothing_set():
    with pytest.raises(db.SupabaseError):
        db.connect()


def test_explicit_args_still_win_over_every_env_var(monkeypatch):
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://research-scoped.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "research-scoped-key")
    conn = db.connect(url="https://explicit.supabase.co", service_key="explicit-key")
    assert conn.base.startswith("https://explicit.supabase.co")
