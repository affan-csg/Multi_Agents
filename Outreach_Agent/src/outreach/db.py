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
from .models import (
    AutomationState,
    Campaign,
    ConversationMessage,
    ConversationSettings,
    FollowupAttempt,
    ImportedProspect,
    OutreachEvent,
    OutreachItem,
)

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

    def update_returning(self, table: str, data: dict[str, Any], **filters: Any) -> list[dict[str, Any]]:
        """PATCH that reports which rows it actually matched.

        `update` discards that, which is fine for unconditional writes but
        useless for a conditional one: the Conversation Agent's advisory lock
        works by PATCHing a row only while `conversation_locked_at=is.null`,
        and needs to know whether it won that race or another tick got there
        first. An empty list means the filter matched nothing -- i.e. the
        lock was already held."""
        r = self._check(self._client.patch(
            f"/{table}", params=filters,
            headers={"Prefer": "return=representation"}, json=data,
        ))
        return r.json() if r.content else []

    def delete(self, table: str, **filters: Any) -> None:
        self._check(self._client.delete(f"/{table}", params=filters))


def connect(url: Optional[str] = None, service_key: Optional[str] = None) -> SupabaseConn:
    url = url or os.environ.get("OUTREACH_SUPABASE_URL")
    service_key = service_key or os.environ.get("OUTREACH_SUPABASE_SERVICE_KEY")
    if not url or not service_key:
        raise SupabaseError(
            "OUTREACH_SUPABASE_URL and OUTREACH_SUPABASE_SERVICE_KEY must be set in .env (see .env.example). "
            "This must point at the Outreach project, not the Research Agent's."
        )
    return SupabaseConn(url, service_key)


# --- prospects (cached snapshot from the Research Agent) -------------------

def upsert_prospect(conn: SupabaseConn, p: ImportedProspect) -> None:
    """Cache (or refresh) one prospect snapshot.

    `research_run_id` is still overwritten to `p.research_run_id` every call
    -- it means "the run this snapshot's data currently reflects", and a
    re-import is supposed to refresh score/tier/contact/etc from whatever run
    triggered it. But `research_run_ids` (the set of every run that has ever
    imported this domain) is MERGED, not overwritten: we read the existing
    row first and append the new run_id if it isn't already recorded, so a
    prospect re-imported under a fresh run_id (which is the ONLY kind of
    rerun the Research Agent supports -- see discover.run_discovery, it has
    no "resume this exact run" path) doesn't lose its membership in the
    run(s) that originally surfaced it. That membership list is what
    list_prospects(run_id=...) filters on, which is what keeps a prospect
    visible on the Prospects page under a run someone previously imported it
    from, even after a later rerun refreshed its data under a new run_id.
    """
    existing = get_prospect(conn, p.research_domain)
    run_ids = list(existing.research_run_ids) if existing else []
    if p.research_run_id and p.research_run_id not in run_ids:
        run_ids.append(p.research_run_id)
    p = p.model_copy(update={"research_run_ids": run_ids})
    _write_prospect(conn, p)


def _write_prospect(conn: SupabaseConn, p: ImportedProspect) -> None:
    """The actual upsert, with no run_ids merging -- upsert_prospect's
    "append the new run, keep every old one" rule is right for a normal
    import, but delete_by_research_run needs to REMOVE a run from the
    membership list (a run being deleted must actually shrink it), which
    upsert_prospect's merge-only logic can't do since it always unions with
    whatever is already stored. Kept private: every other caller should go
    through upsert_prospect so the merge rule is never accidentally
    bypassed."""
    row = p.model_dump(mode="json")
    row["imported_at"] = _iso(p.imported_at or utcnow())
    conn.upsert("prospects", [row], on_conflict="research_domain")


def get_prospect(conn: SupabaseConn, domain: str) -> Optional[ImportedProspect]:
    rows = conn.select("prospects", research_domain=f"eq.{domain}", limit=1)
    return ImportedProspect(**rows[0]) if rows else None


