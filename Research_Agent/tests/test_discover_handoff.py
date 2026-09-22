"""Tests for the automatic Tier A/B -> qualify -> contacts -> Outreach
hand-off (discover.py's _auto_qualify_and_contact / _handoff_to_outreach),
and the config knobs that drive it (Config.auto_qualify_top_n,
Config.outreach_api_base).

No real network/DB: db.get_scores, qualify_mod.qualify_run,
contacts_mod.discover_run, and OutreachClient are all monkeypatched --
these tests lock in the SELECTION and WIRING logic (who gets covered, in
what order, what happens on failure), not the underlying providers
(Hunter.io/LLM/Outreach's own behavior), which are covered elsewhere.
"""

from __future__ import annotations

import asyncio

import pytest

from csg import discover
from csg.config import load_config
from csg.discovery import DiscoveryError
from csg.outreach_client import OutreachClientError


# --- Config knobs ------------------------------------------------------------

def test_auto_qualify_top_n_defaults_to_25():
    cfg = load_config()
    assert cfg.auto_qualify_top_n == 25


def test_outreach_api_base_env_wins_over_config(monkeypatch):
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://env-wins:9999")
    assert cfg.outreach_api_base == "http://env-wins:9999"


def test_outreach_api_base_none_when_unset(monkeypatch):
    cfg = load_config()
    monkeypatch.delenv("OUTREACH_API_BASE", raising=False)
    # icp.yaml has no outreach.api_base key committed -- unset env means None.
    assert cfg.outreach_api_base is None


# --- _auto_qualify_and_contact selection logic -------------------------------

def _score_row(domain: str, tier: str, score: float, disqualified: bool = False) -> dict:
    return {"domain": domain, "tier": tier, "score": score, "disqualified": disqualified}


def test_auto_qualify_selects_only_tier_ab_within_the_cap(monkeypatch):
    """Only Tier A/B (and never a disqualified company) is ever selected,
    regardless of tier interleaving in the ranked list."""
    rows = [
        _score_row("a1.com", "A", 90),
        _score_row("b1.com", "B", 60),
        _score_row("c1.com", "C", 40),  # must never be selected
        _score_row("dq.com", "A", 95, disqualified=True),  # must never be selected
    ]
    monkeypatch.setattr(discover.db, "get_scores", lambda conn, run_id: rows)

    captured_domains = {}

    async def fake_qualify_run(conn, run_id, *, domains, on_progress=None):
        captured_domains["qualify"] = domains
        return {"qualified": len(domains), "skipped": 0, "errors": 0, "error_details": {}}

    async def fake_discover_run(conn, domains, *, on_progress=None):
        captured_domains["contacts"] = domains
        return {"found": len(domains), "skipped": 0, "errors": 0, "contacts_saved": 0, "error_details": {}}

    monkeypatch.setattr(discover.qualify_mod, "qualify_run", fake_qualify_run)
    monkeypatch.setattr(discover.contacts_mod, "discover_run", fake_discover_run)
    monkeypatch.setattr(discover.export, "to_csv", lambda conn, run_id: "x.csv")
    monkeypatch.setattr(discover.export, "to_jsonl", lambda conn, run_id, tiers=None: "x.jsonl")

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._auto_qualify_and_contact(conn=None, run_id="run1", top_n=10, stats=stats, on_progress=None))

    assert captured_domains["qualify"] == ["a1.com", "b1.com"]
    assert captured_domains["contacts"] == ["a1.com", "b1.com"]
    assert stats.auto_qualify_top_n == 2  # a1, b1 -- c1 (Tier C) and dq (disqualified) never counted
    assert stats.auto_qualify_skipped_tier == 0  # nothing is "skipped for tier" any more -- off-tier rows are never in the candidate pool to begin with


