"""Ingest qualified prospects from the Research Agent into Outreach's local
`prospects` cache, and select the top-N of them into a new campaign.

Two-step, matching the ICP brief's "select the configured top prospects":
1. `import_prospects()` -- pull + cache research data (no campaign yet).
2. `create_campaign_from_prospects()` -- pick the configured top-N *already
   cached* prospects and create pending outreach_items for them.

Kept separate so re-importing (refreshing stale research data) never
implicitly creates a new campaign, and a campaign can be built from prospects
imported in an earlier step without re-hitting the Research API.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from . import db
from .config import Config
from .db import SupabaseConn
from .models import Campaign, ImportedProspect, OutreachItem
from .personalize import _sender_signature, draft_message
from .research_client import ResearchClient


_VERIFICATION_RANK = {"valid": 2, "accept_all": 1}
_SENIORITY_RANK = {"executive": 2, "senior": 1}


def _contact_rank_key(c: dict[str, Any]) -> tuple[int, int, float]:
    """Rank contacts so the single most authoritative, highest-confidence
    person sorts first: verified email status beats seniority beats Hunter's
    raw numeric confidence. We only ever email one person per company (see
    _to_imported_prospect), so this ordering IS the send decision.
    """
    verification = _VERIFICATION_RANK.get(c.get("verification"), 0)
    seniority = _SENIORITY_RANK.get(c.get("seniority"), 0)
    confidence = c.get("confidence") if isinstance(c.get("confidence"), (int, float)) else 0
    return (verification, seniority, confidence)


def _best_contact(contacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the one contact to send to: most authoritative + most likely to
    be a valid, deliverable email. Never send to more than one person at the
    same company (see docstring on _contact_rank_key)."""
    if not contacts:
        return {}
    return max(contacts, key=_contact_rank_key)


def _to_imported_prospect(detail: dict[str, Any], run_id: str) -> ImportedProspect:
    """Map a Research Agent /companies/{domain} response onto our local
    snapshot shape. `detail` has the shape produced by
    Research_Agent/src/csg/api.py company_detail()."""
    company = detail.get("company") or {}
    score = detail.get("score") or {}
    qualification = detail.get("qualification") or {}
    qualification_attempt = detail.get("qualification_attempt") or {}
    contacts = detail.get("contacts") or []
    best_contact = _best_contact(contacts)

    return ImportedProspect(
        research_domain=company["domain"],
        research_run_id=run_id,
        name=company.get("name"),
        tier=score.get("tier"),
        score=score.get("score"),
        industry=company.get("industry"),
        region=company.get("region"),
        description=company.get("description"),
        pain_summary=qualification.get("pain_summary"),
        recommended_angle=qualification.get("recommended_angle"),
        open_reqs=(detail.get("postings") or [])[:10],
        hiring_signal=company.get("hiring_signal"),
        # A qualification row (above) always wins if one exists -- this only
        # reflects "failed and nothing has succeeded since" (see
        # ImportedProspect.qualification_failed).
        qualification_failed=not qualification and qualification_attempt.get("status") == "failed",
        contact_email=best_contact.get("email"),
        contact_name=" ".join(
            x for x in (best_contact.get("first_name"), best_contact.get("last_name")) if x
        ) or None,
        contact_position=best_contact.get("position"),
        contact_linkedin_url=best_contact.get("linkedin_url"),
        raw_prospect=detail,
    )


def import_prospects(
    conn: SupabaseConn, cfg: Config, run_id: str, *,
    tiers: Optional[list[str]] = None, min_score: Optional[float] = None,
    limit: Optional[int] = None,
) -> tuple[list[ImportedProspect], list[dict[str, Any]]]:
    """Pull qualified prospects from the Research Agent's API and cache them
    locally. Returns `(imported, excluded)` -- does not create a campaign.

    `excluded` is every tier-matching (A/B by default) company that was left
    out because contact discovery found no contact for it (see
    ResearchClient.qualified_prospects) -- a company scoring Tier A/B on its
    job-posting/firmographic signal alone is not enough to reach Outreach;
    Hunter has to have actually found someone to email.
    """
    tiers = tiers or cfg.qualified_tiers
    sel = cfg.selection
    min_score = min_score if min_score is not None else sel.get("min_score")
    require_email = bool(sel.get("require_contact_email", True))

    client = ResearchClient(cfg)
    try:
        details, excluded = client.qualified_prospects(
            run_id, tiers=tiers, min_score=min_score,
            require_contact_email=require_email, limit=limit,
        )
    finally:
        client.close()

    imported = []
    for detail in details:
        p = _to_imported_prospect(detail, run_id)
        db.upsert_prospect(conn, p)
        imported.append(p)
    return imported, excluded


