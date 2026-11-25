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
from csg.discovery import research
from csg.discovery.base import CompanyCandidate
from csg.discovery.resolver import deduplicate_and_resolve
from csg.models import Company, ProvenanceValue

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


def test_enrichment_timeout_is_counted_and_reports_progress(monkeypatch):
    candidate = CompanyCandidate(
        domain="slow.example", name="Slow", source="fake", discovered_at=datetime.now(),
    )
    progress = []

    async def never_finishes(company):
        await asyncio.Event().wait()

    monkeypatch.setattr(discover, "ENRICH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(discover, "enrich_company_firmographics", never_finishes)

    pairs, skipped, failures = asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", lambda *event: progress.append(event),
    ))

    assert len(pairs) == 1 and skipped == 0
    assert failures == {"timeout": 1}
    assert progress[0][0:3] == ("enrich", 0, 1)
    assert "researching slow.example" in progress[0][3]
    assert progress[-1][0:3] == ("enrich", 1, 1)


def test_enrichment_timeout_cancellation_does_not_mutate_company(monkeypatch):
    candidate = CompanyCandidate(
        domain="slow.example", name="Slow", source="fake", discovered_at=datetime.now(),
    )
    checkpoints = {}

    async def never_finishes(company):
        await asyncio.Event().wait()

    monkeypatch.setattr(discover, "ENRICH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(discover, "enrich_company_firmographics", never_finishes)

    pairs, _skipped, failures = asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", None,
        checkpoint_load=lambda key: checkpoints.get(key),
        checkpoint_save=lambda key, status, payload: checkpoints.update({key: {"status": status, "payload": payload}}) is None,
        run_id="disc-cancellation-test",
    ))

    assert failures == {"timeout": 1}
    assert pairs[0][1].employee_count is None
    assert checkpoints["enrich:slow.example"]["status"] == "failed"


def test_done_enrichment_checkpoint_recovers_without_api_repeat(monkeypatch):
    candidate = CompanyCandidate(
        domain="done.example", name="Done", source="fake", discovered_at=datetime.now(),
    )
    cached = Company(domain="done.example", name="Done")
    checkpoints = {
        "enrich:done.example": {
            "status": "done",
            "payload": {"company": cached.model_dump(mode="json")},
        }
    }
    calls = []

    async def should_not_run(company):
        calls.append(company.domain)
        return None

    monkeypatch.setattr(discover, "enrich_company_firmographics", should_not_run)

    pairs, _skipped, failures = asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", None,
        checkpoint_load=lambda key: checkpoints.get(key),
        checkpoint_save=lambda key, status, payload: True,
    ))

    assert calls == []
    assert failures == {}
    assert pairs[0][1].domain == "done.example"


def test_nonempty_enrichment_reason_is_saved_as_failed_checkpoint(monkeypatch):
    candidate = CompanyCandidate(
        domain="quota.example", name="Quota", source="fake", discovered_at=datetime.now(),
    )
    checkpoints = {}

    async def failed_enrichment(company):
        return "quota_exhausted"

    monkeypatch.setattr(discover, "enrich_company_firmographics", failed_enrichment)

    asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", None,
        checkpoint_save=lambda key, status, payload: checkpoints.update(
            {key: {"status": status, "payload": payload}}
        ) is None,
    ))

    assert checkpoints["enrich:quota.example"]["status"] == "failed"


def test_mixed_enrichment_attempts_each_company_and_keeps_distinct_outcomes(monkeypatch):
    candidates = [
        CompanyCandidate(domain="complete.example", name="Complete", source="fake", discovered_at=datetime.now()),
        CompanyCandidate(domain="partial.example", name="Partial", source="fake", discovered_at=datetime.now()),
    ]
    calls = []
    checkpoints = {}

    async def enrich(company, **_kwargs):
        calls.append(company.domain)
        if company.domain == "complete.example":
            company.research_outcomes = {
                field: {"status": "accepted_and_mapped", "criterion": criterion}
                for field, criterion in research._FIELD_CRITERIA.items()
            }
            company.research_outcomes["_summary"] = {"status": "complete", "missing_fields": {}, "blocking_fields": {}}
            return None
        company.research_outcomes["employee_range"] = {
            "status": "not_returned", "reason": "Gemini omitted employee_count",
        }
        company.research_outcomes["_summary"] = {
            "status": "partial", "missing_fields": {"employee_range": "Gemini omitted employee_count"},
            "blocking_fields": {"employee_range": "Gemini omitted employee_count"},
        }
        return "incomplete_result"

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)

    pairs, skipped, failures = asyncio.run(discover._enrich_all(
        FakeConn(), candidates, "US", None,
        checkpoint_save=lambda key, status, payload: checkpoints.update(
            {key: {"status": status, "payload": payload}}
        ) is None,
        run_id="run-mixed",
    ))

    assert set(calls) == {"complete.example", "partial.example"}
    assert skipped == 0
    assert failures == {}
    assert checkpoints["enrich:complete.example"]["status"] == "done"
    assert checkpoints["enrich:partial.example"]["status"] == "done"
    assert pairs[0][1].research_outcomes["_summary"]["status"] == "complete"
    assert pairs[1][1].research_outcomes["_summary"]["status"] == "partial"
    assert "failure_reason" not in pairs[1][1].research_outcomes["_summary"]