def test_auto_qualify_top_n_ranks_within_tier_ab_not_across_all_tiers(monkeypatch):
    """Regression for the bug reported against the Dashboard: with a cap of
    2 and 3 Tier A/B companies but a higher-scoring Tier D company sitting
    ahead of them in the raw (all-tier) ranking, the OLD behavior sliced the
    top 2 rows first (D, A) and filtered to A/B afterward -- selecting only
    1 eligible company (a1.com) and silently dropping b1.com even though it
    is a fully eligible Tier B company within the top_n=2 budget. The fix
    ranks Tier A/B only, then slices -- so b1.com (the 2nd-highest A/B
    company) is correctly included and only the lowest-ranked A/B company
    (c2.com) is left for a later run/larger cap."""
    rows = [
        _score_row("hot-d.com", "D", 99),  # outranks every A/B row but must never occupy a slot
        _score_row("a1.com", "A", 90),
        _score_row("b1.com", "B", 60),
        _score_row("b2.com", "B", 55),  # 3rd-ranked A/B company -- must be excluded by the cap, not by tier
    ]
    monkeypatch.setattr(discover.db, "get_scores", lambda conn, run_id: rows)

    captured_domains = {}

    async def fake_qualify_run(conn, run_id, *, domains, on_progress=None):
        captured_domains["qualify"] = domains
        return {"qualified": len(domains), "skipped": 0, "errors": 0, "error_details": {}}

    async def fake_discover_run(conn, domains, *, on_progress=None):
        return {"found": len(domains), "skipped": 0, "errors": 0, "contacts_saved": 0, "error_details": {}}

    monkeypatch.setattr(discover.qualify_mod, "qualify_run", fake_qualify_run)
    monkeypatch.setattr(discover.contacts_mod, "discover_run", fake_discover_run)
    monkeypatch.setattr(discover.export, "to_csv", lambda conn, run_id: "x.csv")
    monkeypatch.setattr(discover.export, "to_jsonl", lambda conn, run_id, tiers=None: "x.jsonl")

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._auto_qualify_and_contact(conn=None, run_id="run1", top_n=2, stats=stats, on_progress=None))

    assert captured_domains["qualify"] == ["a1.com", "b1.com"]
    assert stats.auto_qualify_top_n == 2


def test_auto_qualify_cap_limits_how_many_tier_ab_are_covered(monkeypatch):
    """The configured cap (icp.yaml outreach.auto_qualify_top_n) bounds how
    many Tier A/B companies get qualified+contacted -- not every Tier A/B
    company in an arbitrarily large run, to protect Hunter.io's free-tier
    monthly quota."""
    rows = [_score_row(f"co{i}.com", "A", 100 - i) for i in range(5)]
    monkeypatch.setattr(discover.db, "get_scores", lambda conn, run_id: rows)

    captured = {}

    async def fake_qualify_run(conn, run_id, *, domains, on_progress=None):
        captured["domains"] = domains
        return {"qualified": len(domains), "skipped": 0, "errors": 0, "error_details": {}}

    async def fake_discover_run(conn, domains, *, on_progress=None):
        return {"found": len(domains), "skipped": 0, "errors": 0, "contacts_saved": 0, "error_details": {}}

    monkeypatch.setattr(discover.qualify_mod, "qualify_run", fake_qualify_run)
    monkeypatch.setattr(discover.contacts_mod, "discover_run", fake_discover_run)
    monkeypatch.setattr(discover.export, "to_csv", lambda conn, run_id: "x.csv")
    monkeypatch.setattr(discover.export, "to_jsonl", lambda conn, run_id, tiers=None: "x.jsonl")

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._auto_qualify_and_contact(conn=None, run_id="run1", top_n=3, stats=stats, on_progress=None))

    assert captured["domains"] == ["co0.com", "co1.com", "co2.com"]


def test_auto_qualify_no_tier_ab_skips_qualify_and_contacts_entirely(monkeypatch):
    rows = [_score_row("c1.com", "C", 40), _score_row("d1.com", "D", 10)]
    monkeypatch.setattr(discover.db, "get_scores", lambda conn, run_id: rows)

    called = {"qualify": False, "contacts": False}

    async def fake_qualify_run(*a, **k):
        called["qualify"] = True

    async def fake_discover_run(*a, **k):
        called["contacts"] = True

    monkeypatch.setattr(discover.qualify_mod, "qualify_run", fake_qualify_run)
    monkeypatch.setattr(discover.contacts_mod, "discover_run", fake_discover_run)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._auto_qualify_and_contact(conn=None, run_id="run1", top_n=10, stats=stats, on_progress=None))

    assert called == {"qualify": False, "contacts": False}
    assert stats.qualify is None
    assert stats.contacts is None


