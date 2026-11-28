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
import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import contacts as contacts_mod, db, export, pipeline, qualify as qualify_mod
from .dedupe import ExistingCompanies, Match
from .config import Config
from .db import SupabaseConn
from .discovery import (DiscoveryError, WebSearchProvider, deduplicate_and_resolve,
                        enrich_company_firmographics)
from .discovery import site_profile
from .discovery.research import (
    enrichment_completeness, is_field_level_enrichment_reason,
    present_discover_result, present_stored_enrichment_summary,
    stored_enrichment_is_terminal,
)
from .discovery.base import CompanyCandidate
from .http import Fetcher
from .models import Company
from .diagnostics import log_event, worker_version
from .gemini_queue import (
    LATE_RESPONSE_GRACE_SECONDS, GeminiQuotaBlocked, GeminiRetryLater,
)
from .usage_tracker import classify_error

# Enrichment is one LLM round trip per company; run a bounded number at once so
# a 100-company region does not take 100 sequential calls. Kept modest to stay
# well inside provider rate limits.
ENRICH_CONCURRENCY = 2

ProgressFn = Callable[[str, int, int, str], None]
ENRICH_TIMEOUT_SECONDS = 180.0
# Bump when zero-evidence recovery semantics change. A versioned checkpoint
# may be retried once by a newer worker even if an older worker already
# recorded an empty needs_review result.
ENRICHMENT_RECOVERY_VERSION = "2026-11-zero-evidence-v1"
ENRICH_REQUEST_VERSION = "2026-11-async-grounded-v1"
# The official-site pass makes several small public requests (robots, home,
# about/team/contact/careers) with per-request timeouts of its own. This outer
# bound only stops a pathological site from holding an enrichment slot.
SITE_PROFILE_TIMEOUT_SECONDS = 150.0
"""stage, done, total, detail"""