def test_partial_firmographics_are_checkpointed_and_available_on_retry(monkeypatch):
    candidate = CompanyCandidate(
        domain="partial.example", name="Partial", source="fake", discovered_at=datetime.now(),
    )
    checkpoints = {}
    calls = []

    async def enrich(company):
        calls.append(company.domain)
        if len(calls) == 1:
            company.employee_count = ProvenanceValue(
                value=500, source="gemini_grounded_search",
                url="https://partial.example/about", method="estimated", confidence="medium",
            )
            company.research_evidence.append({
                "evidence_key": "valid-evidence", "field": "employee_range",
                "source_url": "https://partial.example/about",
            })
            company.research_outcomes["employee_range"] = {
                "status": "accepted_and_mapped", "criterion": "automatic_company_size",
            }
        else:
            assert company.employee_count is not None
            assert company.employee_count.value == 500
            assert company.research_evidence[0]["evidence_key"] == "valid-evidence"
        return "partial"

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)

    def save(key, status, payload):
        checkpoints[key] = {"status": status, "payload": payload}
        return True

    first = asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", None,
        checkpoint_save=save, run_id="partial-resume",
    ))
    assert first[2] == {}
    checkpoint_company = checkpoints["enrich:partial.example"]["payload"]["company"]
    assert checkpoint_company["employee_count"]["value"] == 500
    assert checkpoint_company["research_evidence"][0]["evidence_key"] == "valid-evidence"
    assert checkpoint_company["research_outcomes"]["_summary"]["status"] == "partial"
    assert checkpoints["enrich:partial.example"]["status"] == "done"

    second = asyncio.run(discover._enrich_all(
        FakeConn(), [candidate], "GA", None,
        checkpoint_load=lambda key: checkpoints.get(key),
        checkpoint_save=save, run_id="partial-resume",
    ))
    assert calls == ["partial.example"]
    assert second[0][0][1].employee_count.value == 500
    assert second[0][0][1].research_evidence[0]["evidence_key"] == "valid-evidence"
    assert second[0][0][1].research_outcomes["_summary"]["status"] == "partial"


def test_resume_selection_uses_latest_failed_checkpoint_independent_of_ats():
    from csg import db

    jobs = [
        {"job_id": "resume-job", "status": "done", "created_at": "2026-01-02"},
        {"job_id": "original-job", "status": "done", "created_at": "2026-01-01"},
    ]
    checkpoints = {
        "resume-job": [
            {"checkpoint_key": "enrich:completed.example", "status": "done", "payload": {}},
        ],
        "original-job": [
            {"checkpoint_key": "enrich:failed-with-ats.example", "status": "done",
             "payload": {"reason": "quota_exhausted"}},
            {"checkpoint_key": "enrich:completed.example", "status": "failed",
             "payload": {"reason": "older failure"}},
            {"checkpoint_key": "enrich:failed.example", "status": "failed",
             "payload": {"reason": "request_error"}},
        ],
    }

    class CheckpointConn:
        def select(self, table, **params):
            if table == "agent_jobs":
                return jobs
            if table == "agent_job_checkpoints":
                return checkpoints[params["job_id"][3:]]
            raise AssertionError(f"unexpected table: {table}")

    assert db.stalled_companies(CheckpointConn(), "run-1") == [
        "failed-with-ats.example", "failed.example",
    ]


