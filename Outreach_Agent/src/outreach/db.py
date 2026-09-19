"""Supabase (Postgres via PostgREST) persistence for the Outreach Agent.

A SEPARATE Supabase project from the Research Agent's -- see schema.sql. This
module never talks to the Research Agent's database; the only connection to
Research is over HTTP, in research_client.py. Repository functions only, no
business logic, same convention as Research_Agent/src/csg/db.py.

`connect()` returns a `SupabaseConn` -- a thin httpx wrapper, not a real DB
connection. `commit()`/`close()` are kept for interface parity with the
Research Agent's db.py even though PostgREST has no client-side transaction
concept.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from .config import PROJECT_ROOT
from .models import Campaign, ImportedProspect, OutreachEvent, OutreachItem

load_dotenv(PROJECT_ROOT / ".env")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


class SupabaseError(RuntimeError):
    pass


class SupabaseConn:
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
        pass

    def close(self) -> None:
        self._client.close()

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

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        r = self._check(self._client.post(
            f"/{table}", headers={"Prefer": "return=representation"}, json=rows,
        ))
        return r.json()

    def update(self, table: str, data: dict[str, Any], **filters: Any) -> None:
        self._check(self._client.patch(f"/{table}", params=filters, json=data))

    def delete(self, table: str, **filters: Any) -> None:
        self._check(self._client.delete(f"/{table}", params=filters))


def connect(url: Optional[str] = None, service_key: Optional[str] = None) -> SupabaseConn:
    url = url or os.environ.get("SUPABASE_URL")
    service_key = service_key or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not service_key:
        raise SupabaseError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env (see .env.example). "
            "This must point at the Outreach project, not the Research Agent's."
        )
    return SupabaseConn(url, service_key)


# --- prospects (cached snapshot from the Research Agent) -------------------

def upsert_prospect(conn: SupabaseConn, p: ImportedProspect) -> None:
    row = p.model_dump(mode="json")
    row["imported_at"] = _iso(p.imported_at or utcnow())
    conn.upsert("prospects", [row], on_conflict="research_domain")


def get_prospect(conn: SupabaseConn, domain: str) -> Optional[ImportedProspect]:
    rows = conn.select("prospects", research_domain=f"eq.{domain}", limit=1)
    return ImportedProspect(**rows[0]) if rows else None


def list_prospects(conn: SupabaseConn, run_id: Optional[str] = None) -> list[ImportedProspect]:
    params: dict[str, Any] = {"order": "score.desc.nullslast"}
    if run_id:
        params["research_run_id"] = f"eq.{run_id}"
    rows = conn.select("prospects", **params)
    return [ImportedProspect(**r) for r in rows]


def prospects_by_domain(conn: SupabaseConn, domains: list[str]) -> dict[str, ImportedProspect]:
    if not domains:
        return {}
    rows = conn.select("prospects", research_domain=f"in.({','.join(domains)})")
    return {r["research_domain"]: ImportedProspect(**r) for r in rows}


# --- campaigns ---------------------------------------------------------------

def create_campaign(conn: SupabaseConn, c: Campaign) -> None:
    row = c.model_dump(mode="json")
    row["created_at"] = _iso(c.created_at or utcnow())
    conn.upsert("campaigns", [row], on_conflict="campaign_id")


def get_campaign(conn: SupabaseConn, campaign_id: str) -> Optional[Campaign]:
    rows = conn.select("campaigns", campaign_id=f"eq.{campaign_id}", limit=1)
    return Campaign(**rows[0]) if rows else None


def list_campaigns(conn: SupabaseConn) -> list[Campaign]:
    rows = conn.select("campaigns", order="created_at.desc")
    return [Campaign(**r) for r in rows]


def rename_campaign(conn: SupabaseConn, campaign_id: str, name: str) -> None:
    conn.update("campaigns", {"name": name}, campaign_id=f"eq.{campaign_id}")


def delete_campaign(conn: SupabaseConn, campaign_id: str) -> None:
    """Delete a campaign and everything under it (items + their audit
    events). Cached `prospects` rows are untouched -- they belong to no
    campaign and can be reused by a future one."""
    items = list_items(conn, campaign_id=campaign_id)
    for item in items:
        conn.delete("outreach_events", item_id=f"eq.{item.item_id}")
    conn.delete("outreach_items", campaign_id=f"eq.{campaign_id}")
    conn.delete("campaigns", campaign_id=f"eq.{campaign_id}")


def delete_by_research_run(conn: SupabaseConn, run_id: str) -> dict[str, int]:
    """Cascade-delete everything Outreach imported from a Research Agent run
    that is being deleted there -- unlike delete_campaign, this also removes
    the cached `prospects` rows themselves (not just the campaign built from
    them), since a prospect with no source run left has nothing to refresh
    from and would otherwise linger forever as an orphan.

    Deletes, in dependency order: outreach_events -> outreach_items ->
    campaigns (all scoped to this run_id), then prospects scoped to this
    run_id. Returns counts for the caller's response/logging."""
    campaigns = [c for c in list_campaigns(conn) if c.research_run_id == run_id]
    items_removed = 0
    for campaign in campaigns:
        items = list_items(conn, campaign_id=campaign.campaign_id)
        for item in items:
            conn.delete("outreach_events", item_id=f"eq.{item.item_id}")
        conn.delete("outreach_items", campaign_id=f"eq.{campaign.campaign_id}")
        items_removed += len(items)
    conn.delete("campaigns", research_run_id=f"eq.{run_id}")

    prospects = list_prospects(conn, run_id=run_id)
    if prospects:
        conn.delete("prospects", research_run_id=f"eq.{run_id}")

    return {
        "campaigns_removed": len(campaigns),
        "items_removed": items_removed,
        "prospects_removed": len(prospects),
    }