def _call_with_compatible_kwargs(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Pass only the keyword arguments a caller actually accepts.

    The discovery pipeline adds checkpoint/lease hooks over time. Older mocks,
    wrappers, and compatibility shims still call the legacy entrypoints without
    these newer kwargs, so we drop the unsupported names instead of crashing.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return func(*args, **kwargs)

    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    filtered = {
        key: value
        for key, value in kwargs.items()
        if accepts_var_kw or key in params
    }
    return func(*args, **filtered)


def _enrichment_failure_metadata(
    reason: str, run_id: str | None, exc: Exception | None = None,
) -> dict[str, Any]:
    root_exc = exc
    while root_exc is not None and getattr(root_exc, "__cause__", None) is not None:
        root_exc = root_exc.__cause__
    response = getattr(root_exc, "response", None) if root_exc is not None else None
    details = getattr(root_exc, "details", None) if root_exc is not None else None
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    return {
        "provider": "gemini",
        "error_kind": getattr(exc, "classification", None) or reason,
        "status_code": (
            getattr(response, "status_code", None)
            or getattr(root_exc, "status_code", None)
            or getattr(root_exc, "code", None)
            or getattr(exc, "status_code", None)
            or getattr(exc, "code", None)
        ) if exc is not None else None,
        "error_details": ({
            "type": type(root_exc).__name__,
            "message": str(root_exc)[:2000],
            "response": details if details is not None else getattr(response, "text", None),
            "retry_after": retry_after or getattr(exc, "retry_after", None),
        } if exc is not None else None),
        "quota_type": getattr(exc, "classification", None),
        "run_id": run_id,
        "worker_version": worker_version(),
    }


def _enrichment_summaries(companies: list[Company]) -> dict[str, dict[str, Any]]:
    return {
        company.domain: company.research_outcomes.get("_summary")
        or enrichment_completeness(company)
        for company in companies
    }


def _is_field_level_enrichment_outcome(reason: Optional[str]) -> bool:
    return is_field_level_enrichment_reason(reason)


def _operational_enrichment_summary(reason: str) -> dict[str, Any]:
    """Provider failure only. Field-validation text stays on the field outcomes."""
    return {
        "status": "failed",
        "failure_reason": reason,
        "missing_fields": {},
        "blocking_fields": {},
        "review_fields": {},
    }


def _restore_checkpoint_company(checkpoint: dict[str, Any], fallback: Company) -> Company:
    saved = checkpoint.get("payload") or {}
    company = Company.model_validate(saved.get("company") or fallback.model_dump(mode="json"))
    company.research_outcomes = present_stored_enrichment_summary(company.research_outcomes)
    return company


def _has_accepted_research(outcomes: Any) -> bool:
    """True when the stored field outcomes contain at least one usable fact."""
    if not isinstance(outcomes, dict):
        return False
    return any(
        isinstance(value, dict)
        and value.get("status") in {"accepted_and_mapped", "carried_forward"}
        for key, value in outcomes.items()
        if key != "_summary"
    )


def _zero_evidence_review(summary: Any, outcomes: Any) -> bool:
    """A completed review with no usable grounded field is recoverable.

    ``needs_review`` normally means a human should inspect conflicting or
    unverifiable claims. When *nothing* was accepted, however, it is usually a
    provider-grounding outage, not a research conclusion; it must not be
    treated as permanently complete. Human-review decisions are kept distinct
    by the versioned, once-per-worker recovery marker in the checkpoint.
    """
    if not isinstance(summary, dict):
        return False
    return (
        summary.get("status") in {"needs_review", "partial"}
        and not _has_accepted_research(outcomes)
    )


def _checkpoint_recovery_marker(payload: dict[str, Any]) -> tuple[str | None, bool]:
    return (
        payload.get("review_recovery_version"),
        bool(payload.get("review_recovery_attempted")),
    )


def _enrichment_checkpoint_is_reusable(checkpoint: Optional[dict[str, Any]]) -> bool:
    """Finished field research, including older checkpoints mislabeled as failed."""
    if not checkpoint or checkpoint.get("status") not in {"done", "failed"}:
        return False
    payload = checkpoint.get("payload") or {}
    reason = payload.get("reason")
    outcomes = (payload.get("company") or {}).get("research_outcomes") or {}
    summary = outcomes.get("_summary") or payload.get("completeness") or {}
    if isinstance(summary, dict) and summary.get("status") == "research_incomplete":
        return False
    if _zero_evidence_review(summary, outcomes):
        # Applies to a done checkpoint and to an older ``failed`` field-level
        # record: an empty review is a grounding outage, not finished research.
        # One retry per recovery version, then keep the result for a human.
        version, attempted = _checkpoint_recovery_marker(payload)
        return attempted and version == ENRICHMENT_RECOVERY_VERSION
    if checkpoint.get("status") == "done" and not reason:
        return True
    if is_field_level_enrichment_reason(reason):
        # Older rows recorded "ungrounded_response" as a failure even when
        # nothing usable came back. That is not finished research: reuse it
        # only when the stored record actually holds a verified fact (or a
        # classified terminal summary), so a resume retries an empty result
        # instead of skipping it forever.
        status = summary.get("status") if isinstance(summary, dict) else None
        if status in {"complete", "complete_with_gaps", "partial", "needs_review"}:
            return True
        return _has_accepted_research(outcomes)
    summary = ((payload.get("company") or {}).get("research_outcomes") or {}).get("_summary")
    return stored_enrichment_is_terminal(summary) and not reason


def _site_checkpoint_company(payload: dict[str, Any]) -> Optional[Company]:
    saved = payload.get("company")
    if not isinstance(saved, dict):
        return None
    try:
        return Company.model_validate(saved)
    except Exception:
        return None


def _restore_site_checkpoint(company: Company, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Merge only official-site facts that are still missing from ``company``.

    A site checkpoint can be older than the current Gemini result. It must
    never overwrite a stronger field or duplicate evidence, so this copies a
    field only when the live company has no value, and merges only evidence
    that the checkpoint itself marks as official-site retrieval.
    """
    profile = payload.get("profile")
    if not isinstance(profile, dict) or profile.get("status") != "ok":
        return None
    saved = _site_checkpoint_company(payload)
    if saved is None:
        return None
    site_fields = (
        "website", "headquarters", "company_type", "business_category",
        "employee_count", "revenue", "funding", "description", "industry",
        "hq_state",
    )
    for field in site_fields:
        if getattr(company, field) is None and getattr(saved, field) is not None:
            setattr(company, field, getattr(saved, field))
    if not company.careers_url and saved.careers_url:
        company.careers_url = saved.careers_url

    seen: set[tuple] = set()
    for item in company.research_evidence:
        if isinstance(item, dict):
            seen.add((item.get("evidence_key"), item.get("field"), item.get("source_url"), item.get("excerpt")))
    for item in saved.research_evidence:
        if not isinstance(item, dict):
            continue
        is_site = item.get("source_tier") == 1 or bool(item.get("informational"))
        if not is_site:
            continue
        identity = (item.get("evidence_key"), item.get("field"), item.get("source_url"), item.get("excerpt"))
        if identity in seen:
            continue
        seen.add(identity)
        company.research_evidence.append(item)

    for field, outcome in saved.research_outcomes.items():
        if field.startswith("_"):
            continue
        current = company.research_outcomes.get(field)
        if not isinstance(current, dict) or current.get("status") in {None, "not_searched"}:
            company.research_outcomes[field] = outcome
    company.research_outcomes["_site_profile"] = profile
    return profile


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
    duplicates_existing: int = 0
    company_outcomes: list[dict[str, Any]] = field(default_factory=list)
    reeligible: int = 0
    """Previously discovered companies that were never qualified (or hand-off
    eligible) and so re-entered scoring/qualification in this run."""
    existing_matches: list[dict[str, str]] = field(default_factory=list)
    """Companies this run found that ALREADY exist in the database (by
    normalized domain, else normalized name): [{"domain", "matched_by"}]. They
    are reused as-is -- not re-inserted, re-scored, re-enriched, or handed to
    Outreach -- and are listed on the run's Companies tab as existing matches
    (see db.save_run_matches)."""
    enrich_failures: dict[str, int] = field(default_factory=dict)
    enrich_partial: int = 0
    enrich_needs_review: int = 0
    enrich_complete_with_gaps: int = 0
    enrichment_summary: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        payload = {
            "region": self.region,
            "run_id": self.run_id,
            "discovered": self.discovered,
            "enriched": self.enriched,
            "enrich_skipped": self.enrich_skipped,
            "enrich_failures": self.enrich_failures,
            "enrich_partial": self.enrich_partial,
            "enrich_needs_review": self.enrich_needs_review,
            "enrich_complete_with_gaps": self.enrich_complete_with_gaps,
            "enrichment_summary": self.enrichment_summary,
            "duplicates_existing": self.duplicates_existing,
            "company_outcomes": [
                *self.company_outcomes,
                *(self.pipeline.company_outcomes if self.pipeline else []),
            ],
            "scoring_failures": self.pipeline.failed if self.pipeline else 0,
            "reeligible": self.reeligible,
            "existing_matches": self.existing_matches,
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
        return present_discover_result(payload)


def _needs_enrichment(existing: Optional[Company]) -> bool:
    """Enrich unless persisted field outcomes prove the research is terminal."""
    if existing is None:
        return True
    return not stored_enrichment_is_terminal(existing.research_outcomes.get("_summary"))


def _needs_identity_enrichment(existing: Optional[Company]) -> bool:
    """Use the same persisted terminal state for identity and firmographic work."""
    if existing is None:
        return True
    return not stored_enrichment_is_terminal(existing.research_outcomes.get("_summary"))


def _restore_discovery_manifest(
    checkpoint: Optional[dict[str, Any]],
) -> Optional[tuple[str, list[CompanyCandidate]]]:
    if not checkpoint or checkpoint.get("status") != "done":
        return None
    payload = checkpoint.get("payload") or {}
    provider_name = payload.get("provider_name")
    if not isinstance(provider_name, str) or not provider_name:
        return None
    return provider_name, [
        CompanyCandidate.model_validate(row) for row in payload.get("candidates", [])
    ]


async def _profile_official_site(
    company: Company,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]],
    run_id: Optional[str],
    ensure_owned: Optional[Callable[[], None]],
    failure_reason: Optional[str] = None,
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> None:
    """Additive deterministic pass over the company's own public pages.

    Runs after Gemini enrichment, before the company's checkpoint is written,
    so the facts and their provenance travel with the same resumable payload.
    A stage checkpoint records retrieval status for diagnosis; the pass itself
    is idempotent (evidence is keyed, accepted fields are not overwritten) and
    never fails the enrichment -- a company whose site is down keeps exactly
    the model-derived result it had.
    """
    stage_key = f"site:{company.domain}"
    existing = company.research_outcomes.get("_site_profile")
    if isinstance(existing, dict) and existing.get("status") == "ok":
        if failure_reason:
            company.research_outcomes["_summary"] = {
                **enrichment_completeness(company),
                **_operational_enrichment_summary(failure_reason),
            }
        return
    if not site_profile.normalize_domain(company.domain or ""):
        return
    saved_stage = checkpoint_load(stage_key) if checkpoint_load else None
    if saved_stage and saved_stage.get("status") == "done":
        restored = _restore_site_checkpoint(company, saved_stage.get("payload") or {})
        if restored is not None:
            log_event(
                "site_profile_checkpoint_reused", domain=company.domain,
                run_id=run_id, extractor=restored.get("extractor"),
            )
            company.research_outcomes["_summary"] = (
                {
                    **enrichment_completeness(company),
                    **_operational_enrichment_summary(failure_reason),
                }
                if failure_reason else enrichment_completeness(company)
            )
            return
    if ensure_owned:
        ensure_owned()
    if checkpoint_save and not checkpoint_save(
        stage_key, "started", {"domain": company.domain, "run_id": run_id},
    ):
        raise asyncio.CancelledError()
    try:
        profile = await asyncio.wait_for(
            site_profile.profile_official_site(company),
            timeout=SITE_PROFILE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        profile = {
            "status": "timeout", "extractor": site_profile.EXTRACTOR_VERSION,
            "error": f"official-site pass exceeded {SITE_PROFILE_TIMEOUT_SECONDS:.0f}s",
        }
    except Exception as exc:  # never let a site failure change enrichment
        profile = {
            "status": "error", "extractor": site_profile.EXTRACTOR_VERSION,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }
    if profile is None:
        return
    company.research_outcomes["_site_profile"] = profile
    if failure_reason:
        # Profiler facts are kept, but a provider failure remains the overall
        # result so the summary stays non-terminal and a later run retries.
        company.research_outcomes["_summary"] = {
            **enrichment_completeness(company),
            **_operational_enrichment_summary(failure_reason),
        }
    else:
        company.research_outcomes["_summary"] = enrichment_completeness(company)
    if checkpoint_save and not checkpoint_save(
        stage_key, "done", {
            "run_id": run_id,
            "profile": profile,
            # Persist the exact post-profile snapshot so a restart between
            # this stage and the enrichment checkpoint can reuse the facts
            # without re-fetching the site. The merge above only fills gaps.
            "company": company.model_dump(mode="json"),
            "request_version": ENRICH_REQUEST_VERSION,
        },
    ):
        raise asyncio.CancelledError()


async def _enrich_all(
    conn: SupabaseConn, candidates: list, region: str, on_progress: Optional[ProgressFn],
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
    ensure_owned: Optional[Callable[[], None]] = None,
    run_id: Optional[str] = None,
) -> tuple[list[tuple[Any, Company]], int, dict[str, int]]:
    """Build Company rows, enriching only the ones we have not researched yet."""
    pairs: list[tuple[Any, Company]] = []
    todo: list[Company] = []
    resumed_gemini: set[str] = set()
    skipped = 0
    failures: dict[str, int] = {}

    for c in candidates:
        existing = db.get_company(conn, c.domain)
        company = Company(
            domain=c.domain,
            name=c.name or (existing.name if existing else None),
            region=(existing.region if existing and existing.region else region),
            industry=c.industry or (existing.industry if existing else None),
            status="discovered",
        )
        if existing is not None:
            for field in (
                "website", "headquarters", "company_type", "business_category",
                "employee_count", "revenue", "funding", "classification_evidence",
                "research_evidence", "research_outcomes", "description", "industry",
                "hq_state",
            ):
                setattr(company, field, getattr(existing, field))
        checkpoint = checkpoint_load(f"enrich:{c.domain}") if checkpoint_load else None
        if _enrichment_checkpoint_is_reusable(checkpoint):
            company = _restore_checkpoint_company(checkpoint or {}, company)
            skipped += 1
            pairs.append((c, company))
            continue
        if checkpoint and (
            checkpoint.get("status") in {"failed", "queued"}
            or (checkpoint.get("status") == "done" and (checkpoint.get("payload") or {}).get("reason"))
            or checkpoint.get("status") == "researched"
        ):
            saved = checkpoint.get("payload") or {}
            company = Company.model_validate(saved.get("company") or company.model_dump(mode="json"))
            if checkpoint.get("status") == "researched":
                resumed_gemini.add(company.domain)
            todo.append(company)
            pairs.append((c, company))
            continue
        if checkpoint and checkpoint.get("status") == "started":
            reason = "interrupted"
            saved = checkpoint.get("payload") or {}
            company = Company.model_validate(saved.get("company") or company.model_dump(mode="json"))
            company.research_outcomes["_summary"] = {
                **enrichment_completeness(company), "status": "partial",
                "failure_reason": reason,
            }
            if checkpoint_save and not checkpoint_save(
                f"enrich:{c.domain}", "failed",
                {"company": company.model_dump(mode="json"), "reason": reason,
                 "provider_error": _enrichment_failure_metadata(reason, run_id)},
            ):
                raise asyncio.CancelledError()
            todo.append(company)
            pairs.append((c, company))
            continue
        if existing is not None and not (_needs_enrichment(existing) or _needs_identity_enrichment(existing)):
            # Carry forward what we already know instead of paying for it again.
            company.employee_count = existing.employee_count
            company.revenue = existing.revenue
            company.funding = existing.funding
            company.website = existing.website
            company.headquarters = existing.headquarters
            company.company_type = existing.company_type
            company.classification_evidence = existing.classification_evidence
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
            if ensure_owned:
                ensure_owned()
            if on_progress:
                on_progress("enrich", done, len(todo), f"researching {company.domain}")
            checkpoint_key = f"enrich:{company.domain}"
            resumed = company.domain in resumed_gemini
            reason: Optional[str] = None
            if not resumed and checkpoint_save and not checkpoint_save(checkpoint_key, "started", {
                "company": company.model_dump(mode="json"),
                "run_id": run_id,
                "worker_version": worker_version(),
                "request_version": ENRICH_REQUEST_VERSION,
            }):
                raise asyncio.CancelledError()
            try:
                if not resumed:
                    # Tavily/Groq fallback is disabled; only Gemini-only enrichment runs.
                    enrichment = _call_with_compatible_kwargs(
                        enrich_company_firmographics, company, run_id=run_id,
                        timeout_seconds=ENRICH_TIMEOUT_SECONDS,
                        late_grace_seconds=LATE_RESPONSE_GRACE_SECONDS,
                    )
                    # The queue owns the soft deadline and late grace; this is
                    # only a hard safety net for a compatibility shim that
                    # ignores its timeout. Real production calls are async and
                    # cancellable, so no provider thread survives this bound.
                    reason = await asyncio.wait_for(
                        enrichment,
                        timeout=ENRICH_TIMEOUT_SECONDS + LATE_RESPONSE_GRACE_SECONDS + 1.0,
                    )
                if ensure_owned:
                    ensure_owned()
                completeness = enrichment_completeness(company)
                if _is_field_level_enrichment_outcome(reason):
                    if reason == "ungrounded_response" and completeness["status"] != "needs_review":
                        completeness["status"] = "needs_review"
                        completeness.setdefault("review_fields", {})["grounding"] = (
                            "One or more claims did not meet the grounding requirements"
                        )
                    reason = None
                if reason is None and not company.research_outcomes.get("_summary"):
                    company.research_outcomes["_summary"] = completeness
                if reason:
                    failures[reason] = failures.get(reason, 0) + 1
                    company.research_outcomes["_summary"] = _operational_enrichment_summary(reason)
                elif not resumed:
                    # Gemini has completed and its result is durable before the
                    # site pass starts. A restart can therefore resume at the
                    # profiler instead of paying for the same Gemini call again.
                    if checkpoint_save and not checkpoint_save(
                        checkpoint_key, "researched", {
                            "company": company.model_dump(mode="json"),
                            "run_id": run_id,
                            "reason": None,
                            "completeness": company.research_outcomes.get("_summary"),
                            "request_version": ENRICH_REQUEST_VERSION,
                        },
                    ):
                        raise asyncio.CancelledError()
            except (GeminiQuotaBlocked, GeminiRetryLater) as exc:
                if checkpoint_save and not checkpoint_save(
                    checkpoint_key, "queued",
                    {"company": company.model_dump(mode="json"), "reason": str(exc),
                     "provider_error": _enrichment_failure_metadata(str(exc), run_id, exc),
                     "retry_after_seconds": getattr(exc, "retry_after", None)
                     or getattr(exc, "delay_seconds", None)},
                ):
                    raise asyncio.CancelledError()
                raise
            except (asyncio.TimeoutError, TimeoutError):
                reason = "timeout"
                company.research_outcomes["_summary"] = _operational_enrichment_summary(reason)
                print(f"  [WARN] enrichment timed out after {ENRICH_TIMEOUT_SECONDS:.0f}s "
                      f"(plus {LATE_RESPONSE_GRACE_SECONDS:.0f}s grace) for {company.domain}")
                log_event("enrichment_timeout", domain=company.domain, run_id=run_id,
                          timeout_seconds=ENRICH_TIMEOUT_SECONDS,
                          late_grace_seconds=LATE_RESPONSE_GRACE_SECONDS,
                          cancellation="async_task_cancelled_after_grace")
                failures[reason] = failures.get(reason, 0) + 1
            except Exception as exc:  # a failed lookup must not kill the run
                reason = classify_error(exc)
                company.research_outcomes["_summary"] = _operational_enrichment_summary(reason)
                print(f"  [WARN] enrichment failed for {company.domain} ({reason}): "
                      f"{type(exc).__name__}: {exc}")
                failures[reason] = failures.get(reason, 0) + 1

            # The official-site pass runs after every terminal Gemini outcome,
            # including provider failures. It can add verified facts and
            # provenance, but failure_reason keeps an operational failure
            # non-terminal and visible; the profiler can never turn it into a
            # successful enrichment.
            await _profile_official_site(
                company, checkpoint_save, run_id, ensure_owned,
                failure_reason=reason, checkpoint_load=checkpoint_load,
            )
            checkpoint_status = "failed" if reason else "done"
            payload: dict[str, Any] = {
                "company": company.model_dump(mode="json"),
                "reason": reason,
                "completeness": company.research_outcomes.get("_summary")
                or enrichment_completeness(company),
                "request_version": ENRICH_REQUEST_VERSION,
            }
            if reason:
                payload["provider_error"] = _enrichment_failure_metadata(reason, run_id)
            summary = company.research_outcomes.get("_summary")
            if _zero_evidence_review(summary, company.research_outcomes):
                payload["review_recovery_version"] = ENRICHMENT_RECOVERY_VERSION
                payload["review_recovery_attempted"] = True
            if checkpoint_save and not checkpoint_save(
                checkpoint_key, checkpoint_status, payload,
            ):
                raise asyncio.CancelledError()
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
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
    ensure_owned: Optional[Callable[[], None]] = None,
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
        if ensure_owned:
            ensure_owned()
        h = cfg.http

        # --- 1. discover ---
        if on_progress:
            on_progress("discover", 0, 1, region)
        discovery_checkpoint = checkpoint_load("discovery") if checkpoint_load else None
        restored_manifest = _restore_discovery_manifest(discovery_checkpoint)
        if restored_manifest is not None:
            provider_name, candidates = restored_manifest
        else:
            if checkpoint_save and not checkpoint_save("discovery", "started", {}):
                raise asyncio.CancelledError()
            async with Fetcher(
                user_agent=h["user_agent"], per_host_delay=float(h["per_host_delay_seconds"]),
                timeout=float(h["timeout_seconds"]), cache_ttl_hours=float(h["cache_ttl_hours"]),
                max_concurrency=int(h["max_concurrency"]), use_cache=use_cache,
            ) as fetcher:
                provider = WebSearchProvider(fetcher=fetcher, cfg=cfg, run_id=rid)
                candidates = deduplicate_and_resolve(
                    await provider.discover(region=region, limit=limit))
            if ensure_owned:
                ensure_owned()
            provider_name = provider.name
            if checkpoint_save and not checkpoint_save("discovery", "done", {
                "provider_name": provider_name,
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            }):
                raise asyncio.CancelledError()
        stats.discovered = len(candidates)
        if on_progress:
            on_progress("discover", 1, 1, f"{len(candidates)} companies")

        if not candidates:
            return stats

        # --- 1b. global dedup: reuse companies that already exist ---
        # A company found by an earlier run (and still present) is matched,
        # never re-created. Domains this run already scored are its own
        # (a resumed/re-run run_id), not duplicates.
        existing = ExistingCompanies.load(conn)
        own = {r["domain"] for r in conn.select("scores", run_id=f"eq.{rid}", select="domain")}
        # "Already discovered" is not "already qualified": only a company that
        # was qualified in some run (or is suppressed/disqualified) is a true
        # duplicate. One that was merely discovered/scored and missed an
        # earlier top-N stays eligible and re-enters the pipeline below.
        found = [(c, existing.lookup(c.domain, c.name)) for c in candidates]
        settled = db.settled_domains(conn, sorted({m.domain for _, m in found if m is not None}))
        matches: dict[str, Match] = {}
        reeligible: set[str] = set()
        fresh = []
        for c, m in found:
            if m is not None and m.domain not in own and m.domain not in settled:
                # Resolve to the stored domain so a name-match updates the
                # existing row instead of inserting a second company.
                if m.domain != c.domain:
                    c = c.model_copy(update={"domain": m.domain})
                if m.domain not in reeligible:
                    reeligible.add(m.domain)
                    fresh.append(c)
            elif m is not None and m.domain not in own:
                matches.setdefault(m.domain, m)  # first (strongest) match wins
                provider_rows = [{
                    "domain": m.domain, "provider_name": provider_name, "url": c.source_url,
                    "discovered_at": c.discovered_at.isoformat() if c.discovered_at else None,
                }]
                try:
                    db.save_discovery_sources(conn, provider_rows)
                except db.SupabaseError as exc:
                    print(f"  [WARN] discovery_sources save failed: {exc}")
            else:
                fresh.append(c)
        candidates = fresh
        stats.discovered = len(candidates) + len(matches)

        # --- 2. enrich (skips companies already researched) ---
        pairs, skipped, enrich_failures = await _enrich_all(
            conn, candidates, region, on_progress,
            checkpoint_load=checkpoint_load,
            checkpoint_save=checkpoint_save,
            ensure_owned=ensure_owned,
            run_id=rid,
        )
        stats.enrichment_summary = _enrichment_summaries([company for _, company in pairs])
        stats.enriched = max(0, sum(
            summary.get("status") == "complete"
            for summary in stats.enrichment_summary.values()
        ) - skipped)
        stats.enrich_complete_with_gaps = sum(
            summary.get("status") == "complete_with_gaps"
            for summary in stats.enrichment_summary.values()
        )
        stats.enrich_skipped = skipped
        stats.enrich_failures = enrich_failures
        stats.enrich_partial = sum(
            summary.get("status") == "partial"
            for summary in stats.enrichment_summary.values()
        )
        stats.enrich_needs_review = sum(
            summary.get("status") == "needs_review"
            for summary in stats.enrichment_summary.values()
        )

        if ensure_owned:
            ensure_owned()

        # --- 3. persist ---
        # claim_company is an atomic insert-if-absent: if a concurrent run
        # created this company between our check and now, we lose the claim
        # and treat it as an existing match instead of overwriting it.
        rows = []
        claimed_pairs = []
        for candidate, company in pairs:
            if ensure_owned:
                ensure_owned()
            if (not db.claim_company(conn, company) and company.domain not in own
                    and company.domain not in reeligible):
                matches[company.domain] = Match(company.domain, "domain")
                continue
            claimed_pairs.append((candidate, company))
            rows.append({
                "domain": candidate.domain,
                "provider_name": provider_name,
                "url": candidate.source_url,
                "discovered_at": (candidate.discovered_at.isoformat()
                                  if candidate.discovered_at else None),
            })
        pairs = claimed_pairs
        try:
            db.save_discovery_sources(conn, rows)
        except db.SupabaseError as exc:
            print(f"  [WARN] discovery_sources save failed: {exc}")
        conn.commit()

        stats.existing_matches = [{"domain": m.domain, "matched_by": m.matched_by}
                                  for m in matches.values()]
        stats.duplicates_existing = len(stats.existing_matches)
        if matches:
            match_domains = sorted(matches)
            in_list = f"in.({','.join(match_domains)})"
            qualified_domains = {
                row["domain"] for row in conn.select(
                    "qualifications", domain=in_list, select="domain",
                )
            }
            statuses = {
                row["domain"]: row.get("status") for row in conn.select(
                    "companies", domain=in_list, select="domain,status",
                )
            }
            suppressed_domains = set(db.load_suppression(conn))
            for match in matches.values():
                if match.domain in qualified_domains:
                    reason = "qualified_in_prior_run"
                elif statuses.get(match.domain) == "disqualified":
                    reason = "already_disqualified"
                elif statuses.get(match.domain) == "suppressed" or match.domain in suppressed_domains:
                    reason = "suppressed"
                else:
                    reason = "already_settled"
                stats.company_outcomes.append({
                    "domain": match.domain,
                    "outcome": "duplicate",
                    "reason": reason,
                    "matched_by": match.matched_by,
                })
        stats.reeligible = sum(1 for _, c in pairs if c.domain in reeligible)
        stats.discovered = len(pairs) + stats.duplicates_existing
        if matches:
            db.save_run_matches(conn, rid, stats.existing_matches)

        if not pairs:
            # Everything found already exists: still register the run (so its
            # matches have a run to live under) but score/hand off nothing.
            db.start_run(conn, rid, cfg.raw.get("profile", "default"), 0,
                         label=label or f"discover {region}")
            db.finish_run(conn, rid, notes="all discovered companies already exist")
            return stats

        # --- 4. ATS + jobs + scoring ---
        def pipeline_progress(done: int, total: int, domain: str) -> None:
            if on_progress:
                on_progress("pipeline", done, total, domain)

        stats.pipeline = await pipeline.run(
            cfg, [c for _, c in pairs], conn, rid,
            use_cache=use_cache, on_progress=pipeline_progress,
            label=label or f"discover {region}",
            skip_scored=checkpoint_save is not None,
            ensure_owned=ensure_owned,
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
            await _call_with_compatible_kwargs(
                _auto_qualify_and_contact,
                conn, rid, top_n, stats, on_progress,
                checkpoint_load=checkpoint_load,
                checkpoint_save=checkpoint_save,
                ensure_owned=ensure_owned,
            )
            if stats.qualify or stats.contacts:
                if ensure_owned:
                    ensure_owned()
                await _call_with_compatible_kwargs(
                    _handoff_to_outreach,
                    cfg, rid, stats, on_progress,
                    checkpoint_load=checkpoint_load,
                    checkpoint_save=checkpoint_save,
                    ensure_owned=ensure_owned,
                )

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
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
    ensure_owned: Optional[Callable[[], None]] = None,
) -> DiscoveryStats:
    """Continue a run for companies stalled by Gemini processing.

    Unlike run_discovery, this does not re-run discovery. It re-enriches only
    these domains and re-runs the scoring pipeline under the same run_id.
    Tavily/Groq processing is disabled, so successful Gemini quota recovery is
    the only provider path that can complete stalled enrichment.
    """
    if not domains:
        raise ValueError("resume_stalled called with no domains -- nothing to resume")

    rid = run_id
    stats = DiscoveryStats(region="", run_id=rid)

    owns_conn = conn is None
    conn = conn or db.connect()
    try:
        if ensure_owned:
            ensure_owned()
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

        # --- re-enrich only the stalled ones through the same resumable path ---
        # resume_stalled must not maintain a second enrichment loop: that is
        # how the old to_thread/wait_for timeout survived here after the main
        # path moved to cancellable async calls. Reusing _enrich_all also
        # applies the researched-stage checkpoint, zero-evidence recovery, and
        # official-site profiler resume rules to a restarted worker.
        candidates = [
            CompanyCandidate(
                domain=company.domain,
                name=company.name,
                source="resume",
                source_url=(company.website.value if company.website and isinstance(company.website.value, str) else None),
                industry=company.industry,
                discovered_at=datetime.now(timezone.utc),
            )
            for company in companies
        ]
        pairs, skipped, failures = await _enrich_all(
            conn, candidates, companies[0].region or "", on_progress,
            checkpoint_load=checkpoint_load,
            checkpoint_save=checkpoint_save,
            ensure_owned=ensure_owned,
            run_id=rid,
        )
        companies = [company for _, company in pairs]
        stats.enrichment_summary = _enrichment_summaries(companies)
        stats.enriched = max(0, sum(
            summary.get("status") == "complete"
            for summary in stats.enrichment_summary.values()
        ) - skipped)
        stats.enrich_complete_with_gaps = sum(
            summary.get("status") == "complete_with_gaps"
            for summary in stats.enrichment_summary.values()
        )
        stats.enrich_skipped = skipped
        stats.enrich_failures = failures
        stats.enrich_partial = sum(
            summary.get("status") == "partial"
            for summary in stats.enrichment_summary.values()
        )
        stats.enrich_needs_review = sum(
            summary.get("status") == "needs_review"
            for summary in stats.enrichment_summary.values()
        )

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
            ensure_owned=ensure_owned,
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
    on_progress: Optional[ProgressFn], ensure_owned: Optional[Callable[[], None]] = None,
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
) -> None:
    """Qualify + find contacts for the top `top_n` Tier A/B scored companies
    -- see run_discovery's docstring.

    `top_n` slices AFTER filtering to Tier A/B, not before: ranking the full
    (all-tier) list first and filtering to A/B afterward would let Tier C/D
    companies occupy slots in the top-N window, silently pushing genuinely
    eligible Tier A/B companies (ranked N+1, N+2, ... among A/B specifically)
    out of qualification even though every requirement (make sure every
    Tier A/B prospect gets qualified, up to the cost cap) says they should be
    covered. Tier is a monotonic function of score, so in practice this only
    differs from the old "slice-then-filter" behavior when there are MORE
    than top_n Tier A/B companies in the run -- exactly the case that must
    not be silently short-changed.

    Failures here are recorded on `stats`, not raised: the discovery/scoring
    run itself already succeeded and must not be reported as failed just
    because the optional qualify/contacts add-on hit a missing API key or
    quota limit.
    """
    eligible = [r for r in db.get_scores(conn, run_id)
                if not r.get("disqualified") and r["tier"] in ("A", "B")]
    ranked = eligible[:top_n]
    domains = [r["domain"] for r in ranked]
    stats.auto_qualify_top_n = len(ranked)
    stats.auto_qualify_skipped_tier = 0
    if not domains:
        return
    if ensure_owned:
        ensure_owned()

    def qual_progress(done: int, total: int, domain: str) -> None:
        if on_progress:
            on_progress("qualify", done, total, domain)

    try:
        stats.qualify = await _call_with_compatible_kwargs(
            qualify_mod.qualify_run,
            conn, run_id, domains=domains, on_progress=qual_progress, ensure_owned=ensure_owned,
        )
    except qualify_mod.QualificationError as exc:
        stats.qualify = {"error": str(exc)}

    def contact_progress(done: int, total: int, domain: str) -> None:
        if on_progress:
            on_progress("contacts", done, total, domain)

    try:
        stats.contacts = await _call_with_compatible_kwargs(
            contacts_mod.discover_run,
            conn, domains, on_progress=contact_progress,
            checkpoint_load=(lambda key: checkpoint_load(f"auto:{key}")) if checkpoint_load else None,
            checkpoint_save=(
                lambda key, status, payload: checkpoint_save(f"auto:{key}", status, payload)
            ) if checkpoint_save else None,
            ensure_owned=ensure_owned,
        )
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
    checkpoint_load: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    checkpoint_save: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
    ensure_owned: Optional[Callable[[], None]] = None,
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

    handoff_key = "outreach_handoff"
    existing = checkpoint_load(handoff_key) if checkpoint_load else None
    if existing and existing.get("status") == "done":
        stats.outreach = (existing.get("payload") or {}).get("result")
        return
    if ensure_owned:
        ensure_owned()
    if checkpoint_save and not checkpoint_save(handoff_key, "started", {"run_id": run_id}):
        raise asyncio.CancelledError()

    if on_progress:
        on_progress("outreach", 0, 1, "importing prospects")

    from .outreach_client import OutreachClient, OutreachClientError

    client = OutreachClient(base)
    try:
        imported = _call_with_compatible_kwargs(
            client.import_prospects,
            run_id,
            tiers=["A", "B"],
            idempotency_key=f"research-handoff-import:{run_id}",
        )
        if ensure_owned:
            ensure_owned()
        imported_count = imported.get("imported", 0)
        excluded = imported.get("excluded_no_contact", [])
        if not imported_count:
            stats.outreach = {
                "imported": 0, "campaign_id": None, "item_count": 0,
                "excluded_no_contact": excluded,
            }
            if checkpoint_save and not checkpoint_save(
                handoff_key, "done", {"run_id": run_id, "result": stats.outreach},
            ):
                raise asyncio.CancelledError()
            return

        if on_progress:
            on_progress("outreach", 1, 2, "drafting campaign")

        created = _call_with_compatible_kwargs(
            client.create_campaign,
            name=_campaign_name(run_id, stats, imported_count),
            research_run_id=run_id, tiers=["A", "B"],
            campaign_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"csg-research-run:{run_id}")),
        )
        if ensure_owned:
            ensure_owned()
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
        if checkpoint_save and not checkpoint_save(
            handoff_key, "done", {"run_id": run_id, "result": stats.outreach},
        ):
            raise asyncio.CancelledError()
        if on_progress:
            on_progress("outreach", 2, 2, "done")
    except OutreachClientError as exc:
        if exc.retryable:
            raise
        stats.outreach = {"error": str(exc)}
        if checkpoint_save and not checkpoint_save(
            handoff_key, "done", {"run_id": run_id, "result": stats.outreach},
        ):
            raise asyncio.CancelledError()
    finally:
        client.close()