def test_historical_partial_checkpoint_is_not_stalled_or_repeated(monkeypatch):
    from csg import db
    from csg.discover import _needs_enrichment

    saved = Company(domain="partial.example", name="Partial", region="US")
    saved.employee_count = ProvenanceValue(
        value=500, source="gemini_grounded_search", url="https://partial.example/about",
        method="reported", confidence="high",
    )
    saved.research_outcomes = {
        field: {"status": "accepted_and_mapped", "criterion": criterion}
        for field, criterion in research._FIELD_CRITERIA.items()
    }
    saved.research_outcomes["funding"] = {
        "status": "searched_but_not_found", "reason": "No reliable information was returned",
    }
    saved.research_outcomes["_summary"] = {
        "status": "failed",
        "failure_reason": "incomplete_result",
        "missing_fields": {"funding": "No reliable information was returned"},
        "blocking_fields": {},
    }
    assert not _needs_enrichment(saved)

    jobs = [{"job_id": "original-job", "status": "done", "created_at": "2026-01-01"}]
    checkpoints = {
        "original-job": [
            {"checkpoint_key": "enrich:partial.example", "status": "failed",
             "payload": {"reason": "incomplete_result", "company": saved.model_dump(mode="json")}},
            {"checkpoint_key": "enrich:quota.example", "status": "failed",
             "payload": {"reason": "quota_exhausted"}},
        ],
    }

    class CheckpointConn:
        def select(self, table, **params):
            if table == "agent_jobs":
                return jobs
            if table == "agent_job_checkpoints":
                return checkpoints[params["job_id"][3:]]
            raise AssertionError(table)

    assert db.stalled_companies(CheckpointConn(), "run-1") == ["quota.example"]

    calls = []

    async def enrich(company, **_kwargs):
        calls.append(company.domain)
        return "quota_exhausted"

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)
    pairs, skipped, failures = asyncio.run(discover._enrich_all(
        FakeConn(),
        [CompanyCandidate(domain="partial.example", name="Partial", source="fake", discovered_at=datetime.now())],
        "US", None,
        checkpoint_load=lambda key: {
            "enrich:partial.example": checkpoints["original-job"][0],
        }.get(key),
    ))

    assert calls == []
    assert skipped == 1
    assert failures == {}
    summary = pairs[0][1].research_outcomes["_summary"]
    assert summary["status"] == "complete_with_gaps"
    assert pairs[0][1].employee_count.value == 500
    assert "failure_reason" not in summary


def test_resume_stalled_retries_failed_only_and_preserves_success(monkeypatch):
    failed_company = Company(domain="failed.example", name="Failed", region="US")
    successful_company = Company(domain="success.example", name="Success", region="US")
    successful_company.research_outcomes = {
        field: {"status": "accepted_and_mapped", "criterion": criterion}
        for field, criterion in research._FIELD_CRITERIA.items()
    }
    successful_company.research_outcomes["_summary"] = {
        "status": "complete", "missing_fields": {}, "blocking_fields": {},
    }
    checkpoints = {
        "enrich:failed.example": {
            "status": "failed",
            "payload": {
                "company": failed_company.model_dump(mode="json"),
                "reason": "quota_exhausted",
            },
        },
        "enrich:success.example": {
            "status": "done",
            "payload": {"company": successful_company.model_dump(mode="json")},
        },
    }
    provider_calls = []
    saved = {}

    class ResumeConn:
        def commit(self):
            pass

    async def enrich(company, **_kwargs):
        provider_calls.append(company.domain)
        company.research_outcomes = {
            field: {"status": "accepted_and_mapped", "criterion": criterion}
            for field, criterion in research._FIELD_CRITERIA.items()
        }
        company.research_outcomes["_summary"] = {
            "status": "complete", "missing_fields": {}, "blocking_fields": {},
        }
        return None

    async def run_pipeline(*_args, **_kwargs):
        return discover.pipeline.Stats()

    monkeypatch.setattr(discover.db, "get_company", lambda _conn, domain: {
        "failed.example": failed_company,
        "success.example": successful_company,
    }[domain])
    monkeypatch.setattr(discover.db, "upsert_company", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)
    monkeypatch.setattr(discover.pipeline, "run", run_pipeline)
    monkeypatch.setattr(discover.export, "to_csv", lambda *_args, **_kwargs: "csv")
    monkeypatch.setattr(discover.export, "to_jsonl", lambda *_args, **_kwargs: "jsonl")

    stats = asyncio.run(discover.resume_stalled(
        object(), "run-1", ["failed.example", "success.example"],
        conn=ResumeConn(),
        checkpoint_load=lambda key: checkpoints.get(key),
        checkpoint_save=lambda key, status, payload: saved.update(
            {key: {"status": status, "payload": payload}}
        ) is None,
    ))

    assert provider_calls == ["failed.example"]
    assert saved["enrich:failed.example"]["status"] == "done"
    assert "enrich:success.example" not in saved
    assert stats.enriched == 1


