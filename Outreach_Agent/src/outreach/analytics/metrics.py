"""Pure aggregation for the Analytics dashboard: raw rows in, JSON-ready dicts
out. No database access here, so every rule is unit-testable.

Definitions (one place, so the numbers on every tab agree):

* Sender -- `outreach_items.sender_email`, the mailbox that actually sent the
  first email (stamped at send time). An item with no recorded sender (sent
  before sender tracking, or not sent yet) is attributed to the sole
  configured account when exactly one is configured -- every email can then
  only have come from it. With several configured accounts it stays
  "Unknown / legacy" rather than being guessed.
* Cohort -- the items whose first email was sent inside the date range (and
  that match the sender/campaign filters). Counts and rates in the summary
  tables describe that cohort, so a rate can never exceed 100%.
* Reply -- an inbound message classified as a human reply (models.
  HUMAN_REPLY_INTENTS), counted once per item. Positive reply -- intent
  `interested` or `booked` (same definition as the Learning module).
* Booking -- an item with an active Calendly booking, or in the `booked` stage
  (the classifier's email-confirmation path).
* Follow-up sent -- a followup_attempts row with status `sent`.
* Failed -- an item in the `failed` stage (the send raised an error), dated by
  its last stage change. Bounced -- an inbound `bounce` message (detected from
  the bounce notice only; there is no delivery-status tracking).
* Trends are by event date (the day each send/reply/booking happened), so they
  can include events from items sent before the range.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..models import HUMAN_REPLY_INTENTS

POSITIVE_INTENTS = {"interested", "booked"}
UNKNOWN_SENDER = "unknown"
UNKNOWN_LABEL = "Unknown / legacy"
# Below this many sent emails a rate is flagged `low_sample` so the UI can
# de-emphasise it rather than present noise as signal.
MIN_SAMPLE = 10


def parse_dt(value: Any) -> Optional[datetime]:
    """Any timestamp (ISO string with or without tz, or datetime) -> aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_bound(value: Optional[str], *, end: bool = False) -> Optional[datetime]:
    """A `date_from` / `date_to` query value. A bare date is a whole day:
    start-of-day for `date_from`, end-of-day for `date_to` (inclusive)."""
    if not value:
        return None
    if len(value) <= 10:
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return None
        base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return base + timedelta(days=1) - timedelta(microseconds=1) if end else base
    return parse_dt(value)


def rate(n: int, d: int) -> dict[str, Any]:
    return {"n": n, "d": d, "pct": round(100.0 * n / d, 1) if d else None, "low_sample": d < MIN_SAMPLE}


def sender_key(sender: Optional[str]) -> str:
    return (sender or "").strip().lower() or UNKNOWN_SENDER


def _in_range(dt: Optional[datetime], lo: Optional[datetime], hi: Optional[datetime]) -> bool:
    if dt is None:
        return lo is None and hi is None
    return (lo is None or dt >= lo) and (hi is None or dt <= hi)


class ItemFacts:
    """Everything the dashboard needs to know about one outreach item."""

    __slots__ = (
        "item", "sender", "sent_at", "stage", "first_reply_at", "first_positive_at",
        "bounced", "bounced_at", "booked", "booked_at", "followups", "failed", "failed_at",
    )

    def __init__(self, item: dict[str, Any], default_sender: Optional[str] = None):
        self.item = item
        self.sender = sender_key(item.get("sender_email") or default_sender)
        self.sent_at = parse_dt(item.get("sent_at"))
        self.stage = item.get("stage")
        self.first_reply_at: Optional[datetime] = None
        self.first_positive_at: Optional[datetime] = None
        self.bounced = False
        self.bounced_at: Optional[datetime] = None
        self.booked = self.stage == "booked"
        self.booked_at: Optional[datetime] = parse_dt(item.get("stage_updated_at")) if self.booked else None
        self.followups: list[datetime] = []  # when each sent follow-up went out
        self.failed = self.stage == "failed"
        self.failed_at = parse_dt(item.get("stage_updated_at")) if self.failed else None

    @property
    def replied(self) -> bool:
        return self.first_reply_at is not None

    @property
    def positive(self) -> bool:
        return self.first_positive_at is not None


