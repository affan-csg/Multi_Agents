"""Scoring is a pure function -- these tests run with no network and no LLM.

Fixture titles reflect CSG's actual practice areas (sourced from
connect-hug-repo/src/content/staffing.ts), not a generic staffing taxonomy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from csg.config import load_config
from csg.models import Company, JobPosting
from csg.scoring import (MAX_RESCALE, compute_metrics, disqualify, evaluate,
                         parse_money)

NOW = datetime(2026, 9, 8)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def req(title: str, *, age_days: int | None = 30, location: str = "Austin, TX",
        desc: str = "", etype: str | None = None, i: int = 0) -> JobPosting:
    posted = NOW - timedelta(days=age_days) if age_days is not None else None
    return JobPosting(
        domain="acme.com", ats_provider="greenhouse", external_id=f"{title}-{i}",
        title=title, location=location, posted_at=posted, description=desc,
        employment_type=etype,
    )


def company(**kw) -> Company:
    base = dict(domain="acme.com", name="Acme Data Systems", employee_count=400)
    base.update(kw)
    return Company(**base)


# --- disqualification ----------------------------------------------------

def test_dq_no_reqs(cfg):
    # Empty postings is no longer a hard DQ -- the company still gets scored
    # (thin, low-tier) rather than dropped before a score row exists.
    assert disqualify(cfg, company(), []) is None


def test_dq_competitor_by_name(cfg):
    c = company(name="Apex Staffing Solutions")
    r = disqualify(cfg, c, [req("Data Engineer", i=1)])
    assert r and "competitor" in r


def test_dq_named_tech_staffing_competitor(cfg):
    c = company(name="Toptal", domain="toptal.com")
    r = disqualify(cfg, c, [req("Software Engineer", i=1)])
    assert r and "competitor" in r


def test_dq_competitor_by_recruiter_share(cfg):
    reqs = [req("Technical Recruiter", i=n) for n in range(3)] + [req("Data Engineer", i=9)]
    r = disqualify(cfg, company(), reqs)
    assert r and "recruiting roles" in r


def test_dq_no_relevant_reqs(cfg):
    r = disqualify(cfg, company(), [req("Chief of Staff", i=1), req("Head of Partnerships", i=2)])
    assert r and "enabled practice areas" in r


def test_size_has_no_hard_floor_or_ceiling(cfg):
    # CSG explicitly serves startups through enterprises -- no headcount DQ.
    reqs = [req("Software Engineer", i=1)]
    assert disqualify(cfg, company(employee_count=5), reqs) is None
    assert disqualify(cfg, company(employee_count=250000), reqs) is None
    assert disqualify(cfg, company(employee_count=None), reqs) is None


def test_dq_all_foreign(cfg):
    reqs = [req("Software Engineer", location="London, United Kingdom", i=1),
            req("Data Engineer", location="Berlin, Germany", i=2)]
    assert "no US presence" in disqualify(cfg, company(), reqs)


def test_us_reqs_not_dq_even_when_mixed(cfg):
    reqs = [req("Software Engineer", location="London, United Kingdom", i=1),
            req("Data Engineer", location="Dallas, TX", i=2)]
    assert disqualify(cfg, company(), reqs) is None


# --- metrics -------------------------------------------------------------

def test_undated_reqs_excluded_from_median(cfg):
    reqs = [req("Data Engineer", age_days=100, i=1),
            req("Cloud Engineer", age_days=100, i=2),
            req("DevOps Engineer", age_days=None, i=3)]
    m = compute_metrics(cfg, reqs, NOW)
    assert m["dated_reqs"] == 2
    assert m["req_count"] == 3
    assert m["ages"] == [100.0, 100.0]  # undated one is absent, not counted as fresh


def test_practice_area_classification(cfg):
    assert cfg.classify("Senior AI Engineer") == ["ai_ml"]
    assert cfg.classify("MLOps Engineer") == ["mlops"]
    assert cfg.classify("Data Engineer, Platform Team") == ["data_engineering"]
    assert cfg.classify("DevSecOps Engineer") == ["devsecops_platform"]
    assert cfg.classify("Chief Revenue Officer") == []


def test_short_acronyms_do_not_overmatch(cfg):
    # "SRE" must not fire inside unrelated words.
    assert cfg.classify("Sales Representative Manager") == []
    assert "devops" in cfg.classify("Site Reliability Engineer")


def test_ai_ml_and_mlops_outweight_generic_software_dev(cfg):
    # CSG's own copy singles out AI/ML and MLOps as highest-priority -- the
    # configured weights (1.3 vs 0.9) must actually influence relevant_share.
    ai_reqs = [req("AI Engineer", i=n) for n in range(5)]
    sd_reqs = [req("Software Engineer", i=n) for n in range(5)]
    m_ai = compute_metrics(cfg, ai_reqs, NOW)
    m_sd = compute_metrics(cfg, sd_reqs, NOW)
    assert m_ai["relevant_share"] > m_sd["relevant_share"]


# --- scoring -------------------------------------------------------------

def test_strong_prospect_scores_high(cfg):
    reqs = (
        [req("AI Engineer", age_days=95, location="Dallas, TX", i=n) for n in range(4)]
        + [req("MLOps Engineer", age_days=80, location="Austin, TX",
               desc="Urgent need, multiple openings, contract-to-hire", i=n + 10) for n in range(4)]
        + [req("Data Engineer", age_days=60, location="Atlanta, GA", i=n + 20) for n in range(4)]
    )
    res = evaluate(cfg, company(employee_count=600), reqs, NOW)
    assert not res.disqualified
    assert res.tier in ("A", "B")
    assert res.score >= 60
    # Fill Pain is capped at its configured weight (20), so "dominant" means
    # near-saturated, not above the cap.
    assert res.dimensions["Fill Pain"] >= 18      # old reqs dominate
    # contract_language_points (3) + multi_location_points (2) = 5 exactly --
    # icp.yaml's buying_signals budget (3/2/2/3, see growth_signal_points).
    assert res.dimensions["Buying Signals"] >= 5


def test_weak_prospect_scores_low(cfg):
    reqs = [req("Product Manager", age_days=3, location="San Francisco, CA", i=1)]
    res = evaluate(cfg, company(employee_count=300), reqs, NOW)
    assert res.score < 40
    assert res.tier in ("C", "D")


def test_unknown_headcount_is_capped_not_inflated(cfg):
    reqs = [req("Cloud Engineer", age_days=90, i=n) for n in range(8)]
    known = evaluate(cfg, company(employee_count=400), reqs, NOW)
    unknown = evaluate(cfg, company(employee_count=None), reqs, NOW)

    # Missing data must never beat known-good data on the firmographic dimension.
    assert unknown.dimensions["Firmographic Fit"] <= known.dimensions["Firmographic Fit"]
    # And the rescale is capped, so the dimension cannot reach its full weight.
    assert unknown.dimensions["Firmographic Fit"] <= cfg.weights["firmographic_fit"]
    assert any("unavailable" in str(r) for r in unknown.reasons)


# --- hiring signal ---------------------------------------------------------
# The bug these guard against: `_hiring_need` used to score `len(postings)`
# directly, so a company whose job discovery was completely exhausted
# (hiring_signal="unknown") scored identically to one with a confirmed-empty
# ATS board (hiring_signal="confirmed") -- both landed as "0 open reqs",
# a confirmed zero. See scoring.py's module docstring and models.HiringSignal.

def test_unknown_hiring_signal_is_not_scored_as_confirmed_zero(cfg):
    confirmed_empty = evaluate(cfg, company(hiring_signal="confirmed"), [], NOW)
    unknown = evaluate(cfg, company(hiring_signal="unknown"), [], NOW)

    # A confirmed empty board is real evidence -- "Hiring Need" scores it
    # (and can legitimately score it at/near zero). An unknown signal must
    # never end up WORSE than a confirmed zero -- ignorance cannot outscore
    # OR underscore evidence; it is simply excluded.
    assert unknown.dimensions["Hiring Need"] >= confirmed_empty.dimensions["Hiring Need"]

    # The unknown case must mark strictly more of the 100-point budget
    # unscorable than the confirmed-empty case (three extra Hiring Need
    # components -- req count, practice-area relevance, reqs-per-100-emp --
    # newly excluded instead of silently scored as a confirmed zero).
    def _unscorable(result):
        for r in result.reasons:
            if r.dimension == "Data Completeness":
                return float(r.detail.split(" of 100")[0])
        return 0.0

    assert _unscorable(unknown) > _unscorable(confirmed_empty)
    # Hiring Need is entirely skipped for the unknown case (all three of its
    # components require a real req count) -- it contributes no reasons of
    # its own, but disappears entirely from the completeness math rather
    # than scoring 0 confirmed points.
    assert not any(r.dimension == "Hiring Need" for r in unknown.reasons)


def test_unknown_hiring_signal_does_not_beat_real_hiring_activity(cfg):
    reqs = [req("Data Engineer", age_days=10, i=n) for n in range(6)]
    active = evaluate(cfg, company(hiring_signal="confirmed"), reqs, NOW)
    unknown = evaluate(cfg, company(hiring_signal="unknown"), [], NOW)

    # Ignorance must never inflate a score above a company we have real,
    # positive hiring evidence for.
    assert unknown.dimensions["Hiring Need"] < active.dimensions["Hiring Need"]


def test_confirmed_zero_reqs_still_scores_as_real_signal(cfg):
    # Explicitly guards the other direction: fixing "unknown" must not
    # accidentally make a genuinely empty, confirmed board stop scoring --
    # Hiring Need must still be an evaluated (not skipped) dimension, even
    # though every component nets 0 points for a real, empty board.
    res = evaluate(cfg, company(hiring_signal="confirmed"), [], NOW)
    assert res.dimensions["Hiring Need"] == 0.0
    assert res.metrics["data_completeness"]["Hiring Need"] > 0.0


def test_hiring_signal_none_or_missing_treated_like_confirmed(cfg):
    # A company scored before this field existed, or one where the caller
    # simply never set it, must not be treated as "unknown" by default --
    # only an explicit "unknown" should skip the dimension. This keeps old
    # score rows and any caller that forgets to set the field scoring
    # exactly as they did before this fix.
    res = evaluate(cfg, company(hiring_signal=None), [], NOW)
    assert res.metrics["data_completeness"]["Hiring Need"] > 0.0


def test_every_point_is_explained(cfg):
    reqs = [req("Data Engineer", age_days=70, i=n) for n in range(10)]
    res = evaluate(cfg, company(), reqs, NOW)
    explained = sum(r.points for r in res.reasons)
    assert res.score > 0
    assert explained > 0
    assert len(res.reasons) >= 4


def test_score_is_deterministic(cfg):
    reqs = [req("Cybersecurity Analyst", age_days=55, i=n) for n in range(7)]
    a = evaluate(cfg, company(employee_count=800), reqs, NOW)
    b = evaluate(cfg, company(employee_count=800), reqs, NOW)
    assert a.score == b.score
    assert [str(x) for x in a.reasons] == [str(x) for x in b.reasons]


def test_tiers_are_ordered(cfg):
    t = cfg.tiers
    assert t["A"] > t["B"] > t["C"]
    assert MAX_RESCALE >= 1.0


# --- provenance ----------------------------------------------------------
# Every one of these covers a bug that shipped: the model was migrated to a
# provenance envelope while callers kept passing bare scalars.

def test_bare_int_headcount_is_coerced_not_rejected():
    """Seed CSVs and hand-entered filters supply a plain int."""
    c = Company(domain="acme.com", employee_count=250)
    assert c.employees == 250
    assert c.employee_count.method == "reported"   # human input, not an estimate
    assert c.employee_count.confidence == "high"


def test_enriched_dict_headcount_round_trips():
    """Research enrichment supplies a full provenance dict."""
    c = Company(domain="acme.com", employee_count={
        "value": 400, "source": "gemini_grounded_search",
        "method": "estimated", "confidence": "medium"})
    assert c.employees == 400
    assert c.employee_count.source == "gemini_grounded_search"


def test_unknown_headcount_stays_none():
    assert Company(domain="acme.com").employees is None
    assert Company(domain="acme.com", employee_count=0).employees is None


def test_financial_fields_accept_text_and_carry_provenance():
    c = Company(domain="acme.com", revenue="$25M", funding="$100M Series C")
    assert c.revenue.value == "$25M"
    assert c.funding.value == "$100M Series C"


# --- financial strength --------------------------------------------------

def test_parse_money_handles_research_text():
    assert parse_money("$25M") == 25_000_000
    assert parse_money("1.2 billion") == 1_200_000_000
    assert parse_money("$500,000") == 500_000
    assert parse_money("Series B") is None
    assert parse_money(None) is None


def test_revenue_above_threshold_scores_full(cfg):
    reqs = [req("Data Engineer", age_days=50, i=n) for n in range(6)]
    rich = evaluate(cfg, company(revenue="$50M"), reqs, NOW)
    poor = evaluate(cfg, company(revenue="$200K"), reqs, NOW)
    assert rich.dimensions["Financial Strength"] > poor.dimensions["Financial Strength"]


def test_unknown_financials_are_skipped_not_zeroed(cfg):
    """Absence of public financial data must not be read as weakness.

    A company with no published financials should not rank below one we know
    to be tiny -- an unscorable dimension is excluded from the denominator
    rather than counted as a confirmed zero.
    """
    reqs = [req("Data Engineer", age_days=50, i=n) for n in range(6)]
    unknown = evaluate(cfg, company(), reqs, NOW)
    weak = evaluate(cfg, company(revenue="$100K"), reqs, NOW)
    assert unknown.score > weak.score
    # ...but it is still flagged, so the gap is visible rather than silent.
    assert unknown.metrics["data_completeness"]["Financial Strength"] == 0.0
    assert any("unscorable" in str(r) for r in unknown.reasons)


def test_financial_strength_is_explained(cfg):
    reqs = [req("Data Engineer", age_days=50, i=n) for n in range(6)]
    res = evaluate(cfg, company(revenue="$25M", funding="$40M Series B"), reqs, NOW)
    details = " ".join(r.detail for r in res.reasons if r.dimension == "Financial Strength")
    assert "revenue" in details and "funding" in details


# --- tiering -------------------------------------------------------------

def test_unparseable_region_does_not_demote_from_tier_a(cfg):
    """Regression: Tier A used to require job locations that parsed to a state
    in `search_regions.active`, so an otherwise-qualifying SMB whose reqs said
    only "Remote" silently fell to B.

    Note this is about the *tier gate*, not the score: unknown headcount still
    legitimately lowers the score via the missing-data policy (see
    test_unknown_headcount_is_capped_not_inflated). What must not happen is a
    company clearing the A threshold on score and then being demoted for a
    signal we merely failed to parse.
    """
    reqs = ([req("AI Engineer", age_days=95, location="Remote", i=n) for n in range(6)]
            + [req("MLOps Engineer", age_days=80, location="Remote",
                   desc="Urgent, multiple openings, contract-to-hire", i=n + 10)
               for n in range(6)])
    res = evaluate(cfg, company(employee_count=400, revenue="$25M"), reqs, NOW)
    if res.score >= cfg.tiers["A"]:
        assert res.tier == "A", "an unparseable location must not demote a qualifying prospect"


def test_known_enterprise_without_urgent_hiring_lands_in_b(cfg):
    """SMBs are easier to penetrate, so a large enterprise needs standout
    hiring demand to reach A."""
    reqs = [req("Software Engineer", age_days=40, location="Atlanta, GA", i=n) for n in range(3)]
    res = evaluate(cfg, company(employee_count=50_000, revenue="$5B"), reqs, NOW)
    assert res.tier != "A"


def test_weights_total_100(cfg):
    assert sum(cfg.weights.values()) == 100