def list_prospects(conn: SupabaseConn, run_id: Optional[str] = None) -> list[ImportedProspect]:
    """All cached prospects, or only those imported at some point from
    `run_id`. Filters on membership in `research_run_ids` (PostgREST's `cs.`
    "contains" operator for a JSONB array column), NOT equality against the
    single `research_run_id` column -- a prospect re-imported under a newer
    run must still show up when a person is viewing an older run that also
    imported it. See upsert_prospect's docstring for why the two fields
    diverge."""
    params: dict[str, Any] = {"order": "score.desc.nullslast"}
    if run_id:
        params["research_run_ids"] = f'cs.["{run_id}"]'
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
    campaigns (all scoped to this run_id), then prospects touched by this
    run_id. A prospect that was ALSO imported from some other still-live run
    (research_run_ids has more than this one entry -- see upsert_prospect) is
    not deleted at all: this run_id is just dropped from its membership list,
    and `research_run_id` (the "current snapshot" pointer) is moved to
    whatever other run remains if it was this one. Only a prospect whose
    *sole* recorded run is the one being deleted is actually removed, same
    as the original single-run behavior this replaces. Returns counts for
    the caller's response/logging -- `prospects_removed` only counts hard
    deletes, not the ones merely un-linked from this run."""
    campaigns = [c for c in list_campaigns(conn) if c.research_run_id == run_id]
    items_removed = 0
    for campaign in campaigns:
        items = list_items(conn, campaign_id=campaign.campaign_id)
        for item in items:
            conn.delete("outreach_events", item_id=f"eq.{item.item_id}")
        conn.delete("outreach_items", campaign_id=f"eq.{campaign.campaign_id}")
        items_removed += len(items)
    conn.delete("campaigns", research_run_id=f"eq.{run_id}")

    touched = list_prospects(conn, run_id=run_id)
    removed = 0
    for p in touched:
        remaining = [r for r in p.research_run_ids if r != run_id]
        if not remaining:
            # This was the only run that ever surfaced this prospect --
            # nothing left to refresh it from, so it's a true orphan.
            conn.delete("prospects", research_domain=f"eq.{p.research_domain}")
            removed += 1
        else:
            # Still imported from at least one other live run -- keep the
            # row, just drop this run from its membership and repoint the
            # "current snapshot" run if that's the one being deleted.
            new_current = p.research_run_id if p.research_run_id != run_id else remaining[-1]
            _write_prospect(conn, p.model_copy(update={
                "research_run_id": new_current, "research_run_ids": remaining,
            }))

    return {
        "campaigns_removed": len(campaigns),
        "items_removed": items_removed,
        "prospects_removed": removed,
    }


# --- outreach items (the pipeline) ------------------------------------------

def create_item(conn: SupabaseConn, item: OutreachItem) -> None:
    row = item.model_dump(mode="json")
    if not row.get("sender_email"):
        # Omitted rather than sent as null so campaign creation keeps working
        # on a database where the sender_email migration is not applied yet.
        row.pop("sender_email", None)
    now = _iso(utcnow())
    row["created_at"] = row["created_at"] or now
    row["stage_updated_at"] = row["stage_updated_at"] or now
    conn.upsert("outreach_items", [row], on_conflict="campaign_id,research_domain")


def domains_with_existing_items(conn: SupabaseConn, domains: list[str]) -> set[str]:
    """Of `domains`, those that already have an outreach item in any campaign
    other than a failed one. Used so a company already contacted (or queued,
    or declined) is never given a second campaign item."""
    if not domains:
        return set()
    rows = conn.select("outreach_items", research_domain=f"in.({','.join(domains)})",
                       select="research_domain,stage")
    return {r["research_domain"] for r in rows if r.get("stage") != "failed"}


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


def mark_sent(
    conn: SupabaseConn, item_id: str, provider_message_id: Optional[str] = None,
    sender_email: Optional[str] = None,
) -> None:
    now = utcnow()
    fields: dict[str, Any] = {
        "sent_at": _iso(now), "provider_message_id": provider_message_id, "error": None,
    }
    if sender_email:
        # The mailbox that actually sent this email; follow-ups and replies
        # for the item continue from it.
        fields["sender_email"] = sender_email
    conn.update("outreach_items", fields, item_id=f"eq.{item_id}")
    set_stage(conn, item_id, "sent", actor="system", detail={"provider_message_id": provider_message_id})


def mark_failed(conn: SupabaseConn, item_id: str, error: str, sender_email: Optional[str] = None) -> None:
    fields: dict[str, Any] = {"error": error}
    if sender_email:
        # A failed send is still attributable to the mailbox that attempted it.
        fields["sender_email"] = sender_email
    conn.update("outreach_items", fields, item_id=f"eq.{item_id}")
    set_stage(conn, item_id, "failed", actor="system", detail={"error": error})


# --- events (audit trail) ---------------------------------------------------

def add_event(conn: SupabaseConn, e: OutreachEvent) -> None:
    row = e.model_dump(mode="json")
    row["created_at"] = _iso(e.created_at or utcnow())
    conn.insert("outreach_events", [row])


def list_events(conn: SupabaseConn, item_id: str) -> list[OutreachEvent]:
    rows = conn.select("outreach_events", item_id=f"eq.{item_id}", order="created_at.asc")
    return [OutreachEvent(**r) for r in rows]


# --- conversation settings (the per-campaign ON/OFF toggle) ------------------

def get_conversation_settings(conn: SupabaseConn, campaign_id: str) -> Optional[ConversationSettings]:
    """The campaign's settings row, or None if it was never configured.

    None is meaningful, not an error: the caller (conversation.settings
    .get_settings) turns it into a disabled default. The Conversation Agent
    is opt-in per campaign, so "no row" and "enabled=false" must behave
    identically."""
    rows = conn.select("conversation_settings", campaign_id=f"eq.{campaign_id}", limit=1)
    return ConversationSettings(**rows[0]) if rows else None


def upsert_conversation_settings(conn: SupabaseConn, s: ConversationSettings) -> None:
    row = s.model_dump(mode="json")
    row["updated_at"] = _iso(s.updated_at or utcnow())
    conn.upsert("conversation_settings", [row], on_conflict="campaign_id")


def list_enabled_conversation_campaigns(conn: SupabaseConn) -> list[str]:
    """Campaign ids with the Conversation Agent switched on. Used by the
    all-campaigns tick so it never even loads items belonging to a campaign
    that has the agent off."""
    rows = conn.select("conversation_settings", enabled="eq.true")
    return [r["campaign_id"] for r in rows]


# --- conversation messages (the thread; append-only) -------------------------

def add_conversation_message(conn: SupabaseConn, m: ConversationMessage) -> bool:
    """Append one message to an item's thread.

    Returns True if it was stored, False if an identical one was already
    there. Deduplication is by (item_id, provider_message_id) -- the UNIQUE
    constraint in schema.sql -- which is what makes reply ingestion
    idempotent: the same email read off IMAP on ten consecutive ticks
    produces one row, gets classified once, and moves the card once.

    The check is done as a read-then-insert rather than relying on the
    constraint to raise, so that a duplicate is an ordinary, expected
    outcome the engine can count, not an exception it has to catch and
    interpret."""
    if m.provider_message_id:
        existing = conn.select(
            "conversation_messages",
            item_id=f"eq.{m.item_id}",
            provider_message_id=f"eq.{m.provider_message_id}",
            limit=1,
        )
        if existing:
            return False

    row = m.model_dump(mode="json")
    row["created_at"] = _iso(m.created_at or utcnow())
    conn.insert("conversation_messages", [row])
    return True


def list_conversation_messages(conn: SupabaseConn, item_id: str) -> list[ConversationMessage]:
    rows = conn.select("conversation_messages", item_id=f"eq.{item_id}", order="created_at.asc")
    return [ConversationMessage(**r) for r in rows]


def get_conversation_message(conn: SupabaseConn, message_id: str) -> Optional[ConversationMessage]:
    rows = conn.select("conversation_messages", message_id=f"eq.{message_id}", limit=1)
    return ConversationMessage(**rows[0]) if rows else None


def update_conversation_message(conn: SupabaseConn, message_id: str, **fields: Any) -> None:
    conn.update("conversation_messages", fields, message_id=f"eq.{message_id}")


def list_pending_drafts(conn: SupabaseConn, item_id: str) -> list[ConversationMessage]:
    """Replies the agent composed but did not send (auto_reply off), waiting
    for a person to approve them."""
    rows = conn.select(
        "conversation_messages",
        item_id=f"eq.{item_id}", direction="eq.outbound", draft_pending="eq.true",
        order="created_at.asc",
    )
    return [ConversationMessage(**r) for r in rows]


# --- conversation item locking ----------------------------------------------

def claim_conversation_lock(conn: SupabaseConn, item_id: str, *, stale_before: datetime) -> bool:
    """Try to take the advisory lock on an item. True if this caller won it.

    Two ticks running at once (a manual "Check replies" click landing on top
    of a CLI tick, say) must not both process the same item -- that is how
    you get two follow-ups sent for one silence. The claim is a single
    conditional PATCH: it only matches rows whose lock is null OR older than
    `stale_before`, so exactly one concurrent caller can succeed, and a lock
    stranded by a crashed tick becomes reclaimable rather than permanent.

    PostgREST has no OR across two filters on one column, so the two cases
    are attempted as two statements -- both are conditional, so neither can
    steal a live lock."""
    now = _iso(utcnow())

    won = conn.update_returning(
        "outreach_items", {"conversation_locked_at": now},
        item_id=f"eq.{item_id}", conversation_locked_at="is.null",
    )
    if won:
        return True

    won = conn.update_returning(
        "outreach_items", {"conversation_locked_at": now},
        item_id=f"eq.{item_id}", conversation_locked_at=f"lt.{_iso(stale_before)}",
    )
    return bool(won)


def release_conversation_lock(conn: SupabaseConn, item_id: str) -> None:
    conn.update("outreach_items", {"conversation_locked_at": None}, item_id=f"eq.{item_id}")


def list_conversation_items(conn: SupabaseConn, campaign_id: str, stages: list[str]) -> list[OutreachItem]:
    """Items in a campaign that belong to the Conversation Agent.

    `stages` is always models.CONVERSATION_STAGES -- the filter is applied in
    the query itself, so items in pending/needs_review/failed are never
    loaded at all. That is the structural guarantee that this agent cannot
    touch work the Outreach Agent still owns."""
    rows = conn.select(
        "outreach_items",
        campaign_id=f"eq.{campaign_id}",
        stage=f"in.({','.join(stages)})",
        order="created_at.desc",
    )
    return [OutreachItem(**r) for r in rows]


# --- follow-up idempotency ledger ---------------------------------------------

def claim_followup_attempt(conn: SupabaseConn, item_id: str, number: int) -> Optional[FollowupAttempt]:
    """Claim "send follow-up `number` for this item". Returns the new ledger
    row if this caller won, None if the number was already claimed.

    The UNIQUE (item_id, followup_number) constraint is the guarantee: it is
    enforced by Postgres, so overlapping ticks, a restart mid-send, or a second
    replica cannot both win. A read-first check keeps the common case cheap and
    a constraint violation on the insert is treated the same as losing."""
    existing = conn.select(
        "followup_attempts", item_id=f"eq.{item_id}", followup_number=f"eq.{number}", limit=1,
    )
    if existing:
        return None
    now = utcnow()
    row = FollowupAttempt(
        attempt_id=f"fu-{item_id}-{number}", item_id=item_id, followup_number=number,
        status="claimed", claimed_at=now, updated_at=now,
    )
    try:
        conn.insert("followup_attempts", [row.model_dump(mode="json")])
    except SupabaseError as e:
        if "23505" in str(e) or "duplicate key" in str(e).lower() or " 409" in str(e):
            return None
        raise
    return row


def update_followup_attempt(conn: SupabaseConn, item_id: str, number: int, **fields: Any) -> None:
    fields["updated_at"] = _iso(utcnow())
    conn.update(
        "followup_attempts", fields,
        item_id=f"eq.{item_id}", followup_number=f"eq.{number}",
    )


def get_followup_attempt(conn: SupabaseConn, item_id: str, number: int) -> Optional[FollowupAttempt]:
    rows = conn.select(
        "followup_attempts", item_id=f"eq.{item_id}", followup_number=f"eq.{number}", limit=1,
    )
    return FollowupAttempt(**rows[0]) if rows else None


def reclaim_failed_followup(conn: SupabaseConn, item_id: str, number: int) -> bool:
    """Take a `failed` attempt back to `claimed` for a retry. A conditional
    PATCH so two ticks cannot both retry the same failure."""
    won = conn.update_returning(
        "followup_attempts", {"status": "claimed", "updated_at": _iso(utcnow())},
        item_id=f"eq.{item_id}", followup_number=f"eq.{number}", status="eq.failed",
    )
    return bool(won)


def list_followup_attempts(conn: SupabaseConn, item_id: str) -> list[FollowupAttempt]:
    rows = conn.select("followup_attempts", item_id=f"eq.{item_id}", order="followup_number.asc")
    return [FollowupAttempt(**r) for r in rows]


# --- automation (scheduler) state and lease -------------------------------------

def get_automation_state(conn: SupabaseConn, job: str = "sync") -> AutomationState:
    """The job's state row; a default (never run) when none exists yet."""
    rows = conn.select("automation_state", job=f"eq.{job}", limit=1)
    return AutomationState(**rows[0]) if rows else AutomationState(job=job)


