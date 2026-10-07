"""Read-only Analytics API: `/analytics/v2/summary` and `/analytics/v2/prospects`.

Reads only the existing Outreach tables (outreach_items, conversation_messages,
followup_attempts, calendly_events, campaigns, prospects) -- no new tables, no
writes. The existing `/analytics` endpoint and the Learning/Optimization
endpoints are untouched; the aggregation rules live in analytics/metrics.py.

Fail-safe: a block that cannot be computed (or a secondary table that cannot be
read) comes back empty with an entry in `errors`, and the other blocks still
render. Only a failure to read outreach_items itself is an error response.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

from .. import db
from ..config import load_config
from . import metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics/v2", tags=["analytics"])

PAGE = 1000  # PostgREST caps a response (default 1000 rows); page explicitly.

_ITEM_COLS = (
    "item_id,campaign_id,research_domain,stage,to_email,sent_at,stage_updated_at,"
    "sender_email,followup_count,error,created_at"
)
_MSG_COLS = "message_id,item_id,direction,intent,from_email,received_at,sent_at,created_at"


def fetch_all(conn: Any, table: str, order: str, **params: Any) -> list[dict[str, Any]]:
    """Every row of `table`, paged so the PostgREST row cap cannot silently
    truncate the data. `order` must be a unique column for stable paging."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = conn.select(table, order=f"{order}.asc", limit=PAGE, offset=offset, **params)
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def _safe(errors: dict[str, str], name: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- isolate one block's failure
        logger.exception("analytics block %s failed", name)
        errors[name] = f"{type(exc).__name__}: {exc}"
        return default


def _configured_senders() -> list[str]:
    try:
        return [a.identity for a in load_config().accounts if a.identity]
    except Exception:  # noqa: BLE001 -- config problems must not break analytics
        return []


def _default_sender(configured: list[str]) -> Optional[str]:
    """The sole configured account, used for items with no recorded sender."""
    unique = {metrics.sender_key(c) for c in configured}
    return configured[0] if len(unique) == 1 else None


def _chunks(values: list[str], size: int = 100):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _prospects(conn: Any, domains: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(sorted(set(d for d in domains if d))):
        for p in db.prospects_by_domain(conn, chunk).values():
            out[p.research_domain] = {
                "name": p.name, "contact_name": p.contact_name, "contact_email": p.contact_email,
            }
    return out


def _load(conn: Any, campaign_id: Optional[str], errors: dict[str, str]) -> dict[str, Any]:
    item_params: dict[str, Any] = {"select": _ITEM_COLS}
    if campaign_id:
        item_params["campaign_id"] = f"eq.{campaign_id}"
    try:
        items = fetch_all(conn, "outreach_items", "item_id", **item_params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("analytics: could not read outreach_items")
        raise HTTPException(status_code=502, detail=f"could not read outreach items: {exc}") from exc
    return {
        "items": items,
        "messages": _safe(errors, "messages", lambda: fetch_all(conn, "conversation_messages", "message_id", select=_MSG_COLS), []),
        "attempts": _safe(errors, "followups", lambda: fetch_all(conn, "followup_attempts", "attempt_id"), []),
        "calendly": _safe(errors, "bookings", lambda: fetch_all(conn, "calendly_events", "event_uri"), []),
        "campaigns": _safe(
            errors, "campaigns",
            lambda: {c["campaign_id"]: c for c in fetch_all(conn, "campaigns", "campaign_id")}, {},
        ),
    }


@router.get("/summary")
def summary(
    sender: Optional[str] = None,
    campaign_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """Overall, per-account, per-campaign, follow-up and booking analytics for
    one filter set. `sender` is a sending address, or `unknown` for items sent
    before sender tracking. `campaign_id`/`sender` omitted = everything."""
    lo = metrics.parse_bound(date_from)
    hi = metrics.parse_bound(date_to, end=True)
    errors: dict[str, str] = {}
    conn = db.connect()
    try:
        # Load every campaign's items: the campaign table compares campaigns,
        # so it must not shrink to the one currently selected.
        configured = _configured_senders()
        data = _load(conn, None, errors)
        facts = metrics.build_facts(
            data["items"], data["messages"], data["attempts"], data["calendly"], _default_sender(configured),
        )
        campaigns = data["campaigns"]
        selected = metrics.select_facts(facts, sender=sender, campaign_id=campaign_id)
        sent, failed = metrics.cohort(selected, lo, hi)
        across_campaigns = metrics.select_facts(facts, sender=sender, campaign_id=None)

        domains = [f.item.get("research_domain") for f in facts.values() if f.booked]
        prospects = _safe(errors, "prospects", lambda: _prospects(conn, domains), {})

        return {
            "filters": {"sender": sender, "campaign_id": campaign_id, "date_from": date_from, "date_to": date_to},
            "senders": [
                {"sender": metrics.sender_key(c), "label": c, "configured": True} for c in configured
            ] + [
                {"sender": s, "label": (metrics.UNKNOWN_LABEL if s == metrics.UNKNOWN_SENDER else s), "configured": False}
                for s in sorted({f.sender for f in facts.values()} - {metrics.sender_key(c) for c in configured})
            ],
            "campaigns_available": [
                {"campaign_id": cid, "name": c.get("name") or cid} for cid, c in campaigns.items()
            ],
            "overall": _safe(errors, "overall", lambda: metrics.totals(sent, failed), {}),
            "trend": _safe(errors, "trend", lambda: metrics.trend(selected, lo, hi), []),
            # The account comparison deliberately ignores the sender filter --
            # it exists to compare accounts -- but honours campaign and dates.
            "accounts": _safe(
                errors, "accounts",
                lambda: metrics.accounts_block(facts, configured, campaign_id=campaign_id, lo=lo, hi=hi), [],
            ),
            "campaigns": _safe(errors, "campaigns_table", lambda: metrics.campaigns_block(across_campaigns, campaigns, lo, hi), []),
            "followups": _safe(errors, "followups_block", lambda: metrics.followups_block(sent), {"rows": [], "note": ""}),
            "bookings": _safe(
                errors, "bookings_block",
                lambda: metrics.bookings_block(
                    facts, data["calendly"], campaigns, prospects,
                    sender=sender, campaign_id=campaign_id, lo=lo, hi=hi,
                ),
                {"rows": [], "total": 0, "active": 0, "canceled": 0, "unmatched": 0, "trend": []},
            ),
            "errors": errors,
        }
    finally:
        conn.close()


@router.get("/prospects")
def prospects_list(
    sender: Optional[str] = None,
    campaign_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Prospect-level drill-down rows for the filters (sender -> campaign ->
    prospect). Open one in the dashboard with the existing
    `GET /items/{item_id}/conversation`."""
    lo = metrics.parse_bound(date_from)
    hi = metrics.parse_bound(date_to, end=True)
    errors: dict[str, str] = {}
    conn = db.connect()
    try:
        data = _load(conn, campaign_id, errors)
        facts = metrics.build_facts(
            data["items"], data["messages"], data["attempts"], data["calendly"],
            _default_sender(_configured_senders()),
        )
        selected = metrics.select_facts(facts, sender=sender, campaign_id=campaign_id)
        # Only the page being returned needs its prospect names looked up.
        probe = metrics.prospect_rows(selected, data["campaigns"], {}, lo, hi)
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        page = probe[offset:offset + limit]
        domains = [facts[r["item_id"]].item.get("research_domain") for r in page]
        prospects = _safe(errors, "prospects", lambda: _prospects(conn, domains), {})
        rows = metrics.prospect_rows(
            [facts[r["item_id"]] for r in page], data["campaigns"], prospects, lo, hi,
        )
        return {"total": len(probe), "limit": limit, "offset": offset, "rows": rows, "errors": errors}
    finally:
        conn.close()
