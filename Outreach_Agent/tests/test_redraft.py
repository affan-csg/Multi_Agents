"""Tests for /items/{id}/redraft and /campaigns/{id}/redraft-generic's
auto-promotion rule (outreach/api.py).

Regression: auto-promoting a needs_review item to pending only checked
whether the new draft was genuinely personalized -- it never re-checked
whether the prospect actually has a contact email, the OTHER independent
gate campaign creation applies (see ingest.create_campaign_from_prospects
and its "two independent needs_review gates" docstring). A redraft only
ever fixes personalization (it re-runs draft_message, not contact
discovery), so a needs_review item held back purely for lacking a contact
would have been wrongly promoted to pending -- send-ready with nobody to
send it to -- the moment its draft became personalized.
"""

from __future__ import annotations

from outreach import api, db
from outreach.models import Campaign, ImportedProspect, OutreachItem
from outreach.personalize import DraftedMessage


def _seed_prospect(conn, domain: str, *, has_contact: bool, qualified: bool) -> None:
    db.upsert_prospect(conn, ImportedProspect(
        research_domain=domain, research_run_id="run1", tier="A", score=80,
        contact_email=f"contact@{domain}" if has_contact else None,
        pain_summary="hiring urgently" if qualified else None,
    ))


def _seed_item(conn, domain: str, *, stage: str) -> str:
    campaign = Campaign(campaign_id="camp1", name="Test", research_run_id="run1")
    db.create_campaign(conn, campaign)
    item = OutreachItem(
        item_id=f"item-{domain}", campaign_id="camp1", research_domain=domain,
        stage=stage, personalized_from="template_only",
    )
    db.create_item(conn, item)
    return item.item_id


def test_redraft_promotes_when_both_gates_clear(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "hascontact.com", has_contact=True, qualified=False)
    item_id = _seed_item(conn, "hascontact.com", stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["stage"] == "pending"
    assert db.get_item(conn, item_id).stage == "pending"


def test_redraft_item_requests_web_search():
    """/items/{id}/redraft is a single, deliberate user action -- it must ask
    draft_message to try a live web search as a last resort before settling
    for the static template (see personalize.draft_message's use_web_search
    tier), unlike campaign creation which never does."""
    import inspect
    source = inspect.getsource(api.redraft_item)
    assert "use_web_search=True" in source


def test_redraft_generic_items_requests_web_search():
    """Same guarantee for the bulk redraft endpoint -- each item it touches
    is already known to be stuck on the static template, so it's worth a
    search call per item, same as the single-item redraft."""
    import inspect
    source = inspect.getsource(api.redraft_generic_items)
    assert "use_web_search=True" in source


def test_redraft_refreshes_to_email_from_current_prospect(conn, monkeypatch):
    """The other half of the same regression: a contact can legitimately
    appear AFTER an item was created (e.g. Hunter.io contact discovery ran
    late, or a re-import picked up a contact that a narrower search missed
    the first time -- see contacts.py's HR-department fallback). redraft
    must pull the item's to_email from the current cached prospect, not
    leave it frozen on the None snapshot from campaign-creation time, or an
    item promoted to pending would have nobody to actually send to."""
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "hascontact.com", has_contact=True, qualified=False)
    item_id = _seed_item(conn, "hascontact.com", stage="needs_review")
    assert db.get_item(conn, item_id).to_email is None  # stale snapshot, as at creation time

    result = api.redraft_item(item_id)

    assert result["to_email"] == "contact@hascontact.com"
    assert db.get_item(conn, item_id).to_email == "contact@hascontact.com"
    assert db.get_item(conn, item_id).stage == "pending"


def test_redraft_does_not_promote_when_contact_still_missing(conn, monkeypatch):
    """The regression: a genuinely personalized redraft must NOT promote an
    item that still has no contact email -- there's nobody to send it to."""
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "nocontact.com", has_contact=False, qualified=False)
    item_id = _seed_item(conn, "nocontact.com", stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["stage"] == "needs_review"
    assert db.get_item(conn, item_id).stage == "needs_review"


def test_redraft_does_not_promote_when_still_template_only(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="template_only"),
    )
    _seed_prospect(conn, "hascontact.com", has_contact=True, qualified=False)
    item_id = _seed_item(conn, "hascontact.com", stage="needs_review")

    result = api.redraft_item(item_id)

    assert result["stage"] == "needs_review"


def test_redraft_generic_does_not_promote_when_contact_still_missing(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "nocontact.com", has_contact=False, qualified=False)
    item_id = _seed_item(conn, "nocontact.com", stage="needs_review")

    result = api.redraft_generic_items("camp1")

    assert result["upgraded"] == 1  # personalization did improve
    assert db.get_item(conn, item_id).stage == "needs_review"  # but still held back for no contact


def test_redraft_generic_promotes_when_both_gates_clear(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: conn)
    monkeypatch.setattr(
        api.personalize, "draft_message",
        lambda cfg, p, **kwargs: DraftedMessage(subject="Hi", body="body", personalized_from="pain_summary"),
    )
    _seed_prospect(conn, "hascontact.com", has_contact=True, qualified=False)
    item_id = _seed_item(conn, "hascontact.com", stage="needs_review")

    result = api.redraft_generic_items("camp1")

    assert result["upgraded"] == 1
    assert db.get_item(conn, item_id).stage == "pending"