def create_campaign_from_prospects(
    conn: SupabaseConn, cfg: Config, *, name: Optional[str] = None,
    research_run_id: Optional[str] = None, limit: Optional[int] = None,
    tiers: Optional[list[str]] = None,
) -> tuple[Campaign, list[dict[str, Any]]]:
    """Select the configured top-N contactable, already-cached prospects and
    create outreach_items for them. Returns `(campaign, excluded)`.

    A cached prospect with no contact email is never selected into a
    campaign, full stop -- Hunter having found nobody to email means there is
    nothing this stage can do with that company yet, regardless of its tier
    or score. `excluded` lists every tier-matching cached prospect left out
    for exactly that reason, as `{"domain", "name", "tier"}`, so the caller
    can report it plainly (e.g. "No contacts found for Acme Co, so it was not
    added to prospects/campaign") instead of the company just silently not
    appearing. `limit` is a send-budget ceiling on the contactable pool, never
    a reason to pad it out with contact-less companies -- if only 2 of 3
    qualifying prospects have a contact, the campaign gets exactly those 2,
    even though `limit` allows more.

    Ranking within the contactable pool is the *cached* score descending
    (Research's ranking is reused verbatim; this agent never re-scores).
    """
    sel = cfg.selection
    limit = limit if limit is not None else int(sel.get("limit", 25))
    tiers = tiers or cfg.qualified_tiers

    prospects = db.list_prospects(conn, run_id=research_run_id)
    prospects = [p for p in prospects if p.tier in tiers]

    contactable = [p for p in prospects if p.has_contact]
    excluded = [
        {"domain": p.research_domain, "name": p.name or p.research_domain, "tier": p.tier}
        for p in prospects if not p.has_contact
    ]

    contactable.sort(key=lambda p: p.score or 0, reverse=True)
    top = contactable[:limit]

    if not top:
        # Nothing cached and contactable for this run/tier selection yet --
        # the Dashboard should already keep Create Campaign disabled until
        # contactable prospects exist (see Prospects.tsx), so reaching here
        # means either a stale UI or a direct API call; either way, a
        # zero-item campaign is never useful and would just have to be
        # deleted and redone.
        raise ValueError(
            f"no contactable cached prospects for research_run_id={research_run_id!r} tiers={tiers!r} -- "
            "every matching prospect is missing a contact email (see contacts.discover_run on the "
            "Research Agent), or none have been imported yet"
        )

    campaign = Campaign(
        campaign_id=str(uuid.uuid4()),
        name=name or f"Campaign {db.utcnow().isoformat(timespec='seconds')}",
        research_run_id=research_run_id,
        channel=cfg.sending.get("channel", "email"),
        selection={"limit": limit, "tiers": tiers, "min_score": sel.get("min_score")},
    )
    db.create_campaign(conn, campaign)

    for p in top:
        draft = draft_message(cfg, p)
        # has_contact is guaranteed True for everything in `top` now; the only
        # remaining "not enough to trust a send" gate is personalization.
        # draft_message already tries an LLM against pain_summary/
        # recommended_angle first, then against whatever other research data
        # is on file (industry/description/open_reqs/score explanation) --
        # see personalize.py's module docstring -- so a 'template_only' draft
        # here means there was truly nothing to personalize from (or every
        # LLM call failed), not merely that the optional qualify step hasn't
        # run yet. Only that draft holds the item in Needs Review until a
        # person approves it or a later re-import/redraft earns it a real one.
        stage = "pending" if draft.personalized_from != "template_only" else "needs_review"
        item = OutreachItem(
            item_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            research_domain=p.research_domain,
            stage=stage,
            channel=campaign.channel,
            subject=draft.subject,
            body=draft.body,
            personalized_from=draft.personalized_from,
            to_email=p.contact_email,
        )
        db.create_item(conn, item)

    return campaign, excluded


