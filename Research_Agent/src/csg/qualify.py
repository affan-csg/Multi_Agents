"""Step 4: LLM qualification layer.

Deterministic scoring (scoring.py) never calls an LLM and stays that way --
this module runs strictly after a company is already scored, and only turns
the arithmetic score + open reqs into a human-readable pain_summary and
recommended_angle. It never re-scores or overrides a tier.

Two backends, selected at runtime by whichever key is present in the
environment. Gemini is currently tried first (see _select_backend) since it
has a free tier and the Anthropic account on file has no credit balance:
  - Gemini (gemini-3.6-flash)   -- has a free tier
  - Anthropic (Claude Opus 5)   -- paid, no free tier

Costs real tokens/quota either way -- callers should scope runs to qualifying
tiers (default A/B) rather than the full funnel.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional, Protocol

from pydantic import BaseModel

from . import db
from .config import PROJECT_ROOT
from .db import SupabaseConn
from .usage_tracker import classify_error, track

PROMPT_PATH = PROJECT_ROOT / "prompts" / "qualify.md"
ANTHROPIC_MODEL = "claude-opus-5"
GEMINI_MODEL = "gemini-3.6-flash"
MAX_CONCURRENCY = 4
# Gemini's free tier is rate-limited per-minute (as low as 5 req/min depending
# on model/account) -- run qualification serially against it so parallel
# workers don't all 429 at once. Anthropic has no such default constraint.
GEMINI_MAX_CONCURRENCY = 1
MAX_RETRIES = 4
BASE_RETRY_DELAY = 20.0


class QualificationError(RuntimeError):
    pass


class _Qualification(BaseModel):
    pain_summary: str
    recommended_angle: str


def _build_context(row: dict[str, Any], postings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "domain": row["domain"],
        "name": row.get("name"),
        "employee_count": row.get("employee_count"),
        "industry": row.get("industry"),
        "tier": row["tier"],
        "score": row["score"],
        "dimensions": row["dimensions"],
        "explanation": row["explanation"],
        "metrics": {k: v for k, v in row["metrics"].items() if k != "top_titles"},
        "open_reqs_sample": postings,
    }


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _render_prompt(row: dict[str, Any], postings: list[dict[str, Any]]) -> str:
    context = _build_context(row, postings)
    template = _load_prompt_template()
    return template.replace("{context_json}", json.dumps(context, indent=2, default=str))


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

class _Backend(Protocol):
    model: str
    max_concurrency: int

    async def complete(self, prompt: str) -> _Qualification: ...
    async def close(self) -> None: ...
    def is_retryable(self, exc: Exception) -> bool: ...


class _AnthropicBackend:
    def __init__(self, api_key: str) -> None:
        import anthropic
        self.model = ANTHROPIC_MODEL
        self.max_concurrency = MAX_CONCURRENCY
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, prompt: str) -> _Qualification:
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=_Qualification,
        )
        return response.parsed_output

    async def close(self) -> None:
        await self._client.close()

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, self._anthropic.RateLimitError):
            return True
        # 5xx: transient server-side overload, distinct from 4xx client errors.
        return isinstance(exc, self._anthropic.APIStatusError) and exc.status_code >= 500


class _GeminiBackend:
    """Rotates through every GEMINI_API_KEY* in .env on daily-quota
    exhaustion (see discovery/gemini_keys.py). `is_retryable` reports False
    for daily-quota errors -- that tells `_complete_with_retry` not to
    backoff-retry the same key, but `complete()` itself already advanced to
    the next key before raising, so the *next* call from the caller's own
    retry loop lands on a fresh key rather than repeating the exhausted one.
    """

    def __init__(self, api_key: str) -> None:
        from google import genai
        from .discovery.gemini_keys import GeminiKeyPool, is_connection_error, make_gemini_client
        self.model = GEMINI_MODEL
        self.max_concurrency = GEMINI_MAX_CONCURRENCY
        self._genai = genai
        self._make_client = make_gemini_client
        self._is_connection_error = is_connection_error
        self._pool = GeminiKeyPool.from_env()
        if not self._pool:
            self._pool = GeminiKeyPool(keys=[api_key])
        self._last_daily_exhausted = False

    async def complete(self, prompt: str) -> _Qualification:
        from google.genai import types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_Qualification,
            max_output_tokens=2048,
        )

        last_exc: Exception | None = None
        for attempt in self._pool.attempts():
            client = self._make_client(attempt.key)
            try:
                with track("gemini", "qualify") as u:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=self.model, contents=prompt, config=config,
                    )
                    meta = getattr(response, "usage_metadata", None)
                    if meta is not None:
                        u.set_tokens(
                            prompt=getattr(meta, "prompt_token_count", None),
                            completion=getattr(meta, "candidates_token_count", None),
                            total=getattr(meta, "total_token_count", None),
                        )
            except Exception as exc:
                last_exc = exc
                if self._is_daily_quota_exhausted(exc):
                    print(f"[qualify] Gemini key {attempt.index}/{attempt.total} "
                          f"quota exhausted, trying next key", file=sys.stderr)
                    self._pool.mark_exhausted(attempt.key)
                    self._last_daily_exhausted = False  # this key's error was consumed here
                    continue
                self._last_daily_exhausted = False
                if self._is_connection_error(exc):
                    print(f"[qualify] Gemini key {attempt.index}/{attempt.total} "
                          f"unreachable (connection/TLS error, not quota): {exc}", file=sys.stderr)
                raise
            parsed = response.parsed
            if parsed is None:
                raise QualificationError(f"Gemini returned no parseable output: {response.text!r}")
            return parsed

        # Every key exhausted -- surface via is_retryable's normal contract
        # (False = do not backoff-retry) so the caller's loop raises cleanly.
        self._last_daily_exhausted = True
        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        pass

    def is_retryable(self, exc: Exception) -> bool:
        if not isinstance(exc, self._genai.errors.APIError):
            return False
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code >= 500:
            return True
        if code != 429:
            return False
        # complete() already rotated through every available key before this
        # exception reached here, so a 429 at this point means the whole pool
        # is exhausted -- retrying the same (still-exhausted) key wastes the
        # backoff window on an error that only resets tomorrow.
        if self._last_daily_exhausted or self._is_daily_quota_exhausted(exc):
            return False
        return True

    def _is_daily_quota_exhausted(self, exc: Exception) -> bool:
        from .discovery.gemini_keys import is_daily_quota_exhausted
        return is_daily_quota_exhausted(exc)


def _select_backend() -> _Backend:
    # Gemini first for now (free tier; Anthropic key on file has no credit) --
    # flip this order back once the Anthropic account is funded.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if gemini_key:
        try:
            return _GeminiBackend(gemini_key)
        except ImportError as exc:
            raise QualificationError(
                "GEMINI_API_KEY is set but the 'google-genai' package is not installed. "
                "Run `pip install -e .`."
            ) from exc

    if anthropic_key:
        try:
            return _AnthropicBackend(anthropic_key)
        except ImportError as exc:
            raise QualificationError(
                "ANTHROPIC_API_KEY is set but the 'anthropic' package is not installed. "
                "Run `pip install -e .`."
            ) from exc

    raise QualificationError(
        "no LLM API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env "
        "(see .env.example) before running `csg qualify`."
    )


def backend_name() -> str:
    """Which provider+model would be used, without opening a client -- for
    cost-preview / confirmation prompts that must not require network access
    or hold a live connection just to report a name."""
    if os.environ.get("GEMINI_API_KEY"):
        return GEMINI_MODEL
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC_MODEL
    raise QualificationError(
        "no LLM API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env "
        "(see .env.example) before running `csg qualify`."
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

async def _complete_with_retry(backend: _Backend, prompt: str) -> _Qualification:
    """Retry on rate-limit (429) and transient server (5xx) errors with backoff.
    Free-tier Gemini in particular enforces a low per-minute request cap and
    occasionally returns 503 under load -- both are expected transient
    conditions, not bugs, and must not be swallowed as silent permanent
    failures."""
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            return await backend.complete(prompt)
        except Exception as exc:
            last_exc = exc
            if not backend.is_retryable(exc) or attempt == MAX_RETRIES - 1:
                raise
            delay = BASE_RETRY_DELAY * (attempt + 1)
            print(f"[qualify] {type(exc).__name__}, retrying in {delay:.0f}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def pending_rows(
    conn: SupabaseConn, run_id: str, tiers: Optional[list[str]] = None, force: bool = False,
    domains: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Scored companies in `run_id` matching `tiers` that still need qualifying.

    `domains`, when given, restricts to exactly those domains (e.g. a
    top-N-by-score slice that may not align with tier boundaries) instead of
    every company in `tiers` -- still subject to the same already-qualified
    skip below.

    Without `force`, a domain already qualified for this run_id is excluded --
    this is what makes `csg qualify` cheap to re-run after partial failures
    (only the missing ones cost tokens/quota) and what the CLI's cost preview
    counts before asking for confirmation.
    """
    want = {t.upper() for t in (tiers or ["A", "B"])}
    rows = [r for r in db.get_scores(conn, run_id) if r["tier"] in want]
    if domains is not None:
        domain_set = set(domains)
        rows = [r for r in rows if r["domain"] in domain_set]
    if force:
        return rows
    already = set(db.get_qualifications(conn, run_id).keys())
    return [r for r in rows if r["domain"] not in already]


