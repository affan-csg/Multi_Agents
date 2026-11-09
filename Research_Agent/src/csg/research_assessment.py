"""Research-owned company targeting, evidence, and readiness contract."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

EvidenceStatus = Literal["verified", "inferred", "unavailable"]
Confidence = Literal["high", "medium", "low"]
IcpCategory = Literal["startup", "smb", "system_integrator"]
Membership = Literal["match", "no_match", "unknown", "conflicted"]
HiringModel = Literal["onshore", "nearshore", "offshore"]
Openness = Literal["open", "not_open", "mixed", "unknown"]
Criterion = Literal[
    "website", "us_hq", "company_size", "role_links", "csg_relevant_opening",
    "decision_maker", "hiring_model", "service_fit",
]
CriterionResult = Literal["met", "not_met", "unknown"]
ResearchReadiness = Literal[
    "unverified_pipeline", "contact_enrichment_pending", "fully_researched", "rejected",
]
ProspectingDisposition = Literal["eligible", "needs_review", "excluded_existing_client", "rejected"]
SourceType = Literal[
    "official_company", "official_job_board", "linkedin", "third_party", "aggregator", "other",
]
ValidationState = Literal["validated", "not_validated", "unavailable", "conflicted"]
HunterOutcome = Literal["valid", "invalid", "accept_all", "not_found", "unverified"]
DecisionMakerPersona = Literal[
    "founder_ceo", "technical_executive", "engineering_data_leader", "talent_hr_leader",
    "delivery_leader", "practice_director", "talent_acquisition_director", "other",
]

REQUIRED_CRITERIA: tuple[Criterion, ...] = (
    "website", "us_hq", "company_size", "role_links", "csg_relevant_opening",
    "decision_maker", "hiring_model", "service_fit",
)
HIRING_MODELS: tuple[HiringModel, ...] = ("onshore", "nearshore", "offshore")
OFFICIAL_SOURCE_TYPES = {"official_company", "official_job_board"}
EXTERNAL_STAFFING_CRITERIA = {
    "external_staffing", "staffing_vendor", "contractor_usage", "outsourcing",
}


class CompanyEvidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    criterion: str
    subject: str | None = None
    claim: dict[str, Any] = Field(default_factory=dict)
    evidence_status: EvidenceStatus
    confidence: Confidence
    source_name: str | None = None
    source_type: SourceType | None = None
    source_url: str | None = None
    source_published_at: datetime | None = None
    retrieved_at: datetime | None = None
    excerpt: str | None = None
    conflict_group: str | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_source_state(self) -> "CompanyEvidence":
        if self.evidence_status == "unavailable":
            if not self.unavailable_reason:
                raise ValueError("unavailable evidence requires unavailable_reason")
            if any(value is not None for value in (self.source_name, self.source_type, self.source_url)):
                raise ValueError("unavailable evidence cannot claim a source")
        elif not self.source_name or not self.source_type or not self.source_url:
            raise ValueError("verified and inferred evidence require source name, type, and URL")
        return self


class CategoryAssessment(BaseModel):
    category: IcpCategory
    membership: Membership
    evidence_status: EvidenceStatus
    evidence_ids: list[str] = Field(default_factory=list)


class HiringModelAssessment(BaseModel):
    model: HiringModel
    openness: Openness
    regional_hiring: Openness = "unknown"
    evidence_status: EvidenceStatus
    confidence: Confidence | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    regional_hiring_evidence_status: EvidenceStatus = "unavailable"
    regional_hiring_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_regional_meaning(cls, value: Any) -> Any:
        if isinstance(value, dict) and "regional_hiring" not in value:
            value = dict(value)
            value["regional_hiring"] = value.get("openness", "unknown")
            value["regional_hiring_evidence_status"] = value.get("evidence_status", "unavailable")
            value["regional_hiring_evidence_ids"] = value.get("evidence_ids", [])
            value["openness"] = "unknown"
            value["evidence_status"] = "unavailable"
            value["evidence_ids"] = []
        return value


class CriterionAssessment(BaseModel):
    criterion: Criterion
    result: CriterionResult
    evidence_ids: list[str] = Field(default_factory=list)
    notes: str | None = None


class DecisionMaker(BaseModel):
    decision_maker_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    title: str
    persona: DecisionMakerPersona
    linkedin_validation: ValidationState = "not_validated"
    official_site_validation: ValidationState = "not_validated"
    linkedin_evidence_ids: list[str] = Field(default_factory=list)
    official_site_evidence_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    hunter_outcome: HunterOutcome | None = None
    hunter_email: str | None = None
    hunter_checked_at: datetime | None = None

    @model_validator(mode="after")
    def hunter_result_is_not_assumed(self) -> "DecisionMaker":
        if self.hunter_outcome is not None and self.hunter_checked_at is None:
            raise ValueError("a recorded Hunter outcome requires hunter_checked_at")
        if self.hunter_outcome == "not_found" and self.hunter_email:
            raise ValueError("not_found cannot include a Hunter email")
        if self.hunter_outcome not in (None, "not_found") and not self.hunter_email:
            raise ValueError("a Hunter outcome requires the checked email address")
        return self


class ResearchAssessmentInput(BaseModel):
    prospecting_disposition: ProspectingDisposition = "needs_review"
    rejection_reason: str | None = None
    category_assessments: list[CategoryAssessment] = Field(default_factory=list)
    hiring_model_assessments: list[HiringModelAssessment] = Field(default_factory=list)
    criteria_assessments: list[CriterionAssessment] = Field(default_factory=list)
    decision_makers: list[DecisionMaker] = Field(default_factory=list)
    evidence: list[CompanyEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references_and_unique_rows(self) -> "ResearchAssessmentInput":
        if self.prospecting_disposition == "rejected" and not self.rejection_reason:
            raise ValueError("rejected disposition requires rejection_reason")
        collections = (
            (self.category_assessments, "category", "category"),
            (self.hiring_model_assessments, "model", "hiring model"),
            (self.criteria_assessments, "criterion", "criterion"),
        )
        for rows, key, label in collections:
            values = [getattr(row, key) for row in rows]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} assessments are not allowed")

        evidence_ids = [row.evidence_id for row in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id values must be unique within an assessment")
        known_ids = set(evidence_ids)
        for row in [*self.category_assessments, *self.hiring_model_assessments,
                    *self.criteria_assessments, *self.decision_makers]:
            refs = set(row.evidence_ids)
            if isinstance(row, DecisionMaker):
                refs.update(row.linkedin_evidence_ids)
                refs.update(row.official_site_evidence_ids)
            if not refs <= known_ids:
                raise ValueError("assessment references evidence not included in this assessment")
        evidence_by_id = {e.evidence_id: e for e in self.evidence}
        for row in self.category_assessments:
            if row.membership == "match" and not row.evidence_ids:
                raise ValueError("category membership match requires linked evidence")
        for person in self.decision_makers:
            if person.linkedin_validation == "validated":
                linked = [evidence_by_id[i] for i in person.linkedin_evidence_ids]
                if not linked or not all(
                    e.evidence_status == "verified" and e.source_type == "linkedin" for e in linked
                ):
                    raise ValueError("validated LinkedIn status requires verified LinkedIn evidence")
                if not any(
                    e.claim.get("name") == person.name and e.claim.get("title") == person.title
                    for e in linked
                ):
                    raise ValueError("LinkedIn evidence must substantiate the decision-maker name and title")
            if person.official_site_validation == "validated":
                linked = [evidence_by_id[i] for i in person.official_site_evidence_ids]
                if not linked or not all(
                    e.evidence_status == "verified" and e.source_type == "official_company" for e in linked
                ):
                    raise ValueError("validated official-site status requires verified official-company evidence")
                if not any(
                    e.claim.get("name") == person.name and e.claim.get("title") == person.title
                    for e in linked
                ):
                    raise ValueError("official-site evidence must substantiate the decision-maker name and title")
        for row in self.hiring_model_assessments:
            openness_evidence = [e for e in self.evidence if e.evidence_id in row.evidence_ids]
            regional_evidence = [e for e in self.evidence if e.evidence_id in row.regional_hiring_evidence_ids]
            if row.openness == "open":
                if not openness_evidence or any(e.evidence_status == "unavailable" for e in openness_evidence):
                    raise ValueError("open hiring-model assessments require usable linked evidence")
                if not any(
                    evidence.criterion in EXTERNAL_STAFFING_CRITERIA
                    or evidence.claim.get("external_staffing") is True
                    or evidence.claim.get("staffing_vendor") is True
                    or evidence.claim.get("contractor_usage") is True
                    or evidence.claim.get("outsourcing") is True
                    for evidence in openness_evidence
                ):
                    raise ValueError("external staffing openness requires explicit staffing/vendor evidence")
                if row.evidence_status == "verified" and any(
                    e.evidence_status != "verified" for e in openness_evidence
                ):
                    raise ValueError("verified hiring-model openness requires verified linked evidence")
            if row.regional_hiring == "open":
                if not regional_evidence or any(e.evidence_status == "unavailable" for e in regional_evidence):
                    raise ValueError("regional hiring requires usable linked evidence")
                if row.regional_hiring_evidence_status == "verified" and any(
                    e.evidence_status != "verified" for e in regional_evidence
                ):
                    raise ValueError("verified regional hiring requires verified linked evidence")
            if row.model == "offshore" and (row.openness == "open" or row.regional_hiring == "open"):
                claims = [json.dumps(e.claim, default=str).lower()
                          for e in [*openness_evidence, *regional_evidence]]
                india_only = any("india" in claim for claim in claims) and not any(
                    "pakistan" in claim for claim in claims
                )
                if india_only and (
                    (row.openness == "open" and row.evidence_status != "inferred")
                    or (row.regional_hiring == "open" and row.regional_hiring_evidence_status != "inferred")
                ):
                    raise ValueError("India-to-Pakistan openness must be marked inferred")
        self._validate_met_criteria(known_ids)
        return self

    def _validate_met_criteria(self, known_ids: set[str]) -> None:
        evidence_by_id = {e.evidence_id: e for e in self.evidence}
        criteria = {c.criterion: c for c in self.criteria_assessments}
        for criterion in REQUIRED_CRITERIA:
            row = criteria.get(criterion)
            if row is None or row.result != "met":
                continue
            linked = [evidence_by_id[i] for i in row.evidence_ids if i in known_ids]
            if criterion == "role_links":
                current = [e for e in linked if e.criterion == "role_link" and e.claim.get("current") is True]
                if len(current) < 3:
                    raise ValueError("role_links met requires at least three current role-link evidence records")
            elif criterion == "decision_maker":
                has_dated_person = any(
                    person.name and person.title and any(
                        evidence_id in known_ids and (
                            evidence_by_id[evidence_id].source_published_at is not None
                            or evidence_by_id[evidence_id].retrieved_at is not None
                        ) for evidence_id in person.evidence_ids
                    ) for person in self.decision_makers
                )
                if not has_dated_person:
                    raise ValueError("decision_maker met requires a named person with dated evidence")
            elif not row.evidence_ids:
                raise ValueError(f"{criterion} met requires linked evidence")
        hiring = criteria.get("hiring_model")
        if hiring and hiring.result == "met":
            supported_open_model = any(
                model.regional_hiring == "open" and model.regional_hiring_evidence_ids
                for model in self.hiring_model_assessments
            )
            all_unavailable = {
                model.model for model in self.hiring_model_assessments
                if model.regional_hiring == "unknown" and model.regional_hiring_evidence_status == "unavailable"
            } == set(HIRING_MODELS)
            if not supported_open_model and not all_unavailable:
                raise ValueError("hiring_model met requires supported regional hiring or all models unavailable")


class CompanyResearchAssessment(ResearchAssessmentInput):
    assessment_id: str
    domain: str
    assessment_version: int
    schema_version: int = 1
    source_run_id: str | None = None
    readiness_status: ResearchReadiness
    assessed_at: datetime


def classify_icp_categories(
    employee_count: int | None,
    *,
    employee_count_status: EvidenceStatus = "unavailable",
    employee_count_evidence_ids: list[str] | None = None,
    startup_evidence_status: EvidenceStatus = "unavailable",
    startup_evidence_ids: list[str] | None = None,
    startup_stage: str | None = None,
    integrator_match: bool | None = None,
    integrator_evidence_status: EvidenceStatus = "unavailable",
    integrator_evidence_ids: list[str] | None = None,
    explicit_company_type: str | None = None,
    explicit_company_type_status: EvidenceStatus = "unavailable",
    explicit_company_type_evidence_ids: list[str] | None = None,
    category_findings: dict[str, tuple[Membership, EvidenceStatus, list[str]]] | None = None,
) -> list[CategoryAssessment]:
    """Return independent, inclusive segment assessments; unknown is not no-match."""
    out: list[CategoryAssessment] = []
    size_evidence = employee_count_evidence_ids or []
    stage_evidence = startup_evidence_ids or []
    integrator_evidence = integrator_evidence_ids or []
    explicit_type_evidence = explicit_company_type_evidence_ids or []
    explicit_type = (explicit_company_type or "").strip().lower().replace(" ", "_")

    def size_membership(low: int, high: int) -> Membership:
        if employee_count is None or employee_count_status != "verified":
            return "unknown"
        return "match" if low <= employee_count <= high else "no_match"

    def combined_status(*statuses: EvidenceStatus) -> EvidenceStatus:
        if "unavailable" in statuses:
            return "unavailable"
        return "inferred" if "inferred" in statuses else "verified"

    startup_size = size_membership(10, 250)
    stage = (startup_stage or "").strip().lower().replace(" ", "_").replace("-", "_")
    early_stages = {
        "bootstrapped", "pre_seed", "seed", "series_a", "series_b", "series_c",
        "other_early_stage",
    }
    if startup_size == "no_match":
        startup_membership: Membership = "no_match"
        startup_status = employee_count_status
    elif startup_size == "unknown" or not stage or startup_evidence_status == "unavailable":
        startup_membership = "unknown"
        startup_status = "unavailable"
    elif stage not in early_stages:
        startup_membership = "no_match"
        startup_status = startup_evidence_status
    else:
        startup_membership = "match"
        startup_status = combined_status(employee_count_status, startup_evidence_status)
    out.append(CategoryAssessment(
        category="startup", membership=startup_membership, evidence_status=startup_status,
        evidence_ids=sorted(set(size_evidence + stage_evidence)),
    ))

    smb = size_membership(50, 500)
    out.append(CategoryAssessment(
        category="smb", membership=smb,
        evidence_status=employee_count_status if smb != "unknown" else "unavailable",
        evidence_ids=size_evidence,
    ))

    if integrator_match is False:
        integrator: Membership = "no_match"
        integrator_status = integrator_evidence_status
    elif integrator_match is None or integrator_evidence_status == "unavailable":
        integrator = "unknown"
        integrator_status = "unavailable"
    else:
        integrator = "match"
        integrator_status = combined_status(employee_count_status, integrator_evidence_status)
    out.append(CategoryAssessment(
        category="system_integrator", membership=integrator,
        evidence_status=integrator_status,
        evidence_ids=sorted(set(size_evidence + integrator_evidence)),
    ))
    if category_findings:
        out = [row.model_copy(update={
            "membership": category_findings[row.category][0],
            "evidence_status": category_findings[row.category][1],
            "evidence_ids": sorted(set(row.evidence_ids + category_findings[row.category][2])),
        }) if row.category in category_findings else row for row in out]
    if explicit_type in {"startup", "smb", "system_integrator"}:
        for index, row in enumerate(out):
            if row.category == explicit_type and row.category not in (category_findings or {}):
                out[index] = row.model_copy(update={
                    "membership": "match",
                    "evidence_status": explicit_company_type_status,
                    "evidence_ids": sorted(set(row.evidence_ids + explicit_type_evidence)),
                })
    return out


def derive_readiness(assessment: ResearchAssessmentInput, assessed_at: datetime | None = None) -> ResearchReadiness:
    """Derive research-completeness milestone; Hunter outcomes are not verification."""
    if assessment.prospecting_disposition in {"excluded_existing_client", "rejected"}:
        return "rejected"

    criteria = {row.criterion: row for row in assessment.criteria_assessments}
    if any(criteria.get(name) is None or criteria[name].result != "met" for name in REQUIRED_CRITERIA):
        return "unverified_pipeline"

    evidence_by_id = {row.evidence_id: row for row in assessment.evidence}
    role_links = [e for e in assessment.evidence
                  if e.criterion == "role_link" and e.claim.get("current") is True]
    dated_decision_maker = any(
        person.name and person.title and any(
            evidence_id in evidence_by_id and (
                evidence_by_id[evidence_id].source_published_at is not None
                or evidence_by_id[evidence_id].retrieved_at is not None
            ) for evidence_id in person.evidence_ids
        ) for person in assessment.decision_makers
    )
    if len(role_links) < 3 or not dated_decision_maker:
        return "unverified_pipeline"

    fully_researched = len([e for e in role_links if e.source_type in OFFICIAL_SOURCE_TYPES]) >= 3
    qualified_people = [person for person in assessment.decision_makers
                        if person.linkedin_validation == "validated"
                        and person.official_site_validation == "validated"
                        and person.linkedin_evidence_ids
                        and person.official_site_evidence_ids
                        and person.hunter_outcome is not None]
    if fully_researched and len(qualified_people) >= 2:
        return "fully_researched"
    return "contact_enrichment_pending"


def build_assessment(
    domain: str,
    payload: ResearchAssessmentInput,
    *,
    assessment_version: int,
    source_run_id: str | None = None,
    assessment_id: str | None = None,
    assessed_at: datetime | None = None,
) -> CompanyResearchAssessment:
    assessed_at = assessed_at or datetime.now(timezone.utc)
    return CompanyResearchAssessment(
        **payload.model_dump(mode="python"),
        assessment_id=assessment_id or str(uuid.uuid4()),
        domain=domain,
        assessment_version=assessment_version,
        source_run_id=source_run_id,
        readiness_status=derive_readiness(payload, assessed_at),
        assessed_at=assessed_at,
    )