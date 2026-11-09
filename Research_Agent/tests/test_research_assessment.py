from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from csg.research_assessment import (
    CompanyEvidence,
    DecisionMaker,
    ResearchAssessmentInput,
    build_assessment,
    classify_icp_categories,
    derive_readiness,
)


def test_categories_have_inclusive_overlapping_boundaries():
    categories = classify_icp_categories(
        100, employee_count_status="verified", startup_evidence_status="inferred",
        employee_count_evidence_ids=["size"], startup_evidence_ids=["stage"],
        startup_stage="bootstrapped", integrator_match=True,
        integrator_evidence_status="verified", integrator_evidence_ids=["integrator"],
    )
    assert [(row.category, row.membership) for row in categories] == [
        ("startup", "match"), ("smb", "match"), ("system_integrator", "match"),
    ]
    assert categories[0].evidence_status == "inferred"
    assert categories[0].evidence_ids == ["size", "stage"]


@pytest.mark.parametrize("count", [10, 250])
def test_startup_bounds_are_inclusive_and_accept_supported_bootstrapped_or_early_stage(count):
    startup = classify_icp_categories(
        count, employee_count_status="verified", startup_evidence_status="verified",
        employee_count_evidence_ids=["size"], startup_evidence_ids=["stage"],
        startup_stage="other early-stage",
    )[0]
    assert startup.membership == "match"
    assert startup.evidence_status == "verified"


def test_category_classifier_distinguishes_unknown_from_no_match():
    unknown = classify_icp_categories(None)
    below = classify_icp_categories(9, employee_count_status="verified",
                                    startup_evidence_status="verified", startup_stage="bootstrapped")
    assert all(row.membership == "unknown" for row in unknown)
    assert below[0].membership == "no_match"


def test_inferred_headcount_does_not_create_category_match_or_no_match():
    categories = classify_icp_categories(
        100, employee_count_status="inferred", startup_evidence_status="inferred",
        startup_stage="seed", employee_count_evidence_ids=["size"],
        startup_evidence_ids=["stage"], integrator_match=None,
    )
    assert all(row.membership == "unknown" for row in categories)


def test_explicit_company_type_does_not_rule_out_other_categories_without_evidence():
    categories = classify_icp_categories(
        None,
        explicit_company_type="startup",
        explicit_company_type_status="verified",
        explicit_company_type_evidence_ids=["type"],
    )
    assert categories[0].membership == "match"
    assert categories[1].membership == "unknown"
    assert categories[2].membership == "unknown"


def test_india_evidence_cannot_be_marked_verified_for_pakistan_openness():
    evidence = CompanyEvidence(
        evidence_id="india-role", criterion="role_link", claim={"country": "India"},
        evidence_status="verified", confidence="high", source_name="Careers",
        source_type="official_job_board", source_url="https://example.com/jobs",
    )
    with pytest.raises(ValidationError, match="must be marked inferred"):
        ResearchAssessmentInput(
            evidence=[evidence],
            hiring_model_assessments=[{
                "model": "offshore", "openness": "open", "evidence_status": "verified",
                "evidence_ids": ["india-role"],
            }],
        )


def test_hiring_model_evidence_keeps_us_latam_and_pakistan_claims_distinct():
    evidence = [
        CompanyEvidence(
            evidence_id="us-role", criterion="role_link", claim={"country": "US"},
            evidence_status="verified", confidence="high", source_name="US Careers",
            source_type="official_job_board", source_url="https://example.com/jobs/us",
        ),
        CompanyEvidence(
            evidence_id="latam-role", criterion="role_link", claim={"region": "LATAM"},
            evidence_status="verified", confidence="high", source_name="LATAM Careers",
            source_type="official_job_board", source_url="https://example.com/jobs/latam",
        ),
        CompanyEvidence(
            evidence_id="pakistan-role", criterion="role_link", claim={"country": "Pakistan"},
            evidence_status="verified", confidence="high", source_name="Pakistan Careers",
            source_type="official_job_board", source_url="https://example.com/jobs/pk",
        ),
    ]
    payload = ResearchAssessmentInput(
        evidence=evidence,
        hiring_model_assessments=[
            {"model": "onshore", "openness": "open", "evidence_status": "verified",
             "confidence": "high", "evidence_ids": ["us-role"]},
            {"model": "nearshore", "openness": "open", "evidence_status": "verified",
             "confidence": "high", "evidence_ids": ["latam-role"]},
            {"model": "offshore", "openness": "open", "evidence_status": "verified",
             "confidence": "high", "evidence_ids": ["pakistan-role"]},
        ],
    )

    assert [(row.model, row.openness, row.regional_hiring,
             row.regional_hiring_evidence_status, row.regional_hiring_evidence_ids[0])
            for row in payload.hiring_model_assessments] == [
        ("onshore", "unknown", "open", "verified", "us-role"),
        ("nearshore", "unknown", "open", "verified", "latam-role"),
        ("offshore", "unknown", "open", "verified", "pakistan-role"),
    ]