# --- outreach items (the pipeline) ------------------------------------------

def create_item(conn: SupabaseConn, item: OutreachItem) -> None:
    row = item.model_dump(mode="json")
    now = _iso(utcnow())
    row["created_at"] = row["created_at"] or now
    row["stage_updated_at"] = row["stage_updated_at"] or now
    conn.upsert("outreach_items", [row], on_conflict="campaign_id,research_domain")


def get_item(conn: SupabaseConn, item_id: str) -> Optional[OutreachItem]:
    rows = conn.select("outreach_items", item_id=f"eq.{item_id}", limit=1)
    return OutreachItem(**rows[0]) if rows else None


def list_items(conn: SupabaseConn, campaign_id: Optional[str] = None, stage: Optional[str] = None) -> list[OutreachItem]:
    params: dict[str, Any] = {"order": "created_at.desc"}
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    if stage:
        params["stage"] = f"eq.{stage}"
    rows = conn.select("outreach_items", **params)
    return [OutreachItem(**r) for r in rows]


def update_item_fields(conn: SupabaseConn, item_id: str, **fields: Any) -> None:
    conn.update("outreach_items", fields, item_id=f"eq.{item_id}")


def schedule_campaign(conn: SupabaseConn, campaign_id: str, scheduled_at: Optional[str]) -> int:
    """Set (or clear) `scheduled_at` on every pending item in a campaign at
    once -- this is what the kanban's "Schedule campaign" kebab action calls.
    Only pending items are touched; sent/failed/etc are left alone. Returns
    how many items were updated. Still informational only -- nothing polls
    this and auto-sends, `campaign send` still has to be clicked."""
    conn.update("outreach_items", {"scheduled_at": scheduled_at},
                campaign_id=f"eq.{campaign_id}", stage="eq.pending")
    return len(list_items(conn, campaign_id=campaign_id, stage="pending"))


def delete_item(conn: SupabaseConn, item_id: str) -> None:
    """Remove a single prospect from a campaign (its audit events too). The
    campaign and the cached prospect row are untouched."""
    conn.delete("outreach_events", item_id=f"eq.{item_id}")
    conn.delete("outreach_items", item_id=f"eq.{item_id}")


def set_stage(conn: SupabaseConn, item_id: str, stage: str, *, actor: str = "system",
              detail: Optional[dict[str, Any]] = None) -> None:
    """Move an item to a new stage and record the transition. The only
    write path for `stage` -- callers should never PATCH outreach_items.stage
    directly, so every transition is guaranteed an audit row."""
    item = get_item(conn, item_id)
    if item is None:
        raise SupabaseError(f"outreach item {item_id} not found")
    now = utcnow()
    conn.update("outreach_items", {"stage": stage, "stage_updated_at": _iso(now)}, item_id=f"eq.{item_id}")
    add_event(conn, OutreachEvent(
        event_id=f"{item_id}-{int(now.timestamp() * 1000)}",
        item_id=item_id, event_type="stage_change",
        from_stage=item.stage, to_stage=stage, actor=actor, detail=detail or {},
    ))


def mark_sent(conn: SupabaseConn, item_id: str, provider_message_id: Optional[str] = None) -> None:
    now = utcnow()
    conn.update("outreach_items", {
        "sent_at": _iso(now), "provider_message_id": provider_message_id, "error": None,
    }, item_id=f"eq.{item_id}")
    set_stage(conn, item_id, "sent", actor="system", detail={"provider_message_id": provider_message_id})


def mark_failed(conn: SupabaseConn, item_id: str, error: str) -> None:
    conn.update("outreach_items", {"error": error}, item_id=f"eq.{item_id}")
    set_stage(conn, item_id, "failed", actor="system", detail={"error": error})


# --- events (audit trail) ---------------------------------------------------

def add_event(conn: SupabaseConn, e: OutreachEvent) -> None:
    row = e.model_dump(mode="json")
    row["created_at"] = _iso(e.created_at or utcnow())
    conn.insert("outreach_events", [row])


def list_events(conn: SupabaseConn, item_id: str) -> list[OutreachEvent]:
    rows = conn.select("outreach_events", item_id=f"eq.{item_id}", order="created_at.asc")
    return [OutreachEvent(**r) for r in rows]