def update_automation_state(conn: SupabaseConn, job: str, **fields: Any) -> None:
    """Merge `fields` into the job's row, creating it if needed."""
    current = conn.select("automation_state", job=f"eq.{job}", limit=1)
    fields = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in fields.items()}
    fields["updated_at"] = _iso(utcnow())
    if current:
        conn.update("automation_state", fields, job=f"eq.{job}")
    else:
        row = AutomationState(job=job).model_dump(mode="json")
        row.update(fields)
        conn.upsert("automation_state", [row], on_conflict="job")


def claim_automation_lease(
    conn: SupabaseConn, job: str, owner: str, *, now: datetime, until: datetime,
) -> bool:
    """Take the scheduler lease. True if this caller won it.

    Same shape as claim_conversation_lock: conditional PATCHes, so exactly one
    concurrent caller (another replica, a CLI run, the API button) can win, and
    a lease stranded by a crash expires on its own at `until`."""
    if not conn.select("automation_state", job=f"eq.{job}", limit=1):
        update_automation_state(conn, job)  # ensure the row exists
    values = {"locked_by": owner, "locked_until": _iso(until), "last_started_at": _iso(now)}
    for cond in ({"locked_until": "is.null"}, {"locked_until": f"lt.{_iso(now)}"}):
        if conn.update_returning("automation_state", values, job=f"eq.{job}", **cond):
            return True
    return False


def renew_automation_lease(
    conn: SupabaseConn, job: str, owner: str, *, until: datetime,
) -> bool:
    return bool(conn.update_returning(
        "automation_state", {"locked_until": _iso(until)},
        job=f"eq.{job}", locked_by=f"eq.{owner}",
    ))


def release_automation_lease(conn: SupabaseConn, job: str, owner: str) -> None:
    conn.update(
        "automation_state", {"locked_by": None, "locked_until": None},
        job=f"eq.{job}", locked_by=f"eq.{owner}",
    )