def test_verified_india_source_can_support_only_an_inferred_pakistan_conclusion():
    evidence = CompanyEvidence(
        evidence_id="india-role", criterion="role_link", claim={"country": "India"},
        evidence_status="verified", confidence="high", source_name="India Careers",
        source_type="official_job_board", source_url="https://example.com/jobs/in",
    )
    payload = ResearchAssessmentInput(
        evidence=[evidence],
        hiring_model_assessments=[{
            "model": "offshore", "openness": "open", "evidence_status": "inferred",
            "confidence": "low", "evidence_ids": ["india-role"],
        }],
    )

    assert payload.evidence[0].evidence_status == "verified"
    assert payload.hiring_model_assessments[0].openness == "unknown"
    assert payload.hiring_model_assessments[0].evidence_status == "unavailable"
    assert payload.hiring_model_assessments[0].regional_hiring == "open"
    assert payload.hiring_model_assessments[0].regional_hiring_evidence_status == "inferred"


def test_regional_job_location_cannot_establish_external_staffing_openness():
    location = CompanyEvidence(
        evidence_id="us-role", criterion="role_link", claim={"country": "US"},
        evidence_status="verified", confidence="high", source_name="Careers",
        source_type="official_job_board", source_url="https://example.com/jobs/us",
    )

    with pytest.raises(ValidationError, match="explicit staffing/vendor evidence"):
        ResearchAssessmentInput(
            evidence=[location],
            hiring_model_assessments=[{
                "regional_hiring": "unknown",
                "model": "onshore", "openness": "open", "evidence_status": "verified",
                "evidence_ids": ["us-role"],
            }],
        )


def test_external_staffing_openness_requires_cited_vendor_evidence():
    vendor_evidence = CompanyEvidence(
        evidence_id="vendor-use", criterion="external_staffing",
        claim={"external_staffing": True, "vendor_type": "staffing agency"},
        evidence_status="verified", confidence="high", source_name="Supplier policy",
        source_type="official_company", source_url="https://example.com/suppliers",
    )
    payload = ResearchAssessmentInput(
        evidence=[vendor_evidence],
        hiring_model_assessments=[{
            "regional_hiring": "unknown",
            "model": "onshore", "openness": "open", "evidence_status": "verified",
            "evidence_ids": ["vendor-use"],
        }],
    )

    assert payload.hiring_model_assessments[0].openness == "open"


def test_hunter_outcome_requires_timestamp_and_does_not_imply_validity():
    with pytest.raises(ValidationError, match="hunter_checked_at"):
        DecisionMaker(name="A Person", title="CTO", persona="technical_executive",
                      hunter_outcome="accept_all")
    with pytest.raises(ValidationError, match="checked email"):
        DecisionMaker(
            name="A Person", title="CTO", persona="technical_executive",
            hunter_outcome="accept_all", hunter_checked_at=datetime.now(timezone.utc),
        )
    person = DecisionMaker(
        name="A Person", title="CTO", persona="technical_executive",
        hunter_outcome="accept_all", hunter_email="a@example.com",
        hunter_checked_at=datetime.now(timezone.utc),
    )
    assert person.hunter_outcome == "accept_all"
    assert person.linkedin_validation == "not_validated"


def test_rejected_disposition_requires_a_reason():
    with pytest.raises(ValidationError, match="rejection_reason"):
        ResearchAssessmentInput(prospecting_disposition="rejected")
    payload = ResearchAssessmentInput(
        prospecting_disposition="rejected", rejection_reason="Not a prospect",
    )
    assert derive_readiness(payload) == "rejected"


