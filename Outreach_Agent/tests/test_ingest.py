"""Selection logic: create_campaign_from_prospects must pick the configured
top-N *contactable* cached prospects, restricted to qualified tiers, ranked
by score -- excluding any tier-matching prospect with no contact email
outright, never padding a slot with one -- and must never call the Research
API (it only reads what's already cached)."""

from __future__ import annotations

import pytest

from outreach import db, ingest
from outreach.config import load_config
from outreach.models import ImportedProspect
from outreach.personalize import DraftedMessage


def _seed(conn, domain: str, tier: str, score: float, has_contact: bool = True, qualified: bool = False) -> None:
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, research_run_id="run1", tier=tier, score=score,
        contact_email=f"contact@{domain}" if has_contact else None,
        pain_summary="hiring urgently for ML roles" if qualified else None,
    ))


def test_top_n_selected_by_score_descending(conn):
    cfg = load_config()
    _seed(conn, "low.com", "A", 60)
    _seed(conn, "high.com", "A", 95)
    _seed(conn, "mid.com", "B", 75)

    campaign, excluded = ingest.create_campaign_from_prospects(conn, cfg, limit=2, tiers=["A", "B"])
    items = db.list_items(conn, campaign_id=campaign.campaign_id)

    assert len(items) == 2
    domains = {i.research_domain for i in items}
    assert domains == {"high.com", "mid.com"}  # low.com (60) excluded, only top 2 by score
    assert excluded == []


def test_limit_never_padded_with_contact_less_prospects(conn):
    """With N=3 and 5 qualified A/B companies where only 2 have a contact,
    the campaign must select exactly those 2 contactable ones -- never pad
    the remaining slot with a contact-less prospect just because `limit`
    allows more. `limit` is a ceiling on the contactable pool, not a target
    to fill regardless of contactability."""
    cfg = load_config()
    _seed(conn, "top-no-contact.com", "A", 99, has_contact=False)
    _seed(conn, "second-no-contact.com", "A", 90, has_contact=False)
    _seed(conn, "third-has-contact.com", "B", 70, has_contact=True)
    _seed(conn, "fourth-has-contact.com", "A", 60, has_contact=True)
    _seed(conn, "fifth-no-contact.com", "B", 50, has_contact=False)

    campaign, excluded = ingest.create_campaign_from_prospects(conn, cfg, limit=3, tiers=["A", "B"])
    items = db.list_items(conn, campaign_id=campaign.campaign_id)
    domains = {i.research_domain for i in items}

    # only the 2 contactable prospects are selected, even though limit=3 --
    # no contact-less prospect fills the unused third slot.
    assert domains == {"third-has-contact.com", "fourth-has-contact.com"}
    assert {e["domain"] for e in excluded} == {
        "top-no-contact.com", "second-no-contact.com", "fifth-no-contact.com",
    }


def test_limit_fills_all_slots_from_contactable_when_enough_exist(conn):
    """When there are already >= N contactable prospects, no contact-less
    prospect should be selected at all, even if it outscores one of them."""
    cfg = load_config()
    _seed(conn, "no-contact-highest.com", "A", 99, has_contact=False)
    _seed(conn, "contact-a.com", "A", 80, has_contact=True)
    _seed(conn, "contact-b.com", "A", 70, has_contact=True)
    _seed(conn, "contact-c.com", "B", 60, has_contact=True)

    campaign, excluded = ingest.create_campaign_from_prospects(conn, cfg, limit=3, tiers=["A", "B"])
    items = db.list_items(conn, campaign_id=campaign.campaign_id)
    domains = {i.research_domain for i in items}

    assert domains == {"contact-a.com", "contact-b.com", "contact-c.com"}
    assert "no-contact-highest.com" not in domains
    assert {e["domain"] for e in excluded} == {"no-contact-highest.com"}


def test_no_cached_prospects_for_run_raises_instead_of_creating_empty_campaign(conn):
    """A research run with nothing imported yet (still discovering, or the
    import step just hasn't run) must never silently produce a zero-item
    campaign -- the caller (API route / Dashboard) needs a clear error to
    surface instead."""
    cfg = load_config()
    _seed(conn, "other-run.com", "A", 80)  # cached, but for a different run_id

    with pytest.raises(ValueError, match="no contactable cached prospects"):
        ingest.create_campaign_from_prospects(conn, cfg, research_run_id="run-with-nothing", tiers=["A", "B"])


def test_no_contactable_prospects_raises_even_if_tier_matches_exist(conn):
    """Tier A/B companies that all lack a contact must not produce an
    empty-but-technically-created campaign -- same error path as having
    nothing cached at all, since there is nothing sendable either way."""
    cfg = load_config()
    _seed(conn, "nocontact-a.com", "A", 90, has_contact=False)
    _seed(conn, "nocontact-b.com", "B", 80, has_contact=False)

    with pytest.raises(ValueError, match="no contactable cached prospects"):
        ingest.create_campaign_from_prospects(conn, cfg, tiers=["A", "B"])


def test_tier_filter_excludes_unwanted_tiers(conn):
    cfg = load_config()
    _seed(conn, "a-tier.com", "A", 80)
    _seed(conn, "c-tier.com", "C", 90)  # higher score, but wrong tier

    campaign, excluded = ingest.create_campaign_from_prospects(conn, cfg, limit=10, tiers=["A", "B"])
    items = db.list_items(conn, campaign_id=campaign.campaign_id)

    assert [i.research_domain for i in items] == ["a-tier.com"]
    assert excluded == []


