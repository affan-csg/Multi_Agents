from __future__ import annotations

from types import SimpleNamespace

from outreach import api, db, ingest
from outreach.models import Campaign, ImportedProspect


def test_repeated_campaign_request_returns_existing_campaign(monkeypatch):
    existing = Campaign(
        campaign_id="stable-campaign",
        research_run_id="run-1",
        name="Research campaign",
    )
    monkeypatch.setattr(api.db, "connect", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(db, "get_campaign", lambda _conn, campaign_id: existing)
    monkeypatch.setattr(db, "list_items", lambda *_args, **_kwargs: [{"item_id": "one"}])
    monkeypatch.setattr(api, "load_config", lambda: object())
    captured = {}

    def no_missing_prospects(_conn, _cfg, **kwargs):
        captured.update(kwargs)
        raise ValueError("nothing contactable remains")

    monkeypatch.setattr(api.ingest, "create_campaign_from_prospects", no_missing_prospects)

    result = api.create_campaign(api.CampaignCreateRequest(
        campaign_id="stable-campaign",
        research_run_id="run-1",
        tiers=["A", "B"],
    ))

    assert result["campaign"]["campaign_id"] == "stable-campaign"
    assert result["item_count"] == 1
    assert captured["campaign_id"] == "stable-campaign"


def test_partial_campaign_replay_drafts_only_missing_domains(monkeypatch):
    existing = Campaign(
        campaign_id="stable-campaign",
        research_run_id="run-1",
        name="Research campaign",
    )
    prospects = [
        ImportedProspect(research_domain="done.example", research_run_id="run-1", tier="A", contact_email="done@example.test"),
        ImportedProspect(research_domain="pending.example", research_run_id="run-1", tier="B", contact_email="pending@example.test"),
    ]
    cfg = SimpleNamespace(
        selection={"limit": 10, "min_score": 50},
        qualified_tiers=["A", "B"],
        sending={"channel": "email"},
    )
    drafted = []

    monkeypatch.setattr(db, "get_campaign", lambda _conn, _campaign_id: existing)
    monkeypatch.setattr(db, "list_prospects", lambda *_args, **_kwargs: prospects)
    monkeypatch.setattr(db, "domains_with_existing_items", lambda *_args: {"done.example"})
    monkeypatch.setattr(db, "create_campaign", lambda *_args: (_ for _ in ()).throw(AssertionError("existing campaign must be reused")))
    monkeypatch.setattr(db, "create_item", lambda _conn, item: drafted.append(item))
    monkeypatch.setattr(
        ingest, "draft_message",
        lambda *_args, **_kwargs: SimpleNamespace(
            subject="subject", body="body", personalized_from="test",
        ),
    )

    campaign, excluded = ingest.create_campaign_from_prospects(
        object(), cfg, research_run_id="run-1", campaign_id="stable-campaign",
    )

    assert campaign is existing
    assert excluded == []
    assert [item.research_domain for item in drafted] == ["pending.example"]