def test_resume_stalled_keeps_gemini_quota_failure_queued(monkeypatch):
    company = Company(domain="quota.example", name="Quota", region="US")
    checkpoints = {
        "enrich:quota.example": {
            "status": "failed",
            "payload": {"company": company.model_dump(mode="json"), "reason": "quota_exhausted"},
        },
    }
    saved = {}

    class ResumeConn:
        pass

    monkeypatch.setattr(discover.db, "get_company", lambda _conn, _domain: company)

    async def blocked(company, **_kwargs):
        raise discover.GeminiQuotaBlocked("monthly_quota", retry_after=60)

    monkeypatch.setattr(discover, "enrich_company_firmographics", blocked)
    with pytest.raises(discover.GeminiQuotaBlocked):
        asyncio.run(discover.resume_stalled(
            object(), "run-quota", [company.domain],
            conn=ResumeConn(),
            checkpoint_load=lambda key: checkpoints.get(key),
            checkpoint_save=lambda key, status, payload: saved.update(
                {key: {"status": status, "payload": payload}}
            ) is None,
        ))

    queued = saved["enrich:quota.example"]
    assert queued["status"] == "queued"
    assert queued["payload"]["provider_error"]["error_kind"] == "monthly_quota"
    assert queued["payload"]["retry_after_seconds"] == 60


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
            company.research_outcomes = {
                field: {"status": "accepted_and_mapped", "criterion": criterion}
                for field, criterion in research._FIELD_CRITERIA.items()
            }
            company.research_outcomes["_summary"] = {
                "status": "complete", "missing_fields": {}, "blocking_fields": {},
            }

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

    def mark_qualified(self, domain: str, run_id: str) -> None:
        self.conn.upsert("qualifications", [{"domain": domain, "run_id": run_id,
                                             "qualified_at": "2026-10-07T00:00:00"}],
                         on_conflict="domain,run_id")

    def run(self, run_id: str, region: str = "GA"):
        return asyncio.run(discover.run_discovery(self.cfg, region=region, conn=self.conn, run_id=run_id))

    @property
    def companies(self):
        return self.conn.tables.get("companies", [])


@pytest.fixture
def env(monkeypatch):
    return _Env(monkeypatch, FakeConn())


def test_discovery_scores_and_persists_company_with_optional_research_gaps(env, monkeypatch):
    env.found = [("partial.example", "Partial Example")]

    async def enrich(company):
        company.employee_count = ProvenanceValue(
            value=500, source="gemini_grounded_search",
            url="https://partial.example/about", method="reported", confidence="high",
        )
        company.research_outcomes = {
            field: {
                "status": "searched_but_not_found",
                "criterion": criterion,
                "reason": "No reliable information was returned",
            }
            for field, criterion in research._FIELD_CRITERIA.items()
        }
        company.research_outcomes["employee_range"] = {
            "status": "accepted_and_mapped",
            "criterion": research._FIELD_CRITERIA["employee_range"],
            "evidence_keys": ["employee-evidence"],
        }
        return None

    monkeypatch.setattr(discover, "enrich_company_firmographics", enrich)

    stats = env.run("incomplete-facts-run")
    saved = next(row for row in env.companies if row["domain"] == "partial.example")
    summary = stats.enrichment_summary["partial.example"]

    assert env.pipeline_domains == [["partial.example"]]
    assert stats.enrich_failures == {}
    assert summary["status"] == "complete_with_gaps"
    assert stats.enrich_complete_with_gaps == 1
    assert saved["employee_count"]["value"] == 500
    assert saved["research_outcomes"]["funding"]["status"] == "searched_but_not_found"
    assert saved["research_outcomes"]["_summary"]["status"] == "complete_with_gaps"


def test_same_company_in_two_runs_is_not_duplicated(env):
    env.found = [("acme.com", "Acme Staffing")]
    a = env.run("disc-A", region="GA")
    assert a.duplicates_existing == 0 and len(env.companies) == 1
    env.companies[0]["status"] = "scored"
    env.mark_qualified("acme.com", "disc-A")

    env.found = [("acme.com", "Acme Staffing")]
    b = env.run("disc-B", region="TX")

    assert len(env.companies) == 1
    assert env.companies[0]["region"] == "GA" and env.companies[0]["status"] == "scored"  # not reset
    assert b.duplicates_existing == 1
    assert b.existing_matches == [{"domain": "acme.com", "matched_by": "domain"}]
    assert b.as_dict()["company_outcomes"] == [{
        "domain": "acme.com",
        "outcome": "duplicate",
        "reason": "qualified_in_prior_run",
        "matched_by": "domain",
    }]
    assert [s["run_id"] for s in env.conn.tables["scores"]] == ["disc-A"]
    assert env.enriched.count("acme.com") == 1