# Fixed test fixtures for `create_test_campaign` -- fake domains that will
# never collide with a real Research Agent domain, so these prospects can
# never be mistaken for (or mixed into) real campaign selection, which only
# ever queries by real `research_domain` values coming from Research's API.
#
# Bodies end with `{{signature}}`, filled in at creation time from the same
# `personalize._sender_signature(cfg)` every real drafted email uses -- so a
# test send always carries the same sign-off (name, company, location,
# website link) as a real one instead of a hand-rolled, easy-to-drift copy.
_TEST_PROSPECTS: list[dict[str, Any]] = [
    {
        "research_domain": "test-pending.csg-internal.invalid",
        "name": "CSG Test Co (Pending)",
        "contact_name": "Affan (Test Pending)",
        "contact_email": "affanjan007@gmail.com",
        "contact_position": "Test Contact",
        "subject": "Quick question about your hiring pipeline",
        "body": (
            "Hi Affan,\n\n"
            "This is a test email from the CSG Outreach Agent's test pipeline -- "
            "verifying that campaign creation, personalization, and sending all "
            "work end-to-end before we run real outreach.\n\n"
            "No action needed beyond confirming this arrived correctly.\n\n"
            "Best,\n{{signature}}"
        ),
        "stage": "pending",
    },
    {
        "research_domain": "test-review.csg-internal.invalid",
        "name": "CSG Test Co (Manual Review)",
        "contact_name": "Affan (Test Review)",
        "contact_email": "affanjan008@gmail.com",
        "contact_position": "Test Contact",
        "subject": "Following up -- test draft pending review",
        "body": (
            "Hi Affan,\n\n"
            "This is a test email from the CSG Outreach Agent's test pipeline, "
            "seeded directly into Manual Review to verify the review/approve "
            "step of the 6-stage pipeline.\n\n"
            "This draft should stay in Manual Review until manually approved.\n\n"
            "Best,\n{{signature}}"
        ),
        "stage": "needs_review",
    },
]


def create_test_campaign(conn: SupabaseConn, cfg: Config) -> Campaign:
    """Create (or reuse) a fixed, safe-to-resend Test Campaign with two
    synthetic prospects for exercising the full pipeline UI without touching
    real Research Agent data or real contacts:

    - affanjan007@gmail.com lands in Pending (ready to send manually).
    - affanjan008@gmail.com lands in Manual Review (needs_review stage) and
      is NOT auto-approved -- exercises the approve/redraft review step.

    Both prospects use `.invalid` fake domains reserved for this fixture, so
    they can never collide with or be confused for a real imported prospect.
    Safe to call repeatedly: prospects upsert by domain and items upsert by
    (campaign_id, research_domain), so re-creating just resets the same rows
    rather than piling up duplicates -- except the campaign_id itself, which
    is randomly generated each call, so a *new* Test Campaign row appears
    each time this is invoked (mirroring "New campaign" always making a new
    campaign row for a real selection).
    """
    channel = cfg.sending.get("channel", "email")
    signature = _sender_signature(cfg)
    campaign = Campaign(
        campaign_id=str(uuid.uuid4()),
        name="Test Campaign",
        research_run_id=None,
        channel=channel,
        selection={"test": True},
        notes="Synthetic test campaign for pipeline QA -- not real prospects.",
    )
    db.create_campaign(conn, campaign)

    for fixture in _TEST_PROSPECTS:
        prospect = ImportedProspect(
            research_domain=fixture["research_domain"],
            research_run_id=None,
            name=fixture["name"],
            tier=None,
            score=None,
            industry="Internal QA",
            region=None,
            description="Synthetic test prospect used to exercise the outreach pipeline UI.",
            contact_name=fixture["contact_name"],
            contact_email=fixture["contact_email"],
            contact_position=fixture["contact_position"],
        )
        db.upsert_prospect(conn, prospect)

        item = OutreachItem(
            item_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            research_domain=fixture["research_domain"],
            stage=fixture["stage"],
            channel=channel,
            subject=fixture["subject"],
            body=fixture["body"].replace("{{signature}}", signature),
            personalized_from="test_fixture",
            to_email=fixture["contact_email"],
        )
        db.create_item(conn, item)

    return campaign
