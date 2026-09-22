"""Tests for the prospects/research-run bug fix (see product owner's
prospects_prompt.md): a prospect already imported for a research run must
stay visible on the Prospects page under that run even after "running
prospect finding again" -- which, on the Research Agent side, never resumes
the old run_id, it always mints a brand-new one (see
Research_Agent/src/csg/discover.py run_discovery, and
Dashboard/src/agents/research.ts's DiscoverRequest, which has no run_id
field at all). Re-importing the same company under that new run_id used to
overwrite prospects.research_run_id outright (upsert keyed on
research_domain alone), so a person still viewing the OLD run's filtered
list on the Prospects page would see the prospect vanish, even though
nothing was ever deleted.

The fix adds `research_run_ids` -- every run that ever imported this domain,
merged (never overwritten) on each upsert -- and list_prospects(run_id=...)
now filters on membership in that list instead of equality against the
single `research_run_id` column."""

from __future__ import annotations

from outreach import db
from outreach.models import Campaign, ImportedProspect, OutreachItem


def test_prospect_reimported_under_new_run_stays_visible_under_old_run(conn):
    """The exact bug: import under run A, then "run prospect finding again"
    for what the user considers the same research produces a fresh run B
    (never the same run_id), and re-importing acme.com there must not make
    it disappear from someone still viewing run A's filtered list."""
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", research_run_id="disc-A", tier="A", score=90,
    ))
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-A")] == ["acme.com"]

    # Re-run discovery for "the same research" -> Research Agent mints a new
    # run_id -> re-import upserts the same domain under it.
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", research_run_id="disc-B", tier="A", score=95,
    ))

    # Still visible under the OLD run someone might still be viewing --
    # this is what previously disappeared.
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-A")] == ["acme.com"]
    # Also visible under the new run.
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-B")] == ["acme.com"]
    # And under "all runs" (viewRun="" in Prospects.tsx).
    assert [p.research_domain for p in db.list_prospects(conn)] == ["acme.com"]


def test_reimport_refreshes_snapshot_data_and_current_run_pointer(conn):
    """research_run_id (singular) still means "which run this snapshot's
    data currently reflects" -- re-importing must still refresh score/tier
    from the newest run, only the run MEMBERSHIP is additive, not the data."""
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", research_run_id="disc-A", tier="B", score=60,
    ))
    db.upsert_prospect(conn, ImportedProspect(
        research_domain="acme.com", research_run_id="disc-B", tier="A", score=95,
    ))

    got = db.get_prospect(conn, "acme.com")
    assert got.research_run_id == "disc-B"  # current snapshot -> newest run
    assert got.tier == "A" and got.score == 95  # data refreshed to newest import
    assert got.research_run_ids == ["disc-A", "disc-B"]  # but membership accumulates


def test_reimporting_same_run_id_twice_does_not_duplicate_membership(conn):
    """Re-running the SAME import (e.g. clicking 'Import qualified prospects'
    again for a run already imported) must not grow research_run_ids --
    membership is a set in spirit, even though it's stored as a list."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A", score=10))
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A", score=20))

    got = db.get_prospect(conn, "acme.com")
    assert got.research_run_ids == ["disc-A"]
    assert got.score == 20  # still refreshes the snapshot data every call


def test_prospect_imported_from_three_runs_visible_under_each(conn):
    """Not just a two-run edge case -- a prospect re-surfaced across many
    reruns must remain visible under every one of them, and under 'all
    runs', simultaneously."""
    for run_id, score in (("disc-A", 10), ("disc-B", 20), ("disc-C", 30)):
        db.upsert_prospect(conn, ImportedProspect(
            research_domain="acme.com", research_run_id=run_id, score=score,
        ))

    for run_id in ("disc-A", "disc-B", "disc-C"):
        assert [p.research_domain for p in db.list_prospects(conn, run_id=run_id)] == ["acme.com"]
    assert [p.research_domain for p in db.list_prospects(conn)] == ["acme.com"]


def test_unrelated_prospect_not_shown_under_a_run_it_was_never_imported_from(conn):
    """The fix must not make every prospect visible under every run --
    membership is still scoped to runs that actually imported it."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A"))
    db.upsert_prospect(conn, ImportedProspect(research_domain="other.com", research_run_id="disc-B"))

    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-A")] == ["acme.com"]
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-B")] == ["other.com"]


def test_delete_by_research_run_unlinks_rather_than_deletes_when_another_run_still_owns_it(conn):
    """Deleting the OLDER of two runs that both imported the same prospect
    must not hard-delete it -- it's still owned by the newer, still-live
    run -- it should just drop from that deleted run's membership and stay
    visible everywhere else."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A", score=10))
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-B", score=20))

    result = db.delete_by_research_run(conn, "disc-A")

    assert result["prospects_removed"] == 0  # unlinked, not deleted
    got = db.get_prospect(conn, "acme.com")
    assert got is not None
    assert got.research_run_ids == ["disc-B"]
    assert got.research_run_id == "disc-B"  # current-snapshot pointer repointed off the deleted run
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-A")] == []
    assert [p.research_domain for p in db.list_prospects(conn, run_id="disc-B")] == ["acme.com"]


def test_delete_by_research_run_still_hard_deletes_when_it_is_the_only_owner(conn):
    """Original single-run behavior is preserved: a prospect with exactly
    one recorded run, once that run is deleted, is a true orphan and must
    still be removed outright -- same as before this fix."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A"))

    result = db.delete_by_research_run(conn, "disc-A")

    assert result["prospects_removed"] == 1
    assert db.get_prospect(conn, "acme.com") is None


def test_delete_by_research_run_last_remaining_run_after_two_deletes(conn):
    """Deleting both runs a prospect was ever imported from, one at a time,
    must eventually remove it -- unlinking must not leak a phantom row that
    never gets cleaned up."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A"))
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-B"))

    first = db.delete_by_research_run(conn, "disc-A")
    assert first["prospects_removed"] == 0
    assert db.get_prospect(conn, "acme.com") is not None

    second = db.delete_by_research_run(conn, "disc-B")
    assert second["prospects_removed"] == 1
    assert db.get_prospect(conn, "acme.com") is None


def test_delete_by_research_run_campaign_and_items_still_scoped_correctly(conn):
    """The campaign/item cascade (unaffected by this fix) still works
    alongside the new prospect-unlinking behavior in the same call."""
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-A"))
    db.upsert_prospect(conn, ImportedProspect(research_domain="acme.com", research_run_id="disc-B"))
    db.create_campaign(conn, Campaign(campaign_id="camp1", name="Test", research_run_id="disc-A"))
    item = OutreachItem(item_id="item1", campaign_id="camp1", research_domain="acme.com")
    db.create_item(conn, item)

    result = db.delete_by_research_run(conn, "disc-A")

    assert result["campaigns_removed"] == 1
    assert result["items_removed"] == 1
    assert db.get_campaign(conn, "camp1") is None
    assert db.get_item(conn, "item1") is None
    # The prospect itself survives -- still owned by run B.
    assert db.get_prospect(conn, "acme.com") is not None