def test_incomplete_firmographic_provenance_is_not_skipped():
    from datetime import datetime, timedelta, timezone

    from csg.discover import _needs_enrichment
    from csg.models import Company, ProvenanceValue

    recent = Company(
        domain="recent.example",
        employee_count=ProvenanceValue(
            value=100, source="search", fetched_at=datetime.now(timezone.utc),
            method="estimated", confidence="medium",
        ),
    )
    stale = Company(
        domain="stale.example",
        employee_count=ProvenanceValue(
            value=100, source="search", fetched_at=datetime.now(timezone.utc) - timedelta(days=31),
            method="estimated", confidence="medium",
        ),
    )
    complete = Company(
        domain="complete.example",
        employee_count=ProvenanceValue(
            value=100, source="profile", url="https://complete.example/about",
            fetched_at=datetime.now(timezone.utc) - timedelta(days=365),
            method="estimated", confidence="medium",
        ),
    )

    assert _needs_enrichment(recent)
    assert _needs_enrichment(stale)
    complete.research_outcomes["_summary"] = {
        "status": "complete", "missing_fields": {}, "blocking_fields": {},
    }
    assert not _needs_enrichment(complete)


def test_url_and_name_formatting_variants_match_the_existing_company(env):
    env.found = [("acme.com", "Acme Staffing, Inc.")]
    env.run("disc-A")
    env.mark_qualified("acme.com", "disc-A")
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
    env.mark_qualified("acme.com", "disc-A")

    env.found = [("www.Acme.com", "Acme Staffing")]
    b = env.run("disc-B")

    assert b.duplicates_existing == 1
    assert len(env.campaigns) == 1                      # still only run A's campaign
    assert "disc-B" not in env.import_calls             # nothing handed to Outreach for B
    assert b.outreach is None


def test_matches_are_listed_for_the_run_but_never_keep_a_company_alive(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    env.mark_qualified("acme.com", "disc-A")
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


# --- already discovered != already qualified -----------------------------------

def test_discovered_but_unqualified_company_is_requalified_in_later_run(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")                       # scored but never qualified (missed the top-N)

    env.found = [("www.Acme.com", "Acme Staffing")]
    b = env.run("disc-B", region="TX")

    assert b.duplicates_existing == 0 and b.reeligible == 1
    assert len(env.companies) == 1                        # same row, not a second company
    assert env.companies[0]["region"] == "GA"             # stored region kept
    assert env.pipeline_domains[-1] == ["acme.com"]       # scored again in run B
    assert sorted(s["run_id"] for s in env.conn.tables["scores"]) == ["disc-A", "disc-B"]
    assert env.import_calls == ["disc-A", "disc-B"] and len(env.campaigns) == 2
    assert env.conn.tables.get("run_company_matches", []) == []


def test_name_match_of_unqualified_company_reuses_stored_domain(env):
    env.found = [("acme.com", "Acme Staffing, Inc.")]
    env.run("disc-A")
    env.found = [("acme-jobs.io", "ACME STAFFING INC")]
    b = env.run("disc-B")

    assert b.reeligible == 1
    assert [c["domain"] for c in env.companies] == ["acme.com"]   # no second company
    assert env.pipeline_domains[-1] == ["acme.com"]


def test_company_with_only_failed_qualification_is_still_eligible(env):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    env.conn.upsert("qualification_attempts", [{"domain": "acme.com", "run_id": "disc-A", "status": "failed",
                                                "attempt_count": 3}], on_conflict="domain,run_id")
    env.found = [("acme.com", "Acme Staffing")]
    b = env.run("disc-B")
    assert b.reeligible == 1 and env.pipeline_domains[-1] == ["acme.com"]


@pytest.mark.parametrize("status", ["suppressed", "disqualified"])
def test_terminal_status_company_stays_skipped(env, status):
    env.found = [("acme.com", "Acme Staffing")]
    env.run("disc-A")
    env.companies[0]["status"] = status
    env.found = [("acme.com", "Acme Staffing")]
    b = env.run("disc-B")
    assert b.duplicates_existing == 1 and b.reeligible == 0
    assert env.pipeline_domains == [["acme.com"]]  # only run A's