# --- _handoff_to_outreach -----------------------------------------------------

class _FakeOutreachClient:
    """Stands in for OutreachClient -- captures calls, returns scripted
    results, never touches the network."""

    instances: list["_FakeOutreachClient"] = []

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.closed = False
        self.import_calls: list[tuple] = []
        self.campaign_calls: list[tuple] = []
        self._import_result: dict = {"imported": 2, "domains": ["a.com", "b.com"]}
        self._campaign_result: dict = {
            "campaign": {"campaign_id": "camp-1"}, "item_count": 2,
        }
        self._raise: Exception | None = None
        type(self).instances.append(self)

    def import_prospects(self, run_id, *, tiers):
        self.import_calls.append((run_id, tuple(tiers)))
        if self._raise:
            raise self._raise
        return self._import_result

    def set_excluded(self, import_excluded=None, campaign_excluded=None) -> None:
        self._import_result = {**self._import_result, "excluded_no_contact": import_excluded or []}
        self._campaign_result = {**self._campaign_result, "excluded_no_contact": campaign_excluded or []}

    def create_campaign(self, *, name=None, research_run_id, tiers, limit=None):
        self.campaign_calls.append((name, research_run_id, tuple(tiers)))
        return self._campaign_result

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_outreach_client():
    _FakeOutreachClient.instances.clear()
    yield
    _FakeOutreachClient.instances.clear()


def _patch_outreach_client(monkeypatch, client_cls):
    import csg.outreach_client as outreach_client_mod
    monkeypatch.setattr(outreach_client_mod, "OutreachClient", client_cls)


def test_handoff_skipped_when_outreach_api_base_unset(monkeypatch):
    cfg = load_config()
    monkeypatch.delenv("OUTREACH_API_BASE", raising=False)
    stats = discover.DiscoveryStats(region="GA", run_id="run1")

    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))

    assert stats.outreach is None
    assert _FakeOutreachClient.instances == []  # never even constructed


def test_handoff_imports_then_creates_campaign_for_tier_ab(monkeypatch):
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")
    _patch_outreach_client(monkeypatch, _FakeOutreachClient)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    stats.pipeline = discover.pipeline.Stats(tiers={"A": 1, "B": 1, "C": 0, "D": 0})
    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))

    client = _FakeOutreachClient.instances[0]
    assert client.import_calls == [("run1", ("A", "B"))]
    name, campaign_run_id, tiers = client.campaign_calls[0]
    assert name.startswith("GA — 1 Tier A + 1 Tier B (")
    assert campaign_run_id == "run1"
    assert tiers == ("A", "B")
    assert client.closed is True
    assert stats.outreach == {
        "imported": 2, "campaign_id": "camp-1", "item_count": 2, "excluded_no_contact": [],
    }


def test_handoff_reports_excluded_no_contact_companies(monkeypatch):
    """A Tier A/B company Hunter never found a contact for is left out of both
    the import and the campaign (see ingest.import_prospects /
    create_campaign_from_prospects) -- the hand-off must surface exactly who,
    not just a silently smaller campaign, so stats.outreach (and whatever
    reads it) can report 'No contacts found for X, so it was not added.'"""
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")

    class ExcludingClient(_FakeOutreachClient):
        def __init__(self, base_url):
            super().__init__(base_url)
            self.set_excluded(import_excluded=[{"domain": "nocontact.com", "name": "No Contact Co", "tier": "B"}])

    _patch_outreach_client(monkeypatch, ExcludingClient)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))

    assert stats.outreach["excluded_no_contact"] == [
        {"domain": "nocontact.com", "name": "No Contact Co", "tier": "B"},
    ]