def test_prospects_without_contact_email_are_excluded_not_needs_reviewed(conn):
    """A Tier A/B prospect with no contact email must be excluded from the
    campaign outright (reported in `excluded`), not given a needs_review
    item -- Hunter finding nobody to email means there is nothing this stage
    can act on yet."""
    cfg = load_config()
    _seed(conn, "nocontact.com", "A", 99, has_contact=False)
    _seed(conn, "hascontact.com", "A", 50, has_contact=True)

    campaign, excluded = ingest.create_campaign_from_prospects(conn, cfg, limit=10, tiers=["A"])
    items = {i.research_domain: i for i in db.list_items(conn, campaign_id=campaign.campaign_id)}

    assert set(items) == {"hascontact.com"}
    assert {e["domain"] for e in excluded} == {"nocontact.com"}


def test_prospect_with_contact_but_no_qualification_needs_review(conn):
    """Having a contact email is not enough on its own -- a prospect that
    was never LLM-qualified still can't be trusted with a real personalized
    draft, so it needs review too (the two gates are independent)."""
    cfg = load_config()
    _seed(conn, "unqualified.com", "A", 80, has_contact=True, qualified=False)

    campaign, _ = ingest.create_campaign_from_prospects(conn, cfg, limit=10, tiers=["A"])
    item = db.list_items(conn, campaign_id=campaign.campaign_id)[0]

    assert item.stage == "needs_review"
    assert item.to_email == "contact@unqualified.com"  # contact IS present -- only the qualification gate held it back


def test_created_items_with_research_data_start_pending(conn, monkeypatch):
    """A prospect that drafts as genuinely personalized (LLM available and
    used pain_summary/recommended_angle) goes straight to pending --
    send-ready. Stubs draft_message so this test doesn't depend on a real
    LLM call, only on ingest.py's stage-gating logic."""
    monkeypatch.setattr(
        ingest, "draft_message",
        lambda cfg, p: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    cfg = load_config()
    _seed(conn, "acme.com", "A", 80, qualified=True)

    campaign, _ = ingest.create_campaign_from_prospects(conn, cfg, limit=10, tiers=["A"])
    item = db.list_items(conn, campaign_id=campaign.campaign_id)[0]

    assert item.stage == "pending"
    assert item.to_email == "contact@acme.com"


def test_created_items_without_research_data_need_review(conn, monkeypatch):
    """A prospect with no pain_summary/recommended_angle can only get a
    generic template draft -- it must NOT enter pending on its own, it waits
    in needs_review for a person (or a future re-draft once qualified)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg = load_config()
    _seed(conn, "nodata.com", "A", 80, qualified=False)

    campaign, _ = ingest.create_campaign_from_prospects(conn, cfg, limit=10, tiers=["A"])
    item = db.list_items(conn, campaign_id=campaign.campaign_id)[0]

    assert item.stage == "needs_review"
    assert item.personalized_from == "template_only"


def test_best_contact_prefers_verified_over_higher_raw_confidence():
    contacts = [
        {"email": "unverified@acme.com", "seniority": "senior", "confidence": 99, "verification": None},
        {"email": "verified@acme.com", "seniority": "senior", "confidence": 60, "verification": "valid"},
    ]
    assert ingest._best_contact(contacts)["email"] == "verified@acme.com"


def test_best_contact_prefers_executive_over_senior_at_equal_verification():
    contacts = [
        {"email": "senior@acme.com", "seniority": "senior", "confidence": 95, "verification": "valid"},
        {"email": "exec@acme.com", "seniority": "executive", "confidence": 70, "verification": "valid"},
    ]
    assert ingest._best_contact(contacts)["email"] == "exec@acme.com"


def test_best_contact_falls_back_to_confidence_when_tied():
    contacts = [
        {"email": "low@acme.com", "seniority": "executive", "confidence": 40, "verification": "valid"},
        {"email": "high@acme.com", "seniority": "executive", "confidence": 90, "verification": "valid"},
    ]
    assert ingest._best_contact(contacts)["email"] == "high@acme.com"


def test_best_contact_empty_list_returns_empty_dict():
    assert ingest._best_contact([]) == {}


def test_to_imported_prospect_uses_best_contact_not_first():
    detail = {
        "company": {"domain": "acme.com", "name": "Acme"},
        "score": {"tier": "A", "score": 80},
        "qualification": {},
        "postings": [],
        "contacts": [
            {"email": "first@acme.com", "first_name": "First", "last_name": "Person",
             "seniority": "senior", "confidence": 99, "verification": None},
            {"email": "exec@acme.com", "first_name": "Exec", "last_name": "Person",
             "seniority": "executive", "confidence": 80, "verification": "valid"},
        ],
    }
    prospect = ingest._to_imported_prospect(detail, "run1")
    assert prospect.contact_email == "exec@acme.com"
    assert prospect.contact_name == "Exec Person"


def test_to_imported_prospect_carries_hiring_signal():
    """Regression: hiring_signal must survive the Research -> Outreach
    handoff so an empty open_reqs list is never silently read here as
    'confirmed no openings' -- see Research_Agent's models.HiringSignal and
    personalize.py's _render_open_reqs."""
    detail = {
        "company": {"domain": "acme.com", "name": "Acme", "hiring_signal": "unknown"},
        "score": {"tier": "B", "score": 40},
        "qualification": {},
        "postings": [],
        "contacts": [{"email": "jane@acme.com"}],
    }
    prospect = ingest._to_imported_prospect(detail, "run1")
    assert prospect.hiring_signal == "unknown"


def test_to_imported_prospect_confirmed_hiring_signal():
    detail = {
        "company": {"domain": "acme.com", "name": "Acme", "hiring_signal": "confirmed"},
        "score": {"tier": "B", "score": 40},
        "qualification": {},
        "postings": [],
        "contacts": [{"email": "jane@acme.com"}],
    }
    prospect = ingest._to_imported_prospect(detail, "run1")
    assert prospect.hiring_signal == "confirmed"