def build_facts(
    items: Iterable[dict[str, Any]],
    messages: Iterable[dict[str, Any]],
    attempts: Iterable[dict[str, Any]],
    calendly: Iterable[dict[str, Any]],
    default_sender: Optional[str] = None,
) -> dict[str, ItemFacts]:
    facts = {i["item_id"]: ItemFacts(i, default_sender) for i in items}

    for m in messages:
        f = facts.get(m.get("item_id"))
        if f is None or m.get("direction") != "inbound":
            continue
        at = parse_dt(m.get("received_at")) or parse_dt(m.get("created_at"))
        intent = m.get("intent")
        if intent in HUMAN_REPLY_INTENTS and at is not None:
            if f.first_reply_at is None or at < f.first_reply_at:
                f.first_reply_at = at
        if intent in POSITIVE_INTENTS and at is not None:
            if f.first_positive_at is None or at < f.first_positive_at:
                f.first_positive_at = at
        if intent == "bounce":
            f.bounced = True
            if at is not None and (f.bounced_at is None or at < f.bounced_at):
                f.bounced_at = at

    for a in attempts:
        f = facts.get(a.get("item_id"))
        if f is not None and a.get("status") == "sent":
            f.followups.append(parse_dt(a.get("updated_at")) or parse_dt(a.get("claimed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    for f in facts.values():
        f.followups.sort()

    # A live Calendly booking dates the booking (earliest event wins); the
    # stage-change time is only the fallback for the email-confirmation path.
    cal_at: dict[str, datetime] = {}
    for e in calendly:
        f = facts.get(e.get("item_id"))
        if f is None or e.get("status") != "active":
            continue
        f.booked = True
        at = parse_dt(e.get("created_at"))
        if at is not None and (e["item_id"] not in cal_at or at < cal_at[e["item_id"]]):
            cal_at[e["item_id"]] = at
    for item_id, at in cal_at.items():
        facts[item_id].booked_at = at
    return facts


def select_facts(
    facts: dict[str, ItemFacts], *, sender: Optional[str], campaign_id: Optional[str],
) -> list[ItemFacts]:
    want = sender_key(sender) if sender else None
    return [
        f for f in facts.values()
        if (want is None or f.sender == want)
        and (not campaign_id or f.item.get("campaign_id") == campaign_id)
    ]


def cohort(
    selected: list[ItemFacts], lo: Optional[datetime], hi: Optional[datetime],
) -> tuple[list[ItemFacts], list[ItemFacts]]:
    """(sent cohort, failed items) for the date range."""
    sent = [f for f in selected if f.sent_at is not None and _in_range(f.sent_at, lo, hi)]
    failed = [f for f in selected if f.failed and f.sent_at is None and _in_range(f.failed_at, lo, hi)]
    return sent, failed


def totals(sent: list[ItemFacts], failed: list[ItemFacts]) -> dict[str, Any]:
    n_sent = len(sent)
    replied = sum(1 for f in sent if f.replied)
    positive = sum(1 for f in sent if f.positive)
    booked = sum(1 for f in sent if f.booked)
    n_failed = len(failed)
    return {
        "sent": n_sent,
        "replies": replied,
        "positive_replies": positive,
        "follow_ups_sent": sum(len(f.followups) for f in sent),
        "bookings": booked,
        "no_replies": sum(1 for f in sent if not f.replied and not f.booked),
        "failed": n_failed,
        "bounced": sum(1 for f in sent if f.bounced),
        "reply_rate": rate(replied, n_sent),
        "positive_reply_rate": rate(positive, n_sent),
        "booking_rate": rate(booked, n_sent),
        "failure_rate": rate(n_failed, n_sent + n_failed),
    }


def trend(selected: list[ItemFacts], lo: Optional[datetime], hi: Optional[datetime]) -> list[dict[str, Any]]:
    """Daily event counts (by the day each event happened)."""
    days: dict[str, dict[str, int]] = defaultdict(
        lambda: {"sent": 0, "replies": 0, "positive_replies": 0, "bookings": 0, "follow_ups": 0}
    )

    def bump(dt: Optional[datetime], key: str) -> None:
        if dt is not None and _in_range(dt, lo, hi):
            days[dt.date().isoformat()][key] += 1

    for f in selected:
        bump(f.sent_at, "sent")
        bump(f.first_reply_at, "replies")
        bump(f.first_positive_at, "positive_replies")
        if f.booked:
            bump(f.booked_at, "bookings")
        for at in f.followups:
            bump(at, "follow_ups")
    return [{"date": d, **v} for d, v in sorted(days.items())]


def accounts_block(
    facts: dict[str, ItemFacts], configured: list[str], *,
    campaign_id: Optional[str], lo: Optional[datetime], hi: Optional[datetime],
) -> list[dict[str, Any]]:
    """One row per sending account. Configured accounts always appear (zeroed
    if idle), plus any sender seen in the data -- adding a mailbox to the
    config needs no code change here."""
    keys: dict[str, str] = {sender_key(c): c for c in configured if c}
    for f in facts.values():
        if f.sender != UNKNOWN_SENDER:
            keys.setdefault(f.sender, (f.item.get("sender_email") or "").strip() or f.sender)
    has_unknown = any(f.sender == UNKNOWN_SENDER for f in facts.values())
    configured_keys = {sender_key(c) for c in configured if c}

    rows = []
    for key, label in sorted(keys.items(), key=lambda kv: kv[1].lower()):
        selected = select_facts(facts, sender=key, campaign_id=campaign_id)
        sent, failed = cohort(selected, lo, hi)
        rows.append({
            "sender": key, "label": label, "configured": key in configured_keys,
            **totals(sent, failed), "trend": trend(selected, lo, hi),
        })
    if has_unknown:
        selected = select_facts(facts, sender=UNKNOWN_SENDER, campaign_id=campaign_id)
        sent, failed = cohort(selected, lo, hi)
        rows.append({
            "sender": UNKNOWN_SENDER, "label": UNKNOWN_LABEL, "configured": False,
            **totals(sent, failed), "trend": trend(selected, lo, hi),
        })
    return rows


STAGES = ("needs_review", "pending", "sent", "waiting", "replied", "booked", "not_interested", "failed")


def campaigns_block(
    selected: list[ItemFacts], campaigns: dict[str, dict[str, Any]],
    lo: Optional[datetime], hi: Optional[datetime],
) -> list[dict[str, Any]]:
    """One row for EVERY campaign (idle ones show zeros). `stages` is the
    current pipeline state of the campaign's prospects and ignores the date
    range; the sent/reply/booking counts and rates describe the cohort sent
    inside the range."""
    by_campaign: dict[str, list[ItemFacts]] = defaultdict(list)
    for cid in campaigns:
        by_campaign[cid]
    for f in selected:
        by_campaign[f.item.get("campaign_id")].append(f)
    rows = []
    for cid, group in by_campaign.items():
        sent, failed = cohort(group, lo, hi)
        camp = campaigns.get(cid) or {}
        stages = {st: 0 for st in STAGES}
        for f in group:
            if f.stage in stages:
                stages[f.stage] += 1
        senders = sorted({f.sender for f in group if f.sent_at is not None or f.failed})
        rows.append({
            "campaign_id": cid,
            "name": camp.get("name") or cid,
            "created_at": camp.get("created_at"),
            "senders": senders,
            "prospects": len(group),
            "stages": stages,
            **totals(sent, failed),
        })
    rows.sort(key=lambda r: (-r["prospects"], -r["sent"], r["name"] or ""))
    return rows


def followups_block(sent: list[ItemFacts]) -> dict[str, Any]:
    """Per follow-up number: how many went out and how many of those prospects
    replied afterwards. A reply is "after" when it arrived at or after the
    follow-up was sent; a prospect who replied before it never receives one,
    so this is not a controlled comparison (see `note`)."""
    sent_n: dict[int, int] = defaultdict(int)
    replied_n: dict[int, int] = defaultdict(int)
    for f in sent:
        for idx, at in enumerate(f.followups, start=1):
            sent_n[idx] += 1
            if f.first_reply_at is not None and f.first_reply_at >= at:
                replied_n[idx] += 1
    return {
        "rows": [
            {"followup_number": n, "sent": sent_n[n], "replied_after": replied_n[n],
             "reply_rate": rate(replied_n[n], sent_n[n])}
            for n in sorted(sent_n)
        ],
        "note": "Replies counted when they arrived after the follow-up was sent; "
                "prospects who replied earlier never receive one.",
    }


def bookings_block(
    facts: dict[str, ItemFacts], calendly: list[dict[str, Any]], campaigns: dict[str, dict[str, Any]],
    prospects: dict[str, dict[str, Any]], *, sender: Optional[str], campaign_id: Optional[str],
    lo: Optional[datetime], hi: Optional[datetime],
) -> dict[str, Any]:
    """Every booking in scope. Matched bookings carry their sender/campaign;
    unmatched Calendly bookings (invitee used a different address) have
    neither and so only appear when no sender/campaign filter is applied."""
    want_sender = sender_key(sender) if sender else None
    rows: list[dict[str, Any]] = []
    seen_items: set[str] = set()

    def row(f: Optional[ItemFacts], e: Optional[dict[str, Any]], status: str, booked_at: Optional[datetime]) -> dict[str, Any]:
        item = f.item if f else {}
        camp = campaigns.get(item.get("campaign_id")) or {}
        pros = prospects.get(item.get("research_domain")) or {}
        return {
            "item_id": item.get("item_id"),
            "campaign_id": item.get("campaign_id"),
            "campaign_name": camp.get("name"),
            "sender": f.sender if f else None,
            "company_name": pros.get("name"),
            "contact_name": pros.get("contact_name") or (e or {}).get("invitee_name"),
            "contact_email": pros.get("contact_email") or item.get("to_email") or (e or {}).get("invitee_email"),
            "status": status,
            "booked_at": booked_at.isoformat() if booked_at else None,
            "meeting_starts_at": (e or {}).get("starts_at"),
            "no_show": bool((e or {}).get("no_show")),
            "source": (e or {}).get("source") or "email_reply",
            "matched": f is not None,
        }

    def wanted(f: Optional[ItemFacts]) -> bool:
        if f is None:
            return want_sender is None and not campaign_id
        return (want_sender is None or f.sender == want_sender) and (not campaign_id or f.item.get("campaign_id") == campaign_id)

    for e in calendly:
        f = facts.get(e.get("item_id"))
        if not wanted(f):
            continue
        at = parse_dt(e.get("created_at"))
        if not _in_range(at, lo, hi):
            continue
        rows.append(row(f, e, e.get("status") or "active", at))
        if f is not None and e.get("status") == "active":
            seen_items.add(f.item["item_id"])

    # Booked via the classifier's email path: stage is booked, no live event.
    for f in facts.values():
        if f.stage != "booked" or f.item["item_id"] in seen_items or not wanted(f):
            continue
        if not _in_range(f.booked_at, lo, hi):
            continue
        rows.append(row(f, None, "active", f.booked_at))

    rows.sort(key=lambda r: r["booked_at"] or "", reverse=True)
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"active": 0, "canceled": 0})
    for r in rows:
        if r["booked_at"]:
            by_day[r["booked_at"][:10]]["canceled" if r["status"] == "canceled" else "active"] += 1
    return {
        "rows": rows,
        "total": len(rows),
        "active": sum(1 for r in rows if r["status"] != "canceled"),
        "canceled": sum(1 for r in rows if r["status"] == "canceled"),
        "unmatched": sum(1 for r in rows if not r["matched"]),
        "trend": [{"date": d, **v} for d, v in sorted(by_day.items())],
    }


def prospect_rows(
    selected: list[ItemFacts], campaigns: dict[str, dict[str, Any]], prospects: dict[str, dict[str, Any]],
    lo: Optional[datetime], hi: Optional[datetime],
) -> list[dict[str, Any]]:
    out = []
    for f in selected:
        # Sent items follow the date range; items not sent yet (manual review,
        # pending, send-failed) are current pipeline state and always listed.
        if f.sent_at is not None and not _in_range(f.sent_at, lo, hi):
            continue
        item = f.item
        pros = prospects.get(item.get("research_domain")) or {}
        out.append({
            "item_id": item.get("item_id"),
            "campaign_id": item.get("campaign_id"),
            "campaign_name": (campaigns.get(item.get("campaign_id")) or {}).get("name"),
            "sender": f.sender,
            "company_name": pros.get("name") or item.get("research_domain"),
            "contact_name": pros.get("contact_name"),
            "contact_email": pros.get("contact_email") or item.get("to_email"),
            "stage": f.stage,
            "sent_at": f.sent_at.isoformat() if f.sent_at else None,
            "follow_ups": len(f.followups),
            "replied": f.replied,
            "positive": f.positive,
            "booked": f.booked,
            "failed": f.failed,
            "rejected": f.stage == "not_interested",
            "bounced": f.bounced,
            "error": item.get("error") if f.failed else None,
        })
    out.sort(key=lambda r: r["sent_at"] or "", reverse=True)
    return out
