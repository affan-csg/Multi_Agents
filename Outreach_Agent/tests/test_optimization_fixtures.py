"""Deterministic synthetic fixtures for the Campaign-Level Optimization
layer, same modulo-based (no randomness) philosophy as
test_learning_fixtures.py -- expected rates are exact integers, not
approximately-N flaky assertions."""

from __future__ import annotations

from typing import Optional


def seed_campaign(conn, campaign_id: str, name: str = "Campaign") -> None:
    conn.tables.setdefault("campaigns", []).append({"campaign_id": campaign_id, "name": name})


def seed_prospect(conn, domain: str, *, tier: Optional[str] = None, industry: Optional[str] = None) -> None:
    conn.tables.setdefault("prospects", []).append({
        "research_domain": domain, "tier": tier, "industry": industry,
        "contact_email": f"contact@{domain}",
    })


DIRECT_CTA = "Are you free to book a 15-minute call this week? Here's my calendar link to grab a slot."
SOFT_CTA = "Would it make sense to have a quick chat sometime? Let me know if you're open to it."

SHORT_BODY_TMPL = "Hi there, I noticed your team is scaling fast. {cta}\n\nBest,\nJordan"
LONG_BODY_TMPL = (
    "Hi there, I wanted to reach out because I've been following your company for a while and "
    "think there could be a great fit between what we do and what you're building right now. "
    "We've worked with a lot of similar teams in your space and have seen some great results "
    "helping them scale their operations more efficiently without adding a ton of overhead. "
    "Over the last few years we've partnered with dozens of fast-growing companies across a "
    "range of industries, and one thing that keeps coming up is how much time gets lost to "
    "manual processes that could easily be automated with the right tooling and the right "
    "team behind it, which is exactly the gap we specialize in closing for our customers. "
    "I'd love to share a bit more about how this has worked for other companies like yours, "
    "including some specific numbers on time saved and the kind of ramp-up period to expect. "
    "{cta}\n\nBest,\nJordan"
)


def _make_item(conn, item_id, campaign_id, domain, *, subject, body, personalized_from, followup_count=0):
    conn.tables.setdefault("outreach_items", []).append({
        "item_id": item_id, "campaign_id": campaign_id, "research_domain": domain,
        "stage": "sent", "channel": "email", "subject": subject, "body": body,
        "personalized_from": personalized_from, "sent_at": "2026-01-01T00:00:00",
        "created_at": "2026-01-01T00:00:00", "stage_updated_at": "2026-01-01T00:00:00",
        "followup_count": followup_count,
    })


def _make_reply(conn, item_id, msg_id, intent):
    conn.tables.setdefault("conversation_messages", []).append({
        "message_id": msg_id, "item_id": item_id, "direction": "inbound",
        "intent": intent, "created_at": "2026-01-02T00:00:00",
    })


def build_cta_style_dataset(
    conn, *, campaign_id="cta-camp", n_direct=40, n_soft=40,
    direct_reply_every=2, soft_reply_every=20, tier: Optional[str] = None,
):
    """Half the items use a direct CTA, half a soft CTA, subject/opening held
    constant so cta_style is the only varying dimension. Outcomes assigned by
    exact modulo, deterministic reply rates."""
    seed_campaign(conn, campaign_id)
    total = n_direct + n_soft
    for i in range(total):
        item_id = f"{campaign_id}-item-{i}"
        domain = f"{campaign_id}{i}.com"
        seed_prospect(conn, domain, tier=tier)
        is_direct = i < n_direct
        cta = DIRECT_CTA if is_direct else SOFT_CTA
        _make_item(
            conn, item_id, campaign_id, domain,
            subject="Quick question", body=SHORT_BODY_TMPL.format(cta=cta),
            personalized_from="pain_summary",
        )
        if is_direct and i % direct_reply_every == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
        elif not is_direct and i % soft_reply_every == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
    return total


def build_length_dataset(conn, *, campaign_id="len-camp", n_short=40, n_long=40, short_reply_every=2, long_reply_every=20):
    seed_campaign(conn, campaign_id)
    total = n_short + n_long
    for i in range(total):
        item_id = f"{campaign_id}-item-{i}"
        domain = f"{campaign_id}{i}.com"
        seed_prospect(conn, domain)
        is_short = i < n_short
        body = (SHORT_BODY_TMPL if is_short else LONG_BODY_TMPL).format(cta=SOFT_CTA)
        _make_item(conn, item_id, campaign_id, domain, subject="Quick question", body=body, personalized_from="pain_summary")
        if is_short and i % short_reply_every == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
        elif not is_short and i % long_reply_every == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
    return total


def build_segmented_dataset(conn, *, campaign_id="seg-camp"):
    """Direct CTA helps tier A (50% vs 5%) but has NO effect for tier C
    (20% vs 20% both ways) -- proves segmentation surfaces the tier-A
    pattern without letting tier C's flat data blend it away, and without
    fabricating a tier-C pattern that doesn't exist."""
    seed_campaign(conn, campaign_id)
    total = 0
    # Tier A: 30 direct (50% reply), 30 soft (5% reply, i.e. every 20th)
    for i in range(60):
        item_id = f"{campaign_id}-a-{i}"
        domain = f"{campaign_id}a{i}.com"
        seed_prospect(conn, domain, tier="A")
        is_direct = i < 30
        cta = DIRECT_CTA if is_direct else SOFT_CTA
        _make_item(conn, item_id, campaign_id, domain, subject="Quick question", body=SHORT_BODY_TMPL.format(cta=cta), personalized_from="pain_summary")
        if is_direct and i % 2 == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
        elif not is_direct and i % 20 == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
        total += 1
    # Tier C: 30 direct, 30 soft, BOTH exactly 20% reply (every 5th)
    for i in range(60):
        item_id = f"{campaign_id}-c-{i}"
        domain = f"{campaign_id}c{i}.com"
        seed_prospect(conn, domain, tier="C")
        is_direct = i < 30
        cta = DIRECT_CTA if is_direct else SOFT_CTA
        _make_item(conn, item_id, campaign_id, domain, subject="Quick question", body=SHORT_BODY_TMPL.format(cta=cta), personalized_from="pain_summary")
        if i % 5 == 0:
            _make_reply(conn, item_id, f"{item_id}-m", "interested")
        total += 1
    return total
