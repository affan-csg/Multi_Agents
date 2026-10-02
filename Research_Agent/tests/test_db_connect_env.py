"""Research db.connect() requires the Research-specific Supabase variables."""

from __future__ import annotations

import pytest

from csg import db


@pytest.fixture(autouse=True)
def _clear_supabase_env(monkeypatch):
    for var in (
        "RESEARCH_SUPABASE_URL", "RESEARCH_SUPABASE_SERVICE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_connect_uses_research_scoped_supabase_vars(monkeypatch):
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://research.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "research-key")
    conn = db.connect()
    assert conn.base.startswith("https://research.supabase.co")


def test_connect_rejects_unscoped_supabase_vars(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://unscoped.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "unscoped-key")
    with pytest.raises(db.SupabaseError, match="RESEARCH_SUPABASE_URL"):
        db.connect()


def test_connect_raises_when_nothing_set():
    with pytest.raises(db.SupabaseError):
        db.connect()


def test_explicit_args_still_win_over_every_env_var(monkeypatch):
    monkeypatch.setenv("RESEARCH_SUPABASE_URL", "https://research-scoped.supabase.co")
    monkeypatch.setenv("RESEARCH_SUPABASE_SERVICE_KEY", "research-scoped-key")
    conn = db.connect(url="https://explicit.supabase.co", service_key="explicit-key")
    assert conn.base.startswith("https://explicit.supabase.co")