def _readiness_payload(official_roles: bool = True, complete_people: bool = True):
    now = datetime.now(timezone.utc)
    source_type = "official_job_board" if official_roles else "aggregator"
    evidence = [CompanyEvidence(
        evidence_id=f"role-{index}", criterion="role_link", claim={"current": True},
        evidence_status="verified", confidence="high", source_name="Company careers",
        source_type=source_type, source_url=f"https://example.com/jobs/{index}",
        retrieved_at=now,
    ) for index in range(3)]
    evidence.extend([
        CompanyEvidence(evidence_id="site", criterion="website", claim={"url": "https://example.com"},
                        evidence_status="verified", confidence="high", source_name="Site",
                        source_type="official_company", source_url="https://example.com", retrieved_at=now),
        CompanyEvidence(evidence_id="hq", criterion="us_hq", claim={"country": "US"},
                        evidence_status="verified", confidence="high", source_name="Site",
                        source_type="official_company", source_url="https://example.com/about", retrieved_at=now),
        CompanyEvidence(evidence_id="size", criterion="company_size", claim={"employees": 100},
                        evidence_status="verified", confidence="medium", source_name="Profile",
                        source_type="third_party", source_url="https://example.com/profile", retrieved_at=now),
        CompanyEvidence(evidence_id="opening", criterion="csg_relevant_opening", claim={"practice": "ai_ml"},
                        evidence_status="verified", confidence="high", source_name="Careers",
                        source_type="official_job_board", source_url="https://example.com/jobs/ai", retrieved_at=now),
        CompanyEvidence(evidence_id="model", criterion="nearshore_model", claim={"country": "Mexico"},
                        evidence_status="verified", confidence="high", source_name="Careers",
                        source_type="official_job_board", source_url="https://example.com/jobs/mx", retrieved_at=now),
        CompanyEvidence(evidence_id="fit", criterion="service_fit", claim={"fit": "AI/ML hiring"},
                        evidence_status="inferred", confidence="medium", source_name="Analysis",
                        source_type="other", source_url="https://example.com/jobs/ai", retrieved_at=now),
    ])
    criteria = [
        {"criterion": criterion, "result": "met", "evidence_ids": ids}
        for criterion, ids in [
            ("website", ["site"]), ("us_hq", ["hq"]), ("company_size", ["size"]),
            ("role_links", [f"role-{index}" for index in range(3)]),
            ("csg_relevant_opening", ["opening"]), ("decision_maker", ["official-0"]),
            ("hiring_model", ["model"]), ("service_fit", ["fit"]),
        ]
    ]
    people = []
    for index in range(2 if complete_people else 1):
        name = f"Leader {index}"
        title = "CTO"
        evidence.extend([
            CompanyEvidence(
                evidence_id=f"linkedin-{index}", criterion="linkedin_validation",
                claim={"name": name, "title": title}, evidence_status="verified",
                confidence="high", source_name="LinkedIn", source_type="linkedin",
                source_url=f"https://linkedin.com/in/example-{index}", retrieved_at=now,
            ),
            CompanyEvidence(
                evidence_id=f"official-{index}", criterion="leadership",
                claim={"name": name, "title": title}, evidence_status="verified",
                confidence="high", source_name="Leadership page", source_type="official_company",
                source_url=f"https://example.com/about/{index}", retrieved_at=now,
            ),
        ])
        people.append({
            "name": name, "title": title, "persona": "technical_executive",
            "linkedin_validation": "validated" if complete_people else "not_validated",
            "official_site_validation": "validated" if complete_people else "not_validated",
            "linkedin_evidence_ids": [f"linkedin-{index}"],
            "official_site_evidence_ids": [f"official-{index}"],
            "evidence_ids": [f"official-{index}"],
            "hunter_outcome": "unverified" if complete_people else None,
            "hunter_email": f"leader{index}@example.com" if complete_people else None,
            "hunter_checked_at": now if complete_people else None,
        })
    return ResearchAssessmentInput(
        evidence=evidence,
        criteria_assessments=criteria,
        hiring_model_assessments=[{
            "model": "nearshore", "openness": "open", "evidence_status": "verified",
            "evidence_ids": ["model"],
        }],
        decision_makers=people,
    )


def test_readiness_requires_all_criteria_and_dated_decision_maker_evidence():
    payload = _readiness_payload(complete_people=False)
    assert derive_readiness(payload) == "contact_enrichment_pending"
    payload.criteria_assessments = [row for row in payload.criteria_assessments
                                    if row.criterion != "service_fit"]
    assert derive_readiness(payload) == "unverified_pipeline"


def test_fully_researched_needs_official_roles_two_validated_people_and_recorded_hunter_results():
    payload = _readiness_payload()
    assert derive_readiness(payload) == "fully_researched"
    payload.decision_makers[0].hunter_outcome = None
    payload.decision_makers[0].hunter_checked_at = None
    assert derive_readiness(payload) == "contact_enrichment_pending"


def test_third_party_roles_cannot_satisfy_fully_researched_but_can_be_pending():
    payload = _readiness_payload(official_roles=False)
    assert derive_readiness(payload) == "contact_enrichment_pending"


def test_build_assessment_uses_server_derived_readiness_and_version():
    payload = _readiness_payload()
    assessed = build_assessment("example.com", payload, assessment_version=3)
    assert assessed.domain == "example.com"
    assert assessed.assessment_version == 3
    assert assessed.readiness_status == "fully_researched"