async def qualify_run(
    conn: SupabaseConn,
    run_id: str,
    tiers: Optional[list[str]] = None,
    limit_reqs: int = 8,
    force: bool = False,
    on_progress: Optional[Any] = None,
    domains: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Qualify scored companies in `run_id` whose tier is in `tiers` (default A/B).

    Pass `domains` to restrict to an exact set (e.g. top-N-by-score) rather
    than every company in `tiers`. Skips domains already qualified for this
    run_id unless `force` is set -- see `pending_rows`. Returns
    {"qualified", "skipped", "errors", "error_details"}.
    """
    backend = _select_backend()
    rows = pending_rows(conn, run_id, tiers, force=force, domains=domains)

    stats: dict[str, Any] = {"qualified": 0, "skipped": 0, "errors": 0, "error_details": {}}
    if not rows:
        return stats

    sem = asyncio.Semaphore(backend.max_concurrency)
    done = 0

    async def worker(row: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            postings = db.get_postings(conn, row["domain"])[:limit_reqs]
            postings_ctx = [
                {
                    "title": p.title,
                    "location": p.location,
                    "department": p.department,
                    "employment_type": p.employment_type,
                    "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                    "description": p.description[:1500],
                }
                for p in postings
            ]
            try:
                prompt = _render_prompt(row, postings_ctx)
                qual = await _complete_with_retry(backend, prompt)
                db.save_qualification(
                    conn, run_id, row["domain"],
                    pain_summary=qual.pain_summary,
                    recommended_angle=qual.recommended_angle,
                    model=backend.model,
                )
                conn.commit()
                stats["qualified"] += 1
            except Exception as exc:
                stats["errors"] += 1
                reason = classify_error(exc)
                stats["error_details"][row["domain"]] = f"[{reason}] {type(exc).__name__}: {exc}"
            done += 1
            if on_progress:
                on_progress(done, len(rows), row["domain"])

    await asyncio.gather(*(worker(r) for r in rows))
    await backend.close()
    return stats
