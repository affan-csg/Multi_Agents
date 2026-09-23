"""Supabase (Postgres via PostgREST) persistence. Repository functions only --
no business logic. Nothing else in the codebase touches SQL/REST directly;
callers only ever import this module's functions.

Originally SQLite, migrated to Supabase so the data is reachable from a
browser/phone/other machine instead of being a single local file. schema.sql
is the schema actually running on the Supabase project this connects to --
run it once in the Supabase SQL Editor before pointing the app at a new
project.

`connect()` returns a `SupabaseConn` -- a thin httpx wrapper, not a real
DB connection. `commit()`/`close()` are no-ops kept only so callers written
against the old sqlite3.Connection interface (pipeline.py, qualify.py, ...)
did not need to change: PostgREST has no client-side transaction or
connection-lifetime concept, every request is already atomic and stateless.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx
from dotenv import load_dotenv

from .config import PROJECT_ROOT
from .models import Company, JobPosting, ScoreResult

load_dotenv(PROJECT_ROOT / ".env")  # config.py also does this; safe/idempotent, and this
                                     # module must not depend on import order to see .env


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


class SupabaseError(RuntimeError):
    pass


class SupabaseConn:
    """Thin PostgREST client. One instance per `connect()` call; safe to
    reuse across the lifetime of a CLI command the way the old
    sqlite3.Connection was."""

    def __init__(self, url: str, service_key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self._client = httpx.Client(
            base_url=self.base,
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def commit(self) -> None:
        pass  # every PostgREST request is already committed; kept for interface compatibility

    def close(self) -> None:
        self._client.close()

    # -- low-level helpers --------------------------------------------------
    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise SupabaseError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text}")
        return r

    def select(self, table: str, **params: Any) -> list[dict[str, Any]]:
        r = self._check(self._client.get(f"/{table}", params=params))
        return r.json()

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> list[dict[str, Any]]:
        if not rows:
            return []
        r = self._check(self._client.post(
            f"/{table}",
            params={"on_conflict": on_conflict},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=rows,
        ))
        return r.json()

    def update(self, table: str, data: dict[str, Any], **filters: Any) -> None:
        self._check(self._client.patch(f"/{table}", params=filters, json=data))

    def delete(self, table: str, **filters: Any) -> None:
        self._check(self._client.delete(f"/{table}", params=filters))


def connect(url: Optional[str] = None, service_key: Optional[str] = None) -> SupabaseConn:
    # RESEARCH_-scoped vars win: the single-port server (Server/app.py) runs
    # this agent and the Outreach Agent in one process, where the unscoped
    # SUPABASE_* names would collide with Outreach's separate project.
    # Standalone use never sets them, so it falls through unchanged.
    url = url or os.environ.get("RESEARCH_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    service_key = (
        service_key or os.environ.get("RESEARCH_SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    )
    if not url or not service_key:
        raise SupabaseError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env (see .env.example)."
        )
    return SupabaseConn(url, service_key)


# --- runs ------------------------------------------------------------------

def start_run(conn: SupabaseConn, run_id: str, profile: str, seed_count: int, label: Optional[str] = None) -> None:
    """Create or re-open a run row. On an existing run_id (resume), the
    original `started_at` and `label` are preserved rather than clobbered --
    a resume continues the same run, it does not start a new one."""
    existing = conn.select("runs", run_id=f"eq.{run_id}", limit=1)
    started_at = existing[0]["started_at"] if existing else _iso(utcnow())
    if existing and label is None:
        label = existing[0].get("label")
    conn.upsert("runs", [{
        "run_id": run_id, "started_at": started_at, "profile": profile, "seed_count": seed_count,
        "label": label, "finished_at": None,
    }], on_conflict="run_id")


def finish_run(conn: SupabaseConn, run_id: str, notes: str = "") -> None:
    conn.update("runs", {"finished_at": _iso(utcnow()), "notes": notes}, run_id=f"eq.{run_id}")


def rename_run(conn: SupabaseConn, run_id: str, label: str) -> None:
    conn.update("runs", {"label": label}, run_id=f"eq.{run_id}")


def delete_run(conn: SupabaseConn, run_id: str) -> int:
    """Delete a run and fully tear down everything that belongs to it alone.

    `scores`/`qualifications` for this run_id go first (plain columns, not
    FKs into runs -- Postgres would not clean these up on its own). Then,
    for every domain this run scored: if no OTHER run's `scores` row still
    references that domain, the domain was exclusively this run's, so its
    `companies` row is deleted too -- which cascades (ON DELETE CASCADE, see
    schema.sql) to job_postings, discovery_sources, contacts,
    saved_companies, suppression, and any leftover qualifications for it.

    A domain scored in more than one run is left completely alone: deleting
    Run A must never remove data another surviving run still shows on its
    Companies tab. This is the one thing that keeps this safe to call
    without also breaking whatever other run happens to share a company.

    Returns the number of companies (and everything under them) fully
    removed.
    """
    scored = conn.select("scores", run_id=f"eq.{run_id}", select="domain")
    domains = sorted({r["domain"] for r in scored})

    conn.delete("scores", run_id=f"eq.{run_id}")
    conn.delete("qualifications", run_id=f"eq.{run_id}")
    conn.delete("runs", run_id=f"eq.{run_id}")

    removed = 0
    for domain in domains:
        still_referenced = conn.select("scores", domain=f"eq.{domain}", select="run_id", limit=1)
        if still_referenced:
            continue
        conn.delete("companies", domain=f"eq.{domain}")
        removed += 1
    return removed


# --- companies ---------------------------------------------------------------

_COALESCE_FIELDS = (
    "name", "employee_count", "revenue", "funding", "description", "region",
    "industry", "hq_state",
    "ats_provider", "ats_token", "ats_detected_by", "careers_url",
    "job_discovery_method", "job_source", "hiring_signal", "job_discovery_notes",
)

# Researched facts stored as JSONB provenance envelopes (value + source +
# confidence), not bare scalars -- see models.ProvenanceValue.
_PROVENANCE_FIELDS = ("employee_count", "revenue", "funding")


def upsert_company(conn: SupabaseConn, c: Company) -> None:
    """Insert or update, preserving existing values for _COALESCE_FIELDS when
    the incoming value is None -- mirrors the SQLite version's
    `COALESCE(excluded.x, companies.x)` ON CONFLICT clause, which PostgREST's
    merge-duplicates (a full-row overwrite) does not do on its own."""
    now = _iso(utcnow())
    existing = get_company(conn, c.domain)
    existing_first_seen = None
    if existing is not None:
        prior = conn.select("companies", domain=f"eq.{c.domain}", select="first_seen", limit=1)
        existing_first_seen = prior[0]["first_seen"] if prior else None

    row: dict[str, Any] = {
        "domain": c.domain,
        "last_checked": now,
        "status": c.status,
        "dq_reason": c.dq_reason,
        "first_seen": existing_first_seen or now,  # preserved across updates; set once on insert
    }
    # mode="json" so ProvenanceValue envelopes (and their datetimes) serialize
    # to JSONB-safe primitives rather than Python objects.
    incoming = c.model_dump(mode="json")
    for field in _COALESCE_FIELDS:
        value = incoming.get(field)
        if value is None and existing is not None:
            prior_value = getattr(existing, field)
            if field in _PROVENANCE_FIELDS and prior_value is not None:
                prior_value = prior_value.model_dump(mode="json")
            value = prior_value
        row[field] = value

    conn.upsert("companies", [row], on_conflict="domain")


def get_company(conn: SupabaseConn, domain: str) -> Optional[Company]:
    rows = conn.select("companies", domain=f"eq.{domain}", limit=1)
    return _row_to_company(rows[0]) if rows else None


def all_companies(conn: SupabaseConn) -> list[Company]:
    rows = conn.select("companies", order="domain.asc")
    return [_row_to_company(r) for r in rows]


def _row_to_company(row: dict[str, Any]) -> Company:
    return Company(
        domain=row["domain"], name=row.get("name"), employee_count=row.get("employee_count"),
        revenue=row.get("revenue"), funding=row.get("funding"),
        description=row.get("description"), region=row.get("region"),
        industry=row.get("industry"), hq_state=row.get("hq_state"),
        ats_provider=row.get("ats_provider"), ats_token=row.get("ats_token"),
        ats_detected_by=row.get("ats_detected_by"), careers_url=row.get("careers_url"),
        status=row.get("status", "discovered"), dq_reason=row.get("dq_reason"),
        job_discovery_method=row.get("job_discovery_method"), job_source=row.get("job_source"),
        hiring_signal=row.get("hiring_signal"), job_discovery_notes=row.get("job_discovery_notes"),
    )


def set_status(conn: SupabaseConn, domain: str, status: str, dq_reason: Optional[str] = None) -> None:
    conn.update("companies", {"status": status, "dq_reason": dq_reason, "last_checked": _iso(utcnow())},
                domain=f"eq.{domain}")


def save_discovery_sources(conn: SupabaseConn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn.upsert("discovery_sources", rows, on_conflict="domain,provider_name")
    return len(rows)


# --- job postings ------------------------------------------------------------

def upsert_postings(conn: SupabaseConn, postings: Iterable[JobPosting]) -> int:
    """Insert new reqs, refresh last_seen on known ones.

    first_seen is preserved on conflict -- that is what makes run 2+ able to
    compute req age for providers that omit a posting date, and (later) repost
    and velocity signals. posted_at is also preserved when a later fetch omits
    it (some providers drop the field intermittently). Because PostgREST's
    merge-duplicates overwrites the whole row, both are fetched once up front
    (a single batch query for this domain) and carried forward explicitly --
    mirrors the SQLite version's `COALESCE(excluded.x, table.x)` ON CONFLICT
    clause, which PostgREST does not provide on its own.
    """
    postings = list(postings)
    if not postings:
        return 0

    now = _iso(utcnow())
    domain = postings[0].domain
    existing = {
        (r["domain"], r["ats_provider"], r["external_id"]): r
        for r in conn.select("job_postings", domain=f"eq.{domain}",
                              select="domain,ats_provider,external_id,first_seen,posted_at")
    }

    rows = []
    for p in postings:
        # job_postings.ats_provider is NOT NULL (it's part of the primary
        # key) -- a posting found via careers_page/web_search has no real
        # AtsProvider, so the row stores its `source` value there instead.
        # `source` is the field everything (scoring, UI, provenance) should
        # actually read; this is purely to satisfy the DB's key constraint.
        db_ats_provider = p.ats_provider or p.source
        prior = existing.get((p.domain, db_ats_provider, p.external_id))
        rows.append({
            "domain": p.domain, "ats_provider": db_ats_provider, "external_id": p.external_id,
            "title": p.title, "location": p.location, "department": p.department,
            "employment_type": p.employment_type, "url": p.url,
            "posted_at": _iso(p.posted_at) or (prior["posted_at"] if prior else None),
            "first_seen": prior["first_seen"] if prior else now, "last_seen": now,
            "description": p.description, "raw_json": p.raw,
            "delivery_fit": p.delivery_fit, "practice_area": p.practice_area,
            "source": p.source,
        })

    conn.upsert("job_postings", rows, on_conflict="domain,ats_provider,external_id")
    return len(rows)


def _row_to_posting(r: dict[str, Any]) -> JobPosting:
    source = r.get("source") or "ats"
    # The DB's ats_provider column holds a source sentinel ("careers_page" /
    # "web_search") for non-ATS postings (see upsert_postings) -- translate
    # that back to a real AtsProvider|None on the model.
    ats_provider = r["ats_provider"] if source == "ats" else None
    return JobPosting(
        domain=r["domain"], ats_provider=ats_provider, source=source, external_id=r["external_id"],
        title=r.get("title") or "", location=r.get("location"), department=r.get("department"),
        employment_type=r.get("employment_type"), url=r.get("url"),
        posted_at=_parse(r.get("posted_at")), first_seen=_parse(r.get("first_seen")),
        description=r.get("description") or "",
        delivery_fit=r.get("delivery_fit"), practice_area=r.get("practice_area"),
    )


def get_postings(conn: SupabaseConn, domain: str) -> list[JobPosting]:
    rows = conn.select("job_postings", domain=f"eq.{domain}")
    return [_row_to_posting(r) for r in rows]


def postings_by_domain(conn: SupabaseConn, domains: list[str]) -> dict[str, list[JobPosting]]:
    """Batch form of get_postings() -- one request for every domain instead
    of one request per domain, same pattern as contacts_by_domain()."""
    if not domains:
        return {}
    rows = conn.select("job_postings", domain=f"in.({','.join(domains)})")
    out: dict[str, list[JobPosting]] = {}
    for r in rows:
        out.setdefault(r["domain"], []).append(_row_to_posting(r))
    return out


def companies_by_domain(conn: SupabaseConn, domains: list[str]) -> dict[str, Company]:
    """Batch form of get_company() -- one request for every domain instead
    of one request per domain."""
    if not domains:
        return {}
    rows = conn.select("companies", domain=f"in.({','.join(domains)})")
    return {r["domain"]: _row_to_company(r) for r in rows}


# --- scores --------------------------------------------------------------

def save_score(conn: SupabaseConn, run_id: str, res: ScoreResult) -> None:
    conn.upsert("scores", [{
        "domain": res.domain, "run_id": run_id, "score": res.score, "tier": res.tier,
        "dimensions": res.dimensions, "explanation": [str(r) for r in res.reasons],
        "metrics": _jsonable(res.metrics), "scored_at": _iso(utcnow()),
    }], on_conflict="domain,run_id")


def _jsonable(obj: Any) -> Any:
    """metrics can contain e.g. a set() (scoring.py's `states`) -- JSONB needs
    plain JSON-serializable values, unlike sqlite3 which stored it as an
    opaque TEXT blob via json.dumps(default=str)."""
    import json
    return json.loads(json.dumps(obj, default=str))


# Company columns joined onto score rows for the report/analytics views.
_SCORE_JOIN_SELECT = ("domain,name,employee_count,revenue,funding,region,"
                      "industry,hq_state,description,ats_provider,ats_token,"
                      "job_discovery_method,job_source,hiring_signal,job_discovery_notes")
_SCORE_JOIN_FIELDS = ("name", "employee_count", "revenue", "funding", "region",
                      "industry", "hq_state", "description",
                      "ats_provider", "ats_token",
                      "job_discovery_method", "job_source", "hiring_signal", "job_discovery_notes")


def _join_companies(conn: SupabaseConn, scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach company firmographics to score rows. Shared by get_scores() and
    all_scores() so the two views never drift apart."""
    domains = sorted({s["domain"] for s in scores})
    companies = {
        c["domain"]: c
        for c in conn.select("companies", domain=f"in.({','.join(domains)})",
                              select=_SCORE_JOIN_SELECT)
    }
    out = []
    for s in scores:
        c = companies.get(s["domain"], {})
        out.append({**s, **{f: c.get(f) for f in _SCORE_JOIN_FIELDS}})
    return out


def get_scores(conn: SupabaseConn, run_id: str) -> list[dict[str, Any]]:
    scores = conn.select("scores", run_id=f"eq.{run_id}", order="score.desc")
    if not scores:
        return []
    return _join_companies(conn, scores)


def all_scores(conn: SupabaseConn) -> list[dict[str, Any]]:
    """Every score row across every run, enriched the same way get_scores()
    enriches a single run -- used for the "all runs" analytics view. One
    domain can appear more than once here (once per run it was scored in)."""
    scores = conn.select("scores", order="score.desc")
    if not scores:
        return []
    return _join_companies(conn, scores)


def latest_scores_per_domain(conn: SupabaseConn) -> list[dict[str, Any]]:
    """all_scores(), collapsed to one row per domain -- whichever run scored
    it most recently -- so the "All research" Companies view shows each
    company once instead of once per run it appeared in."""
    best: dict[str, dict[str, Any]] = {}
    for s in all_scores(conn):
        current = best.get(s["domain"])
        if current is None or s["scored_at"] > current["scored_at"]:
            best[s["domain"]] = s
    return sorted(best.values(), key=lambda r: -r["score"])


def latest_run_id(conn: SupabaseConn) -> Optional[str]:
    rows = conn.select("runs", order="started_at.desc", limit=1)
    return rows[0]["run_id"] if rows else None


def stalled_companies(conn: SupabaseConn, run_id: str) -> list[str]:
    """Domains this run scored that never got real data: no ATS board found
    AND no firmographics either -- the signature of every provider (Gemini,
    the Tavily+Groq discovery/enrichment fallback) being exhausted or
    unreachable for that company, not a company that was genuinely researched
    and just turned out thin. These are what a "resume" re-run should retry;
    a company that was actually checked and came up empty should not be
    re-fetched every time someone clicks resume.
    """
    scored = conn.select("scores", run_id=f"eq.{run_id}", select="domain,score")
    if not scored:
        return []
    zero_domains = sorted({r["domain"] for r in scored if not r.get("score")})
    if not zero_domains:
        return []
    stalled = []
    for i in range(0, len(zero_domains), 50):
        batch = zero_domains[i:i + 50]
        in_list = ",".join(batch)
        rows = conn.select(
            "companies", domain=f"in.({in_list})",
            select="domain,ats_provider,employee_count,revenue",
        )
        for r in rows:
            if not r.get("ats_provider") and not r.get("employee_count") and not r.get("revenue"):
                stalled.append(r["domain"])
    return sorted(stalled)




# --- qualifications (step 4, LLM) -----------------------------------------

def save_qualification(
    conn: SupabaseConn, run_id: str, domain: str,
    pain_summary: str, recommended_angle: str, model: str,
) -> None:
    conn.upsert("qualifications", [{
        "domain": domain, "run_id": run_id, "pain_summary": pain_summary,
        "recommended_angle": recommended_angle, "model": model, "qualified_at": _iso(utcnow()),
    }], on_conflict="domain,run_id")


def get_qualification(conn: SupabaseConn, domain: str, run_id: str) -> Optional[dict[str, Any]]:
    rows = conn.select("qualifications", domain=f"eq.{domain}", run_id=f"eq.{run_id}", limit=1)
    return rows[0] if rows else None


def get_qualifications(conn: SupabaseConn, run_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.select("qualifications", run_id=f"eq.{run_id}")
    return {r["domain"]: r for r in rows}


def save_qualification_attempt(
    conn: SupabaseConn, run_id: str, domain: str, status: str,
    error_reason: Optional[str] = None, error_detail: Optional[str] = None,
) -> None:
    """Record one qualification attempt (success or failure). `status` is
    'succeeded' or 'failed'. Upserts on (domain, run_id) and bumps
    attempt_count by reading the current row first -- attempts are rare
    enough (bounded by MAX_RETRIES worth of backoff, one call per qualify_run
    pass) that the extra read is not worth a raw SQL increment here."""
    existing = get_qualification_attempt(conn, domain, run_id)
    attempt_count = (existing["attempt_count"] + 1) if existing else 1
    conn.upsert("qualification_attempts", [{
        "domain": domain, "run_id": run_id, "status": status,
        "error_reason": error_reason, "error_detail": error_detail,
        "attempt_count": attempt_count, "last_attempted_at": _iso(utcnow()),
    }], on_conflict="domain,run_id")


def get_qualification_attempt(conn: SupabaseConn, domain: str, run_id: str) -> Optional[dict[str, Any]]:
    rows = conn.select("qualification_attempts", domain=f"eq.{domain}", run_id=f"eq.{run_id}", limit=1)
    return rows[0] if rows else None


def get_qualification_attempts(conn: SupabaseConn, run_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.select("qualification_attempts", run_id=f"eq.{run_id}")
    return {r["domain"]: r for r in rows}


def get_failed_qualifications(conn: SupabaseConn, run_id: str) -> list[dict[str, Any]]:
    """Domains whose most recent qualification attempt for this run failed
    and have no successful qualification row -- i.e. still need a retry.
    A domain that later succeeds after a prior failure is excluded here
    (save_qualification_attempt('succeeded') overwrites the failed row)."""
    rows = conn.select("qualification_attempts", run_id=f"eq.{run_id}", status="eq.failed")
    return rows


# --- contacts (Hunter.io) --------------------------------------------------

def save_contacts(conn: SupabaseConn, domain: str, contacts: list[dict[str, Any]], source: str) -> int:
    if not contacts:
        return 0
    now = _iso(utcnow())
    rows = [{
        "domain": domain, "email": c["email"], "first_name": c.get("first_name"),
        "last_name": c.get("last_name"), "position": c.get("position"),
        "seniority": c.get("seniority"), "department": c.get("department"),
        "linkedin_url": c.get("linkedin_url"), "confidence": c.get("confidence"),
        "verification": c.get("verification"), "source": source, "found_at": now,
    } for c in contacts]
    conn.upsert("contacts", rows, on_conflict="domain,email")
    return len(rows)


def get_contacts(conn: SupabaseConn, domain: str) -> list[dict[str, Any]]:
    return conn.select("contacts", domain=f"eq.{domain}", order="confidence.desc")


def has_contacts(conn: SupabaseConn, domain: str) -> bool:
    rows = conn.select("contacts", domain=f"eq.{domain}", limit=1, select="domain")
    return bool(rows)


def contacts_by_domain(conn: SupabaseConn, domains: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not domains:
        return {}
    rows = conn.select("contacts", domain=f"in.({','.join(domains)})", order="domain.asc,confidence.desc")
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["domain"], []).append(r)
    return out


def all_contacts(conn: SupabaseConn) -> list[dict[str, Any]]:
    """Every discovered contact, enriched with the company name/industry so
    the Contacts page can render a full table without a second round trip
    per row."""
    rows = conn.select("contacts", order="found_at.desc")
    if not rows:
        return []
    domains = sorted({r["domain"] for r in rows})
    companies = {
        c["domain"]: c
        for c in conn.select("companies", domain=f"in.({','.join(domains)})",
                              select="domain,name,industry,ats_provider")
    }
    out = []
    for r in rows:
        c = companies.get(r["domain"], {})
        out.append({**r, "company_name": c.get("name"), "industry": c.get("industry")})
    return out


# --- suppression ---------------------------------------------------------

def load_suppression(conn: SupabaseConn) -> set[str]:
    return {r["domain"] for r in conn.select("suppression", select="domain")}


def add_suppression(conn: SupabaseConn, domain: str, reason: str) -> None:
    """suppression.domain has an FK into companies, so manually suppressing a
    domain that was never discovered/scored (the common case from Settings) would
    otherwise violate that constraint -- stub a minimal companies row first."""
    if get_company(conn, domain) is None:
        conn.upsert("companies", [{
            "domain": domain, "first_seen": _iso(utcnow()), "status": "suppressed",
        }], on_conflict="domain")
    conn.upsert("suppression", [{
        "domain": domain, "reason": reason, "added_at": _iso(utcnow()),
    }], on_conflict="domain")


def list_suppression(conn: SupabaseConn) -> list[dict[str, Any]]:
    """Every suppressed domain with its reason/date, enriched with the
    company name when known -- what the Settings page renders."""
    rows = conn.select("suppression", order="added_at.desc")
    if not rows:
        return []
    domains = sorted({r["domain"] for r in rows})
    companies = {
        c["domain"]: c["name"]
        for c in conn.select("companies", domain=f"in.({','.join(domains)})", select="domain,name")
    }
    return [{**r, "company_name": companies.get(r["domain"])} for r in rows]


def remove_suppression(conn: SupabaseConn, domain: str) -> None:
    conn.delete("suppression", domain=f"eq.{domain}")


# --- saved companies -----------------------------------------------------

def saved_domains(conn: SupabaseConn) -> set[str]:
    return {r["domain"] for r in conn.select("saved_companies", select="domain")}


def get_saved(conn: SupabaseConn) -> list[dict[str, Any]]:
    return conn.select("saved_companies", order="saved_at.desc")


def add_saved(conn: SupabaseConn, domain: str, run_id: Optional[str], note: Optional[str]) -> None:
    conn.upsert("saved_companies", [{
        "domain": domain, "saved_from_run": run_id, "note": note, "saved_at": _iso(utcnow()),
    }], on_conflict="domain")


def remove_saved(conn: SupabaseConn, domain: str) -> None:
    conn.delete("saved_companies", domain=f"eq.{domain}")


# --- API usage tracking (Settings > Usage tab) ----------------------------

def insert_api_usage(conn: SupabaseConn, rows: list[dict[str, Any]]) -> None:
    """Plain insert, not upsert -- every call is its own row, there is no
    natural conflict key to dedupe on. Called only by usage_tracker.py."""
    if not rows:
        return
    conn._check(conn._client.post("/api_usage", json=rows))


def api_usage_since(conn: SupabaseConn, since_iso: str) -> list[dict[str, Any]]:
    return conn.select("api_usage", created_at=f"gte.{since_iso}", order="created_at.asc", limit=10000)


def api_usage_all(conn: SupabaseConn) -> list[dict[str, Any]]:
    """Same as api_usage_since but with no lower time bound -- the Usage tab's
    "All Time" filter. Kept as its own function (rather than an Optional
    since_iso on api_usage_since) so every existing call site's signature is
    untouched."""
    return conn.select("api_usage", order="created_at.asc", limit=10000)
