"""Thin HTTP client for the Research Agent's FastAPI.

This is the ONLY connection between the two agents. Outreach never reads the
Research Agent's Supabase project directly -- everything comes through this
client, hitting the same routes the Research frontend uses
(GET /runs/{run_id}/companies, GET /runs/{run_id}/companies/{domain}), and is
cached locally into Outreach's own `prospects` table via db.upsert_prospect.

If the Research API is down, importing simply fails loudly (RequestError) --
there is no local fallback DB read, by design: Outreach must not guess at
Research's data.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import Config


class ResearchClientError(RuntimeError):
    pass


class ResearchClient:
    def __init__(self, cfg: Config) -> None:
        self.base = cfg.research_api_base.rstrip("/")
        self._client = httpx.Client(base_url=self.base, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.RequestError as exc:
            raise ResearchClientError(f"Research Agent API unreachable at {self.base}{path}: {exc}") from exc
        if r.status_code >= 400:
            raise ResearchClientError(f"GET {self.base}{path} -> {r.status_code}: {r.text}")
        return r.json()

    def list_runs(self) -> list[dict[str, Any]]:
        return self._get("/runs")

    def companies(
        self, run_id: str, *, tier: Optional[str] = None, min_score: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Scored companies for a run (or 'all'). Mirrors the frontend's
        Companies page query -- see Research_Agent/src/csg/api.py run_companies()."""
        return self._get(f"/runs/{run_id}/companies", tier=tier, min_score=min_score)

    def company_detail(self, run_id: str, domain: str) -> dict[str, Any]:
        """Full company detail: firmographics, postings, qualification
        (pain_summary/recommended_angle), contacts, and the outreach_ready /
        has_linkedin_contact flags -- see Research_Agent/src/csg/api.py
        company_detail()."""
        return self._get(f"/runs/{run_id}/companies/{domain}")

    def qualified_prospects(
        self, run_id: str, *, tiers: list[str], min_score: Optional[float] = None,
        require_contact_email: bool = True, limit: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Top prospects ready for outreach: scored companies in the wanted
        tiers, enriched with full detail (contacts, pain_summary), filtered to
        those with at least one contact email when require_contact_email is
        set. Ranking is Research's own score, descending -- Outreach never
        re-scores.

        Returns `(prospects, excluded)` -- `excluded` is every tier-matching
        company dropped for lacking a contact email, as `{"domain", "name",
        "tier"}`, so the caller can tell the user exactly who was left out and
        why, instead of the exclusion happening silently.
        """
        rows = self.companies(run_id, min_score=min_score)
        rows = [r for r in rows if r.get("tier") in tiers]
        rows.sort(key=lambda r: r.get("score", 0), reverse=True)
        if limit:
            rows = rows[: limit * 2]  # over-fetch since some may lack a contact; trimmed below

        out: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for r in rows:
            detail = self.company_detail(run_id, r["domain"])
            if require_contact_email and not detail.get("outreach_ready"):
                company = detail.get("company") or {}
                excluded.append({
                    "domain": r["domain"],
                    "name": company.get("name") or r["domain"],
                    "tier": r.get("tier"),
                })
                continue
            out.append(detail)
            if limit and len(out) >= limit:
                break
        return out, excluded
