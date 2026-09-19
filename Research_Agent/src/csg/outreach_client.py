"""Thin HTTP client for the Outreach Agent's FastAPI -- the automatic
hand-off counterpart to Outreach's own research_client.py.

This is the ONLY connection Research makes to Outreach. Research never reads
Outreach's Supabase project or database directly -- only these HTTP routes,
the same ones the Outreach frontend itself calls:
  1. POST /import   -- pull a run's qualified Tier A/B prospects into
                        Outreach's local cache (a background job there;
                        polled here to completion via GET /jobs/{job_id}).
                        Flows Research -> Outreach after a run's companies
                        have been qualified + had contacts looked up (see
                        discover.py's _auto_qualify_and_contact).
  2. POST /campaigns -- draft a personalized campaign from the prospects
                        just imported (synchronous on Outreach's side).
  3. DELETE /research/runs/{run_id} -- cascade-delete whatever Outreach
                        imported from a run, called when Research deletes
                        that run (see db.delete_run) so Outreach never holds
                        prospects/campaigns for a run that no longer exists.

If OUTREACH_API_BASE is unset, or Outreach is unreachable, or any call
fails, the hand-off/cleanup is skipped -- this must never fail or block the
research operation itself, which already succeeded before this runs (see
discover.py and db.delete_run).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx


class OutreachClientError(RuntimeError):
    pass


class OutreachClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._client.post(path, json=json_body)
        except httpx.RequestError as exc:
            raise OutreachClientError(f"Outreach Agent API unreachable at {self.base}{path}: {exc}") from exc
        if r.status_code >= 400:
            raise OutreachClientError(f"POST {self.base}{path} -> {r.status_code}: {r.text}")
        return r.json()

    def _get(self, path: str) -> dict[str, Any]:
        try:
            r = self._client.get(path)
        except httpx.RequestError as exc:
            raise OutreachClientError(f"Outreach Agent API unreachable at {self.base}{path}: {exc}") from exc
        if r.status_code >= 400:
            raise OutreachClientError(f"GET {self.base}{path} -> {r.status_code}: {r.text}")
        return r.json()

    def _delete(self, path: str) -> dict[str, Any]:
        try:
            r = self._client.delete(path)
        except httpx.RequestError as exc:
            raise OutreachClientError(f"Outreach Agent API unreachable at {self.base}{path}: {exc}") from exc
        if r.status_code >= 400:
            raise OutreachClientError(f"DELETE {self.base}{path} -> {r.status_code}: {r.text}")
        return r.json()

    def import_prospects(
        self, run_id: str, *, tiers: list[str], poll_interval: float = 1.0, timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Start an import job on Outreach and block until it finishes.
        Returns the job's result dict (e.g. {"imported": N, "domains": [...]})."""
        started = self._post("/import", {"run_id": run_id, "tiers": tiers})
        job_id = started.get("job_id")
        if not job_id:
            raise OutreachClientError(f"POST /import did not return a job_id: {started}")

        deadline = time.monotonic() + timeout
        while True:
            snap = self._get(f"/jobs/{job_id}")
            status = snap.get("status")
            if status == "done":
                return snap.get("result") or {}
            if status == "error":
                raise OutreachClientError(f"Outreach import job failed: {snap.get('error')}")
            if time.monotonic() > deadline:
                raise OutreachClientError(f"Outreach import job {job_id} did not finish within {timeout}s")
            time.sleep(poll_interval)

    def create_campaign(
        self, *, name: Optional[str] = None, research_run_id: str, tiers: list[str], limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """Synchronous on Outreach's side -- drafts a personalized campaign
        from whatever prospects import_prospects() just cached. Returns
        {"campaign": {...}, "item_count": N}."""
        body: dict[str, Any] = {"research_run_id": research_run_id, "tiers": tiers}
        if name:
            body["name"] = name
        if limit is not None:
            body["limit"] = limit
        return self._post("/campaigns", body)

    def delete_research_run(self, run_id: str) -> dict[str, Any]:
        """Cascade-delete whatever Outreach imported for this run (campaigns,
        items, events, cached prospects). Safe to call even if this run was
        never handed off -- Outreach's endpoint is idempotent and just
        returns zero counts. Returns the counts dict Outreach reports."""
        return self._delete(f"/research/runs/{run_id}")
