"""Global company de-duplication across research runs.

Offline: a fake in-memory PostgREST (same idea as test_db_roundtrip.FakeConn,
plus insert_if_absent / delete / in. filters) drives the real db.py and
discover.run_discovery code paths. Only the network edges are faked -- the
discovery provider, LLM enrichment, scoring pipeline and the Outreach client.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from csg import db, discover
from csg.dedupe import ExistingCompanies, find_duplicate_groups, normalize_domain, normalize_name
from csg.discovery.base import CompanyCandidate
from csg.discovery.resolver import deduplicate_and_resolve
from csg.models import Company

KEYS = {
    "companies": ("domain",),
    "scores": ("domain", "run_id"),
    "runs": ("run_id",),
    "run_company_matches": ("run_id", "domain"),
    "discovery_sources": ("domain", "provider_name"),
}


class FakeConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.lock = threading.Lock()

    def _key(self, table, row):
        return tuple(row.get(k) for k in KEYS.get(table, ("domain",)))

    def upsert(self, table, rows, on_conflict=""):
        with self.lock:
            store = self.tables.setdefault(table, [])
            for row in rows:
                for i, e in enumerate(store):
                    if self._key(table, e) == self._key(table, row):
                        store[i] = dict(row)
                        break
                else:
                    store.append(dict(row))
        return rows

    def insert_if_absent(self, table, rows, on_conflict=""):
        inserted = []
        with self.lock:
            store = self.tables.setdefault(table, [])
            for row in rows:
                if not any(self._key(table, e) == self._key(table, row) for e in store):
                    store.append(dict(row))
                    inserted.append(row)
        return inserted

    def _filter(self, table, filters):
        rows = self.tables.get(table, [])
        for field, expr in filters.items():
            if expr.startswith("eq."):
                rows = [r for r in rows if str(r.get(field)) == expr[3:]]
            elif expr.startswith("in."):
                wanted = set(expr[4:-1].split(","))
                rows = [r for r in rows if str(r.get(field)) in wanted]
        return rows

    def select(self, table, select="*", limit=None, order=None, **filters):
        rows = self._filter(table, filters)
        return [dict(r) for r in (rows[:limit] if limit else rows)]

    def update(self, table, values, **filters):
        for r in self._filter(table, filters):
            r.update(values)

    def delete(self, table, **filters):
        gone = self._filter(table, filters)
        self.tables[table] = [r for r in self.tables.get(table, []) if r not in gone]

    def commit(self): ...
    def close(self): ...


# --- pure normalization -----------------------------------------------------

@pytest.mark.parametrize("raw", [
    "company.com", "COMPANY.COM", "www.company.com", "https://www.Company.com/",
    "http://company.com/careers?x=1#y", "company.com:443", "company.com.",
    "  https://user@www.company.com:8080/a/b  ",
])
def test_domain_variants_normalize_to_one_key(raw):
    assert normalize_domain(raw) == "company.com"


def test_name_variants_normalize_to_one_key():
    keys = {normalize_name(n) for n in ("Acme Staffing, Inc.", "ACME STAFFING INC", "Acme  Staffing LLC", "Acmé Staffing")}
    assert keys == {"acmestaffing"}


def test_short_or_generic_names_never_match_by_name():
    assert normalize_name("Inc") == ""
    assert normalize_name("Ace Co") == ""  # too short to identify a company


def test_resolver_collapses_url_variants_within_a_batch():
    now = datetime.now()
    cands = [CompanyCandidate(domain=d, source="t", discovered_at=now)
             for d in ("Foo.com", "https://www.foo.com/jobs", "foo.com:443", "bar.com", "")]
    assert [c.domain for c in deduplicate_and_resolve(cands)] == ["foo.com", "bar.com"]


def test_existing_lookup_prefers_domain_then_name():
    idx = ExistingCompanies([{"domain": "acme.com", "name": "Acme Staffing Inc"}])
    assert idx.lookup("https://WWW.acme.com/x", "Other").matched_by == "domain"
    m = idx.lookup("acme-staffing.io", "ACME Staffing, LLC")
    assert (m.domain, m.matched_by) == ("acme.com", "name")
    assert idx.lookup("different.com", "Different Corp") is None
    assert idx.lookup("x.com", "Ace") is None


def test_find_duplicate_groups_reports_legacy_duplicates():
    rows = [{"domain": "foo.com", "name": "Foo"}, {"domain": "Foo.com", "name": "Foo"},
            {"domain": "a.com", "name": "Globex Corp"}, {"domain": "b.com", "name": "Globex Inc"},
            {"domain": "solo.com", "name": "Solo Systems"}]
    kinds = {(g["kind"], tuple(g["domains"])) for g in find_duplicate_groups(rows)}
    assert ("domain", ("Foo.com", "foo.com")) in kinds
    assert ("name", ("a.com", "b.com")) in kinds
    assert len(kinds) == 2


# --- claim_company: atomic, never overwrites --------------------------------

def test_claim_company_inserts_once_and_never_overwrites():
    conn = FakeConn()
    assert db.claim_company(conn, Company(domain="acme.com", name="Acme", region="GA", status="scored")) is True
    assert db.claim_company(conn, Company(domain="acme.com", name="Changed", region="TX")) is False
    row = conn.tables["companies"][0]
    assert (row["name"], row["region"], row["status"]) == ("Acme", "GA", "scored")


def test_concurrent_claims_create_exactly_one_company():
    conn = FakeConn()
    wins: list[bool] = []

    def go():
        wins.append(db.claim_company(conn, Company(domain="race.com", name="Race")))

    threads = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1
    assert len(conn.tables["companies"]) == 1


# --- run_discovery end to end (network edges faked) --------------------------

class _Env:
    """Wires run_discovery to fakes and records what reached Outreach."""

    def __init__(self, monkeypatch, conn: FakeConn) -> None:
        self.conn = conn
        self.found: list[tuple[str, str]] = []          # (domain, name) the provider returns
        self.import_calls: list[str] = []
        self.campaigns: list[dict] = []
        self.pipeline_domains: list[list[str]] = []
        self.enriched: list[str] = []
        env = self

        class Provider:
            name = "fake"
            def __init__(self, **_): ...
            async def discover(self, region, limit):
                now = datetime.now()
                return [CompanyCandidate(domain=d, name=n, source="fake", discovered_at=now) for d, n in env.found]

        class Fetcher:
            def __init__(self, **_): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): ...

        async def enrich(company):
            env.enriched.append(company.domain)

        async def fake_pipeline(cfg, companies, conn_, run_id, **kw):
            env.pipeline_domains.append([c.domain for c in companies])
            db.start_run(conn_, run_id, "default", len(companies))
            for c in companies:
                c.status = "scored"
                db.upsert_company(conn_, c)
                conn_.upsert("scores", [{"domain": c.domain, "run_id": run_id, "score": 90.0, "tier": "A",
                                         "scored_at": "2026-10-07T00:00:00"}], on_conflict="domain,run_id")
            return SimpleNamespace(tiers={"A": len(companies)}, companies=len(companies))

        async def fake_qualify(conn_, run_id, top_n, stats, on_progress):
            stats.qualify = {"ok": True}

        class OutreachClient:
            def __init__(self, base): ...
            def close(self): ...
            def import_prospects(self, run_id, tiers):
                env.import_calls.append(run_id)
                domains = [r["domain"] for r in env.conn.tables.get("scores", []) if r["run_id"] == run_id]
                return {"imported": len(domains), "domains": domains}
            def create_campaign(self, **kw):
                env.campaigns.append(kw)
                return {"campaign": {"campaign_id": f"c{len(env.campaigns)}"}, "item_count": 1}

        import csg.outreach_client as oc
        monkeypatch.setattr(oc, "OutreachClient", OutreachClient)
        monkeypatch.setattr(discover, "WebSearchProvider", Provider)
        monkeypatch.setattr(discover, "Fetcher", Fetcher)
        monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)
        monkeypatch.setattr(discover.pipeline, "run", fake_pipeline)
        monkeypatch.setattr(discover, "_auto_qualify_and_contact", fake_qualify)
        monkeypatch.setattr(discover.export, "to_csv", lambda *a, **k: "x.csv")
        monkeypatch.setattr(discover.export, "to_jsonl", lambda *a, **k: "x.jsonl")
        self.cfg = SimpleNamespace(
            resolve_region=lambda r: r or "GA", raw={"profile": "default"}, auto_qualify_top_n=5,
            outreach_api_base="http://fake-outreach",
            http={"user_agent": "t", "per_host_delay_seconds": 0, "timeout_seconds": 1,
                  "cache_ttl_hours": 1, "max_concurrency": 1},
        )

    def run(self, run_id: str, region: str = "GA"):
        return asyncio.run(discover.run_discovery(self.cfg, region=region, conn=self.conn, run_id=run_id))

    @property
    def companies(self):
        return self.conn.tables.get("companies", [])


@pytest.fixture
def env(monkeypatch):
    return _Env(monkeypatch, FakeConn())


def test_same_company_in_two_runs_is_not_duplicated(env):
    env.found = [("acme.com", "Acme Staffing")]
    a = env.run("disc-A", region="GA")
    assert a.duplicates_existing == 0 and len(env.companies) == 1
    env.companies[0]["status"] = "scored"

    env.found = [("acme.com", "Acme Staffing")]
    b = env.run("disc-B", region="TX")

    assert len(env.companies) == 1
    assert env.companies[0]["region"] == "GA" and env.companies[0]["status"] == "scored"  # not reset
    assert b.duplicates_existing == 1
    assert b.existing_matches == [{"domain": "acme.com", "matched_by": "domain"}]
    assert [s["run_id"] for s in env.conn.tables["scores"]] == ["disc-A"]  # no second score row
    assert env.enriched.count("acme.com") == 1  # not re-enriched for run B


def test_url_and_name_formatting_variants_match_the_existing_company(env):
    env.found = [("acme.com", "Acme Staffing, Inc.")]
    env.run("disc-A")
    env.found = [("https://www.ACME.com/careers", "Other Name"), ("acme-jobs.io", "ACME STAFFING INC"),
                 ("brandnew.com", "Brand New Co Ltd")]
    b = env.run("disc-B")

    assert sorted(c["domain"] for c in env.companies) == ["acme.com", "brandnew.com"]
    assert {(m["domain"], m["matched_by"]) for m in b.existing_matches} == {("acme.com", "domain"), ("acme.com", "name")} \
        or [m["domain"] for m in b.existing_matches] == ["acme.com"]
    assert env.pipeline_domains[-1] == ["brandnew.com"]


def test_deleted_company_can_be_added_again(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    # Deleting run A removes the company (nothing else scored it); the real DB
    # cascades -- the fake needs it spelled out.
    assert db.delete_run(env.conn, "disc-A") == 1
    assert env.companies == []

    env.found = [("acme.com", "Acme Staffing")]
    b = env.run("disc-B")
    assert b.duplicates_existing == 0
    assert [c["domain"] for c in env.companies] == ["acme.com"]
    assert [s["run_id"] for s in env.conn.tables["scores"]] == ["disc-B"]


def test_rerunning_same_run_id_is_idempotent(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    again = env.run("disc-A")  # resume/re-run of the same run: its own company, not a duplicate
    assert again.duplicates_existing == 0
    assert len(env.companies) == 1 and len(env.conn.tables["scores"]) == 1


def test_race_loser_treats_company_as_existing_instead_of_overwriting(env, monkeypatch):
    env.found = [("acme.com", "Acme Staffing")]
    real_claim = db.claim_company

    def claim_after_rival(conn, c):
        # a concurrent run inserts between our existence check and our claim
        real_claim(conn, Company(domain=c.domain, name="Rival", region="NY", status="scored"))
        return real_claim(conn, c)

    monkeypatch.setattr(db, "claim_company", claim_after_rival)
    s = env.run("disc-B")

    assert len(env.companies) == 1 and env.companies[0]["name"] == "Rival"
    assert s.duplicates_existing == 1
    assert env.pipeline_domains == []  # nothing left to score
    assert env.import_calls == [] and env.campaigns == []


def test_existing_company_does_not_create_another_campaign(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    assert env.import_calls == ["disc-A"] and len(env.campaigns) == 1

    env.found = [("www.Acme.com", "Acme Staffing")]
    b = env.run("disc-B")

    assert b.duplicates_existing == 1
    assert len(env.campaigns) == 1                      # still only run A's campaign
    assert "disc-B" not in env.import_calls             # nothing handed to Outreach for B
    assert b.outreach is None


def test_matches_are_listed_for_the_run_but_never_keep_a_company_alive(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    env.found = [("acme.com", "Acme Staffing"), ("new.com", "New Thing")]
    env.run("disc-B")

    rows = db.get_run_matches(env.conn, "disc-B")
    assert [(r["domain"], r["existing_match"], r["matched_from_run"]) for r in rows] == [("acme.com", True, "disc-A")]

    # Deleting B drops its match rows but leaves A's company in place.
    db.delete_run(env.conn, "disc-B")
    assert env.conn.tables["run_company_matches"] == []
    assert "acme.com" in [c["domain"] for c in env.companies]
    # Deleting A removes the company (B's match never counted as a reference).
    db.delete_run(env.conn, "disc-A")
    assert "acme.com" not in [c["domain"] for c in env.companies]