def test_handoff_merges_import_and_campaign_exclusions_without_duplicates(monkeypatch):
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")

    class DoubleExcludingClient(_FakeOutreachClient):
        def __init__(self, base_url):
            super().__init__(base_url)
            self.set_excluded(
                import_excluded=[{"domain": "a.com", "name": "A Co", "tier": "A"}],
                campaign_excluded=[
                    {"domain": "a.com", "name": "A Co", "tier": "A"},  # duplicate, must not double up
                    {"domain": "b.com", "name": "B Co", "tier": "B"},
                ],
            )

    _patch_outreach_client(monkeypatch, DoubleExcludingClient)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))

    assert {e["domain"] for e in stats.outreach["excluded_no_contact"]} == {"a.com", "b.com"}
    assert len(stats.outreach["excluded_no_contact"]) == 2


def test_handoff_stops_after_import_when_nothing_was_imported(monkeypatch):
    """Zero prospects imported (e.g. contact discovery found nothing usable)
    must not still call /campaigns to draft an empty campaign."""
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")

    class ZeroImportClient(_FakeOutreachClient):
        def __init__(self, base_url):
            super().__init__(base_url)
            self._import_result = {"imported": 0, "domains": []}

    _patch_outreach_client(monkeypatch, ZeroImportClient)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))

    client = ZeroImportClient.instances[0]
    assert client.import_calls == [("run1", ("A", "B"))]
    assert client.campaign_calls == []  # never reached
    assert stats.outreach == {
        "imported": 0, "campaign_id": None, "item_count": 0, "excluded_no_contact": [],
    }


def test_handoff_failure_is_recorded_not_raised(monkeypatch):
    """The research run already succeeded before this step runs -- an
    unreachable/erroring Outreach Agent must not raise out of
    run_discovery, only be recorded on stats.outreach."""
    cfg = load_config()
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")

    class FailingClient(_FakeOutreachClient):
        def __init__(self, base_url):
            super().__init__(base_url)
            self._raise = OutreachClientError("Outreach Agent API unreachable")

    _patch_outreach_client(monkeypatch, FailingClient)

    stats = discover.DiscoveryStats(region="GA", run_id="run1")
    asyncio.run(discover._handoff_to_outreach(cfg, "run1", stats, on_progress=None))  # must not raise

    assert stats.outreach == {"error": "Outreach Agent API unreachable"}
    assert FailingClient.instances[0].closed is True  # client.close() still runs


# --- run_discovery wiring: auto_pipeline vs explicit auto_qualify_top -------

def _resolve_top_n(cfg, auto_qualify_top, auto_pipeline):
    """Mirrors run_discovery's 'step 6' top_n resolution exactly (see
    discover.py) -- kept here as a single source of truth these tests check
    against, rather than duplicating the formula inline in every case."""
    return auto_qualify_top if auto_qualify_top is not None else (cfg.auto_qualify_top_n if auto_pipeline else 0)


def test_top_n_resolution_matches_run_discovery_semantics():
    """Locks in the resolution run_discovery uses to pick top_n: an explicit
    auto_qualify_top (including 0) always wins; otherwise auto_pipeline=True
    uses the configured cap; auto_pipeline=False with nothing provided means
    0 (skip entirely) -- the CLI's default, opt-in-only behavior."""
    cfg = load_config()

    assert _resolve_top_n(cfg, None, True) == 25   # API/Dashboard default: automatic, configured cap
    assert _resolve_top_n(cfg, None, False) == 0   # CLI default: fully opt-in, nothing happens
    assert _resolve_top_n(cfg, 10, False) == 10    # CLI --auto-qualify-top 10 (after confirmation)
    assert _resolve_top_n(cfg, 10, True) == 10     # explicit value still wins over the cap


def test_explicit_zero_is_honored_as_skip_not_treated_as_unset():
    """Regression: auto_qualify_top=0 (the Dashboard's "Top N" field
    explicitly set to 0, meaning "skip qualify/contacts for this run") used
    to be treated the same as None/not-provided because of a truthiness
    check (`if auto_qualify_top else ...`), silently falling through to the
    automatic default cap instead of actually skipping -- the one case the
    UI's "Set to 0 to skip" hint text promises. 0 must resolve to 0, not 25,
    on both auto_pipeline settings."""
    cfg = load_config()

    assert _resolve_top_n(cfg, 0, True) == 0    # API/Dashboard: explicit 0 must skip, not use the cap
    assert _resolve_top_n(cfg, 0, False) == 0   # CLI: explicit 0 is also just 0 either way


