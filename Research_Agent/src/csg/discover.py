"""Autonomous discovery orchestration: region in, drafted campaign out.

This is the product's main entrypoint, shared by `csg discover` and the
`POST /discover` API route so the two can never drift apart. The caller
supplies only a geographic region -- every other criterion (size, financials,
practice areas, priority) comes from the embedded ICP in icp.yaml.

    region -> discover -> resolve/dedupe -> enrich -> persist
           -> ATS + jobs -> score -> tier -> export
           -> qualify + Hunter.io contacts for Tier A/B (capped, see below)
           -> hand off to the Outreach Agent: import as prospects,
              draft a personalized campaign (never auto-sent)

Contact discovery and LLM qualification cost real quota (Hunter's free tier is
25 searches/month; qualify spends LLM tokens), so every Tier A/B company is
covered but capped -- see `auto_pipeline`/`auto_qualify_top` on run_discovery
and icp.yaml's `outreach.auto_qualify_top_n`. The API route (what the
Dashboard's "Start research" uses) runs this automatically, no flag needed;
the CLI keeps an explicit `--auto-qualify-top` opt-in with a cost
confirmation, since a person running raw commands may want that conscious
gate before spending real money. The Outreach hand-off itself only fires if
`OUTREACH_API_BASE` is configured, and only sends anything as far as drafting
a campaign -- sending an email is always a separate, later, human action.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from . import contacts as contacts_mod, db, export, pipeline, qualify as qualify_mod
from .config import Config
from .db import SupabaseConn
from .discovery import (DiscoveryError, WebSearchProvider, GroqTavilyProvider,
                        deduplicate_and_resolve, enrich_company_firmographics)
from .http import Fetcher
from .models import Company
from .usage_tracker import classify_error

# Enrichment is one LLM round trip per company; run a bounded number at once so
# a 100-company region does not take 100 sequential calls. Kept modest to stay
# well inside provider rate limits.
ENRICH_CONCURRENCY = 5

ProgressFn = Callable[[str, int, int, str], None]
"""stage, done, total, detail"""


@dataclass
class DiscoveryStats:
    region: str = ""
    run_id: str = ""
    discovered: int = 0
    enriched: int = 0
    enrich_skipped: int = 0      # already had firmographics from a previous run
    pipeline: Optional[pipeline.Stats] = None
    exports: dict[str, str] = field(default_factory=dict)
    qualify: Optional[dict[str, Any]] = None
    contacts: Optional[dict[str, Any]] = None
    auto_qualify_top_n: int = 0
    auto_qualify_skipped_tier: int = 0
    enrich_failures: dict[str, int] = field(default_factory=dict)
    """Count of enrichment failures by reason (see enrich_company_firmographics
    and usage_tracker.classify_error): "no_api_key", "quota_exhausted",
    "rate_limited", "connection_error", "timeout", "parse_error", or the
    catch-all "request_error" -- a systemic problem (e.g. quota exhaustion
    across every key) shows up here as a large count under its real cause
    instead of N silent per-company server-log lines all reading the same
    generic "request failed"."""
    outreach: Optional[dict[str, Any]] = None
    """Result of handing qualified Tier A/B prospects to the Outreach Agent
    (see _handoff_to_outreach): {"imported": N, "campaign_id": ..., "item_count": N}
    on success, {"error": ...} on failure, or None if the hand-off was never
    attempted (no Tier A/B companies qualified, or OUTREACH_API_BASE unset)."""

    def as_dict(self) -> dict[str, Any]:
        p = self.pipeline
        return {
            "region": self.region,
            "run_id": self.run_id,
            "discovered": self.discovered,
            "enriched": self.enriched,
            "enrich_skipped": self.enrich_skipped,
            "enrich_failures": self.enrich_failures,
            "companies": p.companies if p else 0,
            "suppressed": p.suppressed if p else 0,
            "no_ats": p.no_ats if p else 0,
            "ats_found": p.ats_found if p else 0,
            "no_reqs": p.no_reqs if p else 0,
            "reqs_fetched": p.reqs_fetched if p else 0,
            "disqualified": p.disqualified if p else 0,
            "scored": p.scored if p else 0,
            "tiers": p.tiers if p else {},
            "dq_reasons": p.dq_reasons if p else {},
            "detected_by": p.detected_by if p else {},
            "providers": p.providers if p else {},
            # Job discovery fallback usage (see jobs_discovery.py's chain) --
            # counts by which method actually produced each company's job
            # signal, plus how many landed on "unknown" (every method
            # exhausted, NOT a confirmed zero -- see that module's
            # docstring). "no_ats"/"ats_found" above only ever meant "was an
            # ATS board found", never "was any job signal found" -- these two
            # fields are what actually answer that broader question now.
            "job_discovery_methods": p.job_discovery_methods if p else {},
            "hiring_signal_unknown": p.hiring_signal_unknown if p else 0,
            "exports": self.exports,
            "qualify": self.qualify,
            "contacts": self.contacts,
            "auto_qualify_top_n": self.auto_qualify_top_n,
            "auto_qualify_skipped_tier": self.auto_qualify_skipped_tier,
            "outreach": self.outreach,
        }


def _needs_enrichment(existing: Optional[Company]) -> bool:
    """Skip companies we already researched -- enrichment costs an LLM call and
    firmographics do not change run to run."""
    if existing is None:
        return True
    return existing.employee_count is None and existing.revenue is None


async def _enrich_all(
    conn: SupabaseConn, candidates: list, region: str, on_progress: Optional[ProgressFn],
) -> tuple[list[tuple[Any, Company]], int, dict[str, int]]:
    """Build Company rows, enriching only the ones we have not researched yet."""
    pairs: list[tuple[Any, Company]] = []
    todo: list[Company] = []
    skipped = 0
    failures: dict[str, int] = {}

    for c in candidates:
        existing = db.get_company(conn, c.domain)
        company = Company(
            domain=c.domain,
            name=c.name or (existing.name if existing else None),
            region=region,
            industry=c.industry or (existing.industry if existing else None),
            status="discovered",
        )
        if existing is not None and not _needs_enrichment(existing):
            # Carry forward what we already know instead of paying for it again.
            company.employee_count = existing.employee_count
            company.revenue = existing.revenue
            company.funding = existing.funding
            company.description = existing.description
            skipped += 1
        else:
            todo.append(company)
        pairs.append((c, company))

    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    done = 0

    async def one(company: Company) -> None:
        nonlocal done
        async with sem:
            try:
                reason = await enrich_company_firmographics(company)
                if reason:
                    failures[reason] = failures.get(reason, 0) + 1
            except Exception as exc:  # a failed lookup must not kill the run
                reason = classify_error(exc)
                print(f"  [WARN] enrichment failed for {company.domain} ({reason}): "
                      f"{type(exc).__name__}: {exc}")
                failures[reason] = failures.get(reason, 0) + 1
            done += 1
            if on_progress:
                on_progress("enrich", done, len(todo), company.domain)

    if todo:
        await asyncio.gather(*(one(c) for c in todo))
    return pairs, skipped, failures


async def run_discovery(
    cfg: Config,
    region: Optional[str] = None,
    limit: int = 100,
    conn: Optional[SupabaseConn] = None,
    run_id: Optional[str] = None,
    label: Optional[str] = None,
    use_cache: bool = True,
    on_progress: Optional[ProgressFn] = None,
    auto_qualify_top: Optional[int] = None,
    auto_pipeline: bool = True,
) -> DiscoveryStats:
    """Discover, research, score, and export prospects for one region.

    `region` defaults to the active region in the ICP config, so a run needs no
    arguments at all. Raises DiscoveryError if discovery itself cannot run --
    an empty result and a broken provider must not look the same.

    `auto_pipeline` (default True -- this is what makes a normal run, e.g.
    from the Dashboard's "Start research", automatic with no flag): every
    Tier A/B company this run scores (undisqualified, ranked by score, capped
    at icp.yaml's outreach.auto_qualify_top_n) is automatically qualified
    (LLM pain summary), has contacts looked up (Hunter.io), and -- if
    OUTREACH_API_BASE is configured -- is handed off to the Outreach Agent to
    become a cached prospect + a drafted (never auto-sent) campaign. See
    _auto_qualify_and_contact and _handoff_to_outreach.

    `auto_qualify_top`, when given, OVERRIDES auto_pipeline's cap with an
    explicit N instead of the configured default -- this is what the CLI's
    `--auto-qualify-top` flag drives, after its own cost confirmation (see
    cli.py). Passing `auto_pipeline=False` with `auto_qualify_top=None` skips
    qualify/contacts/hand-off entirely (the CLI's default -- a person must
    explicitly opt in there, since it spends real quota with no automatic
    cap-based safety rail beyond what --auto-qualify-top itself asks for).
    """
    region = cfg.resolve_region(region)
    rid = run_id or datetime.now().strftime("disc-%Y%m%d-%H%M%S")
    stats = DiscoveryStats(region=region, run_id=rid)

    owns_conn = conn is None
    conn = conn or db.connect()
    try:
        h = cfg.http

        # --- 1. discover ---
        if on_progress:
            on_progress("discover", 0, 1, region)
        async with Fetcher(
            user_agent=h["user_agent"], per_host_delay=float(h["per_host_delay_seconds"]),
            timeout=float(h["timeout_seconds"]), cache_ttl_hours=float(h["cache_ttl_hours"]),
            max_concurrency=int(h["max_concurrency"]), use_cache=use_cache,
        ) as fetcher:
            provider = WebSearchProvider(fetcher=fetcher, cfg=cfg)
            try:
                candidates = deduplicate_and_resolve(
                    await provider.discover(region=region, limit=limit))
            except DiscoveryError as exc:
                # Gemini's key pool is exhausted/unavailable -- fall back to
                # Tavily search + Groq parsing rather than failing the run,
                # same ICP-driven prompts, just a different backend.
                print(f"  [WARN] Gemini discovery unavailable ({exc}); falling back to Tavily+Groq")
                provider = GroqTavilyProvider(fetcher=fetcher, cfg=cfg)
                candidates = deduplicate_and_resolve(
                    await provider.discover(region=region, limit=limit))
        provider_name = provider.name
        stats.discovered = len(candidates)
        if on_progress:
            on_progress("discover", 1, 1, f"{len(candidates)} companies")

        if not candidates:
            return stats

        # --- 2. enrich (skips companies already researched) ---
        pairs, skipped, enrich_failures = await _enrich_all(conn, candidates, region, on_progress)
        stats.enriched = len(pairs) - skipped - sum(enrich_failures.values())
        stats.enrich_skipped = skipped
        stats.enrich_failures = enrich_failures

        # --- 3. persist ---
        rows = []
        for candidate, company in pairs:
            db.upsert_company(conn, company)
            rows.append({
                "domain": candidate.domain,
                "provider_name": provider_name,
                "url": candidate.source_url,
                "discovered_at": (candidate.discovered_at.isoformat()
                                  if candidate.discovered_at else None),
            })
        try:
            db.save_discovery_sources(conn, rows)
        except db.SupabaseError as exc:
            print(f"  [WARN] discovery_sources save failed: {exc}")
        conn.commit()

        # --- 4. ATS + jobs + scoring ---
        def pipeline_progress(done: int, total: int, domain: str) -> None:
            if on_progress:
                on_progress("pipeline", done, total, domain)

        stats.pipeline = await pipeline.run(
            cfg, [c for _, c in pairs], conn, rid,
            use_cache=use_cache, on_progress=pipeline_progress,
            label=label or f"discover {region}",
        )

        # --- 5. export ---
        stats.exports = {
            "csv": str(export.to_csv(conn, rid)),
            "jsonl": str(export.to_jsonl(conn, rid, tiers=["A", "B", "C"])),
        }

        # --- 6. qualify + contacts + Outreach hand-off for Tier A/B ---
        # `auto_qualify_top=0` is an explicit "skip this" from the caller
        # (e.g. the Dashboard's "Top N" field set to 0) and must be honored
        # as zero, not treated the same as "not provided" (None) -- only
        # None falls through to auto_pipeline's configured default cap.
        top_n = auto_qualify_top if auto_qualify_top is not None else (cfg.auto_qualify_top_n if auto_pipeline else 0)
        if top_n:
            await _auto_qualify_and_contact(conn, rid, top_n, stats, on_progress)
            if stats.qualify or stats.contacts:
                await _handoff_to_outreach(cfg, rid, stats, on_progress)

        return stats
    finally:
        if owns_conn:
            conn.close()


async def resume_stalled(
    cfg: Config,
    run_id: str,
    domains: list[str],
    conn: Optional[SupabaseConn] = None,
    on_progress: Optional[ProgressFn] = None,
) -> DiscoveryStats:
    """Continue a run for the companies that stalled last time -- scored 0
    with no ATS board and no firmographics, the signature of every provider
    (Gemini, and the Tavily+Groq fallback) being exhausted or unreachable for
    them (see db.stalled_companies).

    Unlike run_discovery, this does NOT re-run discovery -- the companies are
    already known, re-searching the region would just be a second, possibly
    different, set of search results. It re-enriches only these domains (the
    same skip-if-already-enriched logic means a company that got firmographics
    via the fallback last time won't be re-fetched) and re-runs them through
    the scoring pipeline under the same run_id, so a fixed/refilled provider
    quota turns a stalled 0-score company into a properly researched one.
    """
    if not domains:
        raise ValueError("resume_stalled called with no domains -- nothing to resume")

    rid = run_id
    stats = DiscoveryStats(region="", run_id=rid)

    owns_conn = conn is None
    conn = conn or db.connect()
    try:
        fetched = [(d, db.get_company(conn, d)) for d in domains]
        missing = [d for d, c in fetched if c is None]
        companies = [c for _, c in fetched if c is not None]
        if missing:
            print(f"  [WARN] resume_stalled: {len(missing)} domain(s) no longer exist, "
                  f"skipping: {', '.join(missing)}")
        if not companies:
            raise DiscoveryError(
                f"none of the {len(domains)} stalled compan{'y' if len(domains) == 1 else 'ies'} "
                f"for run {run_id!r} still exist -- they may have been deleted since this run "
                f"finished. Nothing to resume."
            )
        stats.region = companies[0].region or ""
        stats.discovered = len(companies)

        # --- re-enrich only the stalled ones (skips if already researched) ---
        todo = [c for c in companies if _needs_enrichment(c)]
        sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
        done = 0
        failures: dict[str, int] = {}

        async def one(company: Company) -> None:
            nonlocal done
            async with sem:
                try:
                    reason = await enrich_company_firmographics(company)
                    if reason:
                        failures[reason] = failures.get(reason, 0) + 1
                except Exception as exc:
                    reason = classify_error(exc)
                    print(f"  [WARN] enrichment failed for {company.domain} ({reason}): "
                          f"{type(exc).__name__}: {exc}")
                    failures[reason] = failures.get(reason, 0) + 1
                done += 1
                if on_progress:
                    on_progress("enrich", done, len(todo), company.domain)

        if todo:
            await asyncio.gather(*(one(c) for c in todo))
        stats.enriched = len(companies) - len(todo) - sum(failures.values())
        stats.enrich_skipped = len(companies) - len(todo)
        stats.enrich_failures = failures

        for c in companies:
            db.upsert_company(conn, c)
        conn.commit()

        # --- re-run ATS + jobs + scoring for these domains only ---
        def pipeline_progress(done_: int, total: int, domain: str) -> None:
            if on_progress:
                on_progress("pipeline", done_, total, domain)

        stats.pipeline = await pipeline.run(
            cfg, companies, conn, rid,
            on_progress=pipeline_progress,
        )

        stats.exports = {
            "csv": str(export.to_csv(conn, rid)),
            "jsonl": str(export.to_jsonl(conn, rid, tiers=["A", "B", "C"])),
        }
        return stats
    finally:
        if owns_conn:
            conn.close()


async def _auto_qualify_and_contact(
    conn: SupabaseConn, run_id: str, top_n: int, stats: DiscoveryStats,
    on_progress: Optional[ProgressFn],
) -> None:
    """Qualify + find contacts for the top `top_n` scored companies that
    landed in Tier A/B -- see run_discovery's docstring. Failures here are
    recorded on `stats`, not raised: the discovery/scoring run itself already
    succeeded and must not be reported as failed just because the optional
    qualify/contacts add-on hit a missing API key or quota limit.
    """
    ranked = [r for r in db.get_scores(conn, run_id) if not r.get("disqualified")][:top_n]
    domains = [r["domain"] for r in ranked if r["tier"] in ("A", "B")]
    stats.auto_qualify_top_n = len(ranked)
    stats.auto_qualify_skipped_tier = len(ranked) - len(domains)
    if not domains:
        return

    def qual_progress(done: int, total: int, domain: str) -> None:
        if on_progress:
            on_progress("qualify", done, total, domain)

    try:
        stats.qualify = await qualify_mod.qualify_run(conn, run_id, domains=domains, on_progress=qual_progress)
    except qualify_mod.QualificationError as exc:
        stats.qualify = {"error": str(exc)}

    def contact_progress(done: int, total: int, domain: str) -> None:
        if on_progress:
            on_progress("contacts", done, total, domain)

    try:
        stats.contacts = await contacts_mod.discover_run(conn, domains, on_progress=contact_progress)
    except contacts_mod.ContactDiscoveryError as exc:
        stats.contacts = {"error": str(exc)}

    # Re-export so the CSV/JSONL handoff includes the qualification + contact
    # fields just written -- otherwise Outreach's import would only see the
    # pre-qualify snapshot from step 5.
    stats.exports = {
        "csv": str(export.to_csv(conn, run_id)),
        "jsonl": str(export.to_jsonl(conn, run_id, tiers=["A", "B", "C"])),
    }


def _campaign_name(run_id: str, stats: DiscoveryStats, imported_count: int) -> str:
    """Build a human-readable campaign name from what this run actually found,
    instead of exposing the internal run_id (e.g. 'disc-20260918-034949') as
    the name -- a person scanning the campaign list needs to recognize a
    campaign by region/date/size at a glance, not decode a job id."""
    tiers = stats.pipeline.tiers if stats.pipeline else {}
    tier_a, tier_b = tiers.get("A", 0), tiers.get("B", 0)
    tier_bits = [f"{n} Tier {t}" for t, n in (("A", tier_a), ("B", tier_b)) if n]
    tier_summary = " + ".join(tier_bits) if tier_bits else f"{imported_count} prospects"

    try:
        when = datetime.strptime(run_id, "disc-%Y%m%d-%H%M%S")
    except ValueError:
        when = datetime.now()

    region = stats.region or "Unknown region"
    return f"{region} — {tier_summary} ({when.strftime('%b')} {when.day}, {when.year})"


async def _handoff_to_outreach(
    cfg: Config, run_id: str, stats: DiscoveryStats, on_progress: Optional[ProgressFn],
) -> None:
    """Pull this run's freshly-qualified Tier A/B prospects into the Outreach
    Agent and draft a campaign for them -- the automatic Research -> Outreach
    hand-off (see run_discovery's docstring). Never sends anything; a
    campaign's items land in `pending` (ready to send, a person still clicks
    send) or `needs_review` (not enough data to trust a personalized draft --
    see Outreach's ingest.create_campaign_from_prospects), same distinction
    that already exists for a low-quality LLM qualification. Skipped
    entirely, not failed, if OUTREACH_API_BASE isn't configured -- this is an
    optional integration, not a requirement to run Research standalone.
    Failures are recorded on stats.outreach, never raised: the research run
    (already scored, qualified, and contacted) must not be reported as
    failed just because the downstream Outreach Agent is unreachable.
    """
    base = cfg.outreach_api_base
    if not base:
        return

    if on_progress:
        on_progress("outreach", 0, 1, "importing prospects")

    from .outreach_client import OutreachClient, OutreachClientError

    client = OutreachClient(base)
    try:
        imported = client.import_prospects(run_id, tiers=["A", "B"])
        imported_count = imported.get("imported", 0)
        excluded = imported.get("excluded_no_contact", [])
        if not imported_count:
            stats.outreach = {
                "imported": 0, "campaign_id": None, "item_count": 0,
                "excluded_no_contact": excluded,
            }
            return

        if on_progress:
            on_progress("outreach", 1, 2, "drafting campaign")

        created = client.create_campaign(
            name=_campaign_name(run_id, stats, imported_count),
            research_run_id=run_id, tiers=["A", "B"],
        )
        campaign = created.get("campaign") or {}
        # Merge import-time and campaign-time exclusions (the latter would
        # only differ if a prospect lost its contact between the two calls,
        # which can't happen inside one synchronous hand-off, but merging
        # keeps this correct if that ever changes) so stats.outreach always
        # names every A/B company left out of this run's hand-off, not just
        # half of them.
        campaign_excluded = created.get("excluded_no_contact", [])
        seen = {e["domain"] for e in excluded}
        excluded = excluded + [e for e in campaign_excluded if e["domain"] not in seen]
        stats.outreach = {
            "imported": imported_count,
            "campaign_id": campaign.get("campaign_id"),
            "item_count": created.get("item_count", 0),
            "excluded_no_contact": excluded,
        }
        if on_progress:
            on_progress("outreach", 2, 2, "done")
    except OutreachClientError as exc:
        stats.outreach = {"error": str(exc)}
    finally:
        client.close()
