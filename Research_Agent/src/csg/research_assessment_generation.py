"""Build and persist conservative assessments from already-collected research."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from . import db
from .db import SupabaseConn
from .models import Company, JobPosting, ProvenanceValue
from .research_assessment import (
    CategoryAssessment,
    CompanyEvidence,
    DecisionMaker,
    HiringModelAssessment,
    ResearchAssessmentInput,
    build_assessment,
    classify_icp_categories,
)

_STARTUP_STAGE = re.compile(r"\b(bootstrapped|pre[ -]?seed|seed|series [abc]|early[ -]?stage)\b", re.I)
_INTEGRATOR_CLAIM = re.compile(
    r"\b(?:we are|we're|our company is|the company is|we provide|our services include)"
    r"\b.{0,100}\b(?:systems integrator|systems integration|IT consulting|technology consulting)\b",
    re.I,
)
_LATAM_TERMS = re.compile(
    r"\b(?:LATAM|Latin America|Argentina|Bolivia|Brazil|Chile|Colombia|Costa Rica|"
    r"Dominican Republic|Ecuador|El Salvador|Guatemala|Honduras|Mexico|Nicaragua|"
    r"Panama|Paraguay|Peru|Uruguay)\b",
    re.I,
)
_US_TERMS = re.compile(r"\b(?:United States|USA|U\.S\.|US[- ]based|US only)\b", re.I)
_US_STATE_CODES = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VT|VA|WA|WV|WI|WY)\b"
)
_US_STATE_NAMES = re.compile(
    r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    r"Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    r"Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    r"Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
    r"North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    r"South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    r"Wisconsin|Wyoming)\b",
    re.I,
)
_ELIGIBILITY = re.compile(
    r"(?:remote|based|located|eligible|authorized|authorization|work from|hiring in|location)"
    r"[^.!?\n]{0,140}",
    re.I,
)
_VALID_HUNTER_OUTCOMES = {"valid", "invalid", "accept_all", "unverified"}


def _stable_id(kind: str, values: Any) -> str:
    serialized = json.dumps(values, sort_keys=True, default=str, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"csg-assessment:{kind}:{serialized}"))


def _has_url(value: str | None) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _source_type(url: str, domain: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    bare_domain = domain.lower().removeprefix("www.")
    return "official_company" if host == bare_domain or host.endswith(f".{bare_domain}") else "third_party"


def _company_fact(
    company: Company, field: str, evidence_criterion: str,
) -> CompanyEvidence | None:
    provenance: ProvenanceValue | None = getattr(company, field)
    if provenance is None or not provenance.url or not _has_url(provenance.url):
        return None
    return CompanyEvidence(
        evidence_id=_stable_id(evidence_criterion, {
            "domain": company.domain,
            "field": field,
            "value": provenance.value,
            "url": provenance.url,
            "fetched_at": provenance.fetched_at,
        }),
        criterion=f"automatic_{evidence_criterion}",
        subject=field,
        claim={field: provenance.value},
        evidence_status="verified" if provenance.method == "reported" else "inferred",
        confidence=provenance.confidence,
        source_name=provenance.source,
        source_type=_source_type(provenance.url, company.domain),
        source_url=provenance.url,
        retrieved_at=provenance.fetched_at,
    )


def _classification_evidence(company: Company) -> list[CompanyEvidence]:
    rows: list[CompanyEvidence] = []
    for item in company.classification_evidence:
        category = str(item.get("category") or "").strip().lower()
        source_url = item.get("source_url")
        fact = str(item.get("fact") or "").strip()
        why = str(item.get("why") or "").strip()
        if category not in {"startup", "smb", "system_integrator"} or not _has_url(source_url):
            continue
        if not fact or not why or not isinstance(item.get("supports"), bool):
            continue
        evidence_type = str(item.get("evidence_type") or "inferred").lower()
        if evidence_type not in {"direct", "inferred"}:
            evidence_type = "inferred"
        status = "verified" if evidence_type == "direct" else "inferred"
        rows.append(CompanyEvidence(
            evidence_id=_stable_id("classification", {
                "domain": company.domain, "category": category, "fact": fact,
                "source_url": source_url,
            }),
            criterion=f"classification_{category}",
            subject=category,
            claim={
                "category": category, "supports": item["supports"], "fact": fact,
                "why": why, "evidence_type": evidence_type,
            },
            evidence_status=status,
            confidence=item.get("confidence", "medium"),
            source_name=str(item.get("source_name") or source_url),
            source_type=_source_type(source_url, company.domain),
            source_url=source_url,
            retrieved_at=item.get("retrieved_at"),
            excerpt=f"{fact} Why: {why}",
        ))
    return rows


def _posting_source(posting: JobPosting) -> tuple[str, str, str]:
    validation = posting.raw.get("_csg_validation") or {}
    if validation.get("ownership") == "verified":
        evidence_status = "verified" if validation.get("current") == "verified" else "inferred"
        if posting.source == "ats":
            return posting.ats_provider or "company ATS", "official_job_board", evidence_status
        if posting.source == "careers_page":
            return "company careers page", "official_company", evidence_status
    return "web search result", "aggregator", "inferred"


def _retrieved_at(posting: JobPosting) -> datetime | None:
    value = (posting.raw.get("_csg_validation") or {}).get("retrieved_at")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return posting.first_seen


def _role_link_evidence(posting: JobPosting) -> CompanyEvidence | None:
    validation = posting.raw.get("_csg_validation") or {}
    if not posting.url or not _has_url(posting.url) or validation.get("ownership") != "verified":
        return None
    source_name, source_type, evidence_status = _posting_source(posting)
    return CompanyEvidence(
        evidence_id=_stable_id("role_link", {"domain": posting.domain, "url": posting.url}),
        criterion="role_link",
        subject=posting.title or None,
        claim={
            "title": posting.title,
            "current": validation.get("current") == "verified",
            "practice_area": posting.practice_area,
        },
        evidence_status=evidence_status,
        confidence="high" if evidence_status == "verified" else "low",
        source_name=source_name,
        source_type=source_type,
        source_url=posting.url,
        source_published_at=posting.posted_at,
        retrieved_at=_retrieved_at(posting),
        excerpt=validation.get("reason"),
    )


def _service_fit_evidence(posting: JobPosting) -> CompanyEvidence | None:
    if not posting.practice_area or not posting.delivery_fit or not posting.url or not _has_url(posting.url):
        return None
    validation = posting.raw.get("_csg_validation") or {}
    if validation.get("ownership") != "verified":
        return None
    source_name, source_type, _ = _posting_source(posting)
    return CompanyEvidence(
        evidence_id=_stable_id("service_fit", {"url": posting.url, "practice": posting.practice_area,
                                                "delivery_fit": posting.delivery_fit}),
        criterion="service_fit",
        subject=posting.title or None,
        claim={"practice_area": posting.practice_area, "delivery_fit": posting.delivery_fit},
        evidence_status="inferred",
        confidence="medium",
        source_name=source_name,
        source_type=source_type,
        source_url=posting.url,
        source_published_at=posting.posted_at,
        retrieved_at=_retrieved_at(posting),
        excerpt="Delivery fit is a Research classification derived from the validated role requirements.",
    )


def _regions_for_posting(posting: JobPosting) -> set[str]:
    eligibility = " ".join(_ELIGIBILITY.findall(posting.description or ""))
    text = f"{posting.location or ''} {eligibility}"
    regions: set[str] = set()
    if _US_TERMS.search(text) or _US_STATE_CODES.search(posting.location or "") or _US_STATE_NAMES.search(posting.location or ""):
        regions.add("onshore")
    if _LATAM_TERMS.search(text):
        regions.add("nearshore")
    if re.search(r"\bPakistan\b", text, re.I):
        regions.add("offshore")
    return regions


def _integrator_evidence(posting: JobPosting, domain: str) -> CompanyEvidence | None:
    if not posting.url or not _has_url(posting.url):
        return None
    match = _INTEGRATOR_CLAIM.search(posting.description or "")
    if match is None:
        return None
    source_name, source_type, evidence_status = _posting_source(posting)
    excerpt = (posting.description or "")[max(0, match.start() - 50):match.end() + 50].strip()
    claim = {"statement": match.group(0), "company_identity_claim": True}
    return CompanyEvidence(
        evidence_id=_stable_id("system_integrator", {
            "domain": domain, "url": posting.url, "claim": claim,
        }),
        criterion="automatic_system_integrator",
        claim=claim,
        evidence_status=evidence_status,
        confidence="high" if evidence_status == "verified" else "medium",
        source_name=source_name,
        source_type=source_type,
        source_url=posting.url,
        source_published_at=posting.posted_at,
        retrieved_at=_retrieved_at(posting),
        excerpt=excerpt,
    )


def _posting_location_evidence(posting: JobPosting, domain: str) -> CompanyEvidence | None:
    if not posting.url or not _has_url(posting.url):
        return None
    regions = sorted(_regions_for_posting(posting))
    if not regions:
        return None
    source_name, source_type, evidence_status = _posting_source(posting)
    claim = {
        "title": posting.title,
        "location": posting.location,
        "regions": regions,
        "current": (posting.raw.get("_csg_validation") or {}).get("current") == "verified",
    }
    return CompanyEvidence(
        evidence_id=_stable_id("role_location", {
            "domain": domain, "url": posting.url, "claim": claim,
            "posted_at": posting.posted_at, "first_seen": posting.first_seen,
        }),
        criterion="automatic_role_location",
        subject=posting.title or None,
        claim=claim,
        evidence_status=evidence_status,
        confidence="high" if evidence_status == "verified" else "medium",
        source_name=source_name,
        source_type=source_type,
        source_url=posting.url,
        source_published_at=posting.posted_at,
        retrieved_at=_retrieved_at(posting),
        excerpt=posting.location,
    )


def _startup_stage(funding: Any) -> str | None:
    if funding is None:
        return None
    match = _STARTUP_STAGE.search(str(funding))
    if match is None:
        return None
    stage = match.group(0).lower().replace(" ", "_").replace("-", "_")
    if stage.startswith("series_"):
        return stage
    if stage in {"pre_seed", "bootstrapped"}:
        return stage
    if stage == "early_stage":
        return "other_early_stage"
    return "seed"


def _persona(title: str) -> str:
    normalized = title.lower()
    if any(term in normalized for term in ("founder", "chief executive", "ceo")):
        return "founder_ceo"
    if any(term in normalized for term in ("chief technology", "cto", "chief information", "cio")):
        return "technical_executive"
    if any(term in normalized for term in ("engineering", "data leader", "vp of data")):
        return "engineering_data_leader"
    if any(term in normalized for term in ("talent", "recruit", "people", "human resources", "hr")):
        return "talent_hr_leader"
    if "delivery" in normalized:
        return "delivery_leader"
    if "practice" in normalized:
        return "practice_director"
    if "acquisition" in normalized:
        return "talent_acquisition_director"
    return "other"


def _merge_decision_makers(
    contacts: list[dict[str, Any]], previous: list[DecisionMaker],
) -> list[DecisionMaker]:
    by_email = {person.hunter_email.lower(): person for person in previous if person.hunter_email}
    for contact in contacts:
        email = str(contact.get("email") or "").strip().lower()
        first = str(contact.get("first_name") or "").strip()
        last = str(contact.get("last_name") or "").strip()
        title = str(contact.get("position") or "").strip()
        checked_at = contact.get("found_at")
        if contact.get("source") != "hunter" or not email or not first or not last or not title or not checked_at:
            continue
        outcome = str(contact.get("verification") or "unverified").lower()
        if outcome not in _VALID_HUNTER_OUTCOMES:
            outcome = "unverified"
        existing = by_email.get(email)
        data = {
            "name": f"{first} {last}",
            "title": title,
            "persona": _persona(title),
            "hunter_outcome": outcome,
            "hunter_email": email,
            "hunter_checked_at": checked_at,
        }
        if existing:
            by_email[email] = existing.model_copy(update=data)
        else:
            by_email[email] = DecisionMaker(
                decision_maker_id=_stable_id("hunter_contact", email), **data,
            )
    def priority(person: DecisionMaker) -> tuple[int, int, str]:
        title = person.title.lower()
        persona_rank = {
            "talent_hr_leader": 5,
            "talent_acquisition_director": 5,
            "founder_ceo": 4,
            "delivery_leader": 3,
            "practice_director": 3,
            "engineering_data_leader": 2,
            "technical_executive": 2,
        }.get(person.persona, 0)
        purchase_language = any(term in title for term in ("procurement", "sourcing", "staffing", "workforce"))
        verification_rank = {"valid": 3, "accept_all": 2, "unverified": 1}.get(person.hunter_outcome, 0)
        return (persona_rank + int(purchase_language) * 2, verification_rank, person.name.lower())

    return sorted(by_email.values(), key=priority, reverse=True)


def _merge_categories(
    generated: list[CategoryAssessment], previous: dict[str, CategoryAssessment],
) -> list[CategoryAssessment]:
    out = []
    for row in generated:
        old = previous.get(row.category)
        if old is None:
            out.append(row)
        elif old.membership == "conflicted":
            out.append(old)
        elif old.membership == "no_match" and old.evidence_status != "verified":
            out.append(row)
        elif row.membership == "unknown" and old.membership != "unknown":
            out.append(old)
        elif (old.membership not in {"unknown", row.membership}
              and old.evidence_ids and row.evidence_ids):
            out.append(CategoryAssessment(
                category=row.category,
                membership="conflicted",
                evidence_status="inferred" if "inferred" in (old.evidence_status, row.evidence_status) else "verified",
                evidence_ids=sorted(set(old.evidence_ids + row.evidence_ids)),
            ))
        else:
            out.append(row)
    return out


def _merge_hiring_models(
    generated: list[HiringModelAssessment], previous: dict[str, HiringModelAssessment],
) -> list[HiringModelAssessment]:
    out: list[HiringModelAssessment] = []
    for row in generated:
        old = previous.get(row.model)
        if old is None:
            out.append(row)
        else:
            regional = row.regional_hiring
            regional_status = row.regional_hiring_evidence_status
            regional_ids = row.regional_hiring_evidence_ids
            if regional == "unknown" and old.regional_hiring != "unknown":
                regional = old.regional_hiring
                regional_status = old.regional_hiring_evidence_status
                regional_ids = old.regional_hiring_evidence_ids
            elif regional == old.regional_hiring == "open":
                regional_ids = sorted(set(regional_ids + old.regional_hiring_evidence_ids))

            openness = row.openness
            status = row.evidence_status
            confidence = row.confidence
            evidence_ids = row.evidence_ids
            if openness == "unknown" and old.openness != "unknown":
                openness = old.openness
                status = old.evidence_status
                confidence = old.confidence
                evidence_ids = old.evidence_ids
            elif old.openness == openness == "open":
                evidence_ids = sorted(set(evidence_ids + old.evidence_ids))
            elif old.openness == "not_open" and openness == "open" and old.evidence_ids:
                openness = "mixed"
                status = "inferred" if "inferred" in (old.evidence_status, status) else "verified"
                confidence = "medium"
                evidence_ids = sorted(set(evidence_ids + old.evidence_ids))
            out.append(row.model_copy(update={
                "regional_hiring": regional,
                "regional_hiring_evidence_status": regional_status,
                "regional_hiring_evidence_ids": regional_ids,
                "openness": openness,
                "evidence_status": status,
                "confidence": confidence,
                "evidence_ids": evidence_ids,
            }))
    return out


def _assessment_payload(
    conn: SupabaseConn, company: Company, source_run_id: str | None = None,
) -> ResearchAssessmentInput:
    previous = db.latest_research_assessment(conn, company.domain)
    evidence: dict[str, CompanyEvidence] = {}
    for fact, criterion in (("employee_count", "company_size"), ("funding", "funding_stage")):
        row = _company_fact(company, fact, criterion)
        if row:
            evidence[row.evidence_id] = row
    for fact, criterion in (("website", "website"), ("headquarters", "headquarters"),
                            ("company_type", "company_type")):
        row = _company_fact(company, fact, criterion)
        if row:
            evidence[row.evidence_id] = row
    for row in _classification_evidence(company):
        evidence[row.evidence_id] = row
    employee_fact = company.employee_count
    if employee_fact and isinstance(employee_fact.value, int) and employee_fact.value > 500 and employee_fact.url:
        evidence_id = _stable_id("classification_smb_size", {
            "domain": company.domain, "employees": employee_fact.value, "url": employee_fact.url,
        })
        evidence[evidence_id] = CompanyEvidence(
            evidence_id=evidence_id,
            criterion="classification_smb",
            subject="smb",
            claim={
                "category": "smb", "supports": False,
                "fact": f"The source reports approximately {employee_fact.value:,} employees.",
                "why": "A reported employee count above 500 is outside the SMB range used by Research.",
                "evidence_type": "direct" if employee_fact.method == "reported" else "inferred",
            },
            evidence_status="verified" if employee_fact.method == "reported" else "inferred",
            confidence=employee_fact.confidence,
            source_name=employee_fact.source,
            source_type=_source_type(employee_fact.url, company.domain),
            source_url=employee_fact.url,
            retrieved_at=employee_fact.fetched_at,
            excerpt=f"Approximately {employee_fact.value:,} employees.",
        )

    location_ids: dict[str, list[str]] = {"onshore": [], "nearshore": [], "offshore": []}
    integrator_ids: list[str] = []
    postings = db.get_validated_postings(conn, company.domain, run_id=source_run_id)
    for posting in postings:
        role_link = _role_link_evidence(posting)
        if role_link:
            evidence[role_link.evidence_id] = role_link
        service_fit = _service_fit_evidence(posting)
        if service_fit:
            evidence[service_fit.evidence_id] = service_fit
        location = _posting_location_evidence(posting, company.domain)
        if location:
            evidence[location.evidence_id] = location
            for model in location.claim["regions"]:
                location_ids[model].append(location.evidence_id)
        integrator = _integrator_evidence(posting, company.domain)
        if integrator:
            evidence[integrator.evidence_id] = integrator
            integrator_ids.append(integrator.evidence_id)

    size_evidence = next((row for row in evidence.values()
                          if row.criterion == "automatic_company_size"), None)
    funding_evidence = next((row for row in evidence.values()
                             if row.criterion == "automatic_funding_stage"), None)
    company_type_evidence = next((row for row in evidence.values()
                                  if row.criterion == "automatic_company_type"), None)
    targeted_findings: dict[str, tuple[str, str, list[str]]] = {}
    for category in ("startup", "smb", "system_integrator"):
        rows = [row for row in evidence.values() if row.criterion == f"classification_{category}"]
        if not rows:
            continue
        supporting = [row for row in rows if row.claim.get("supports") is True]
        selected = supporting or [row for row in rows if row.claim.get("supports") is False]
        if selected:
            membership = "match" if supporting else "no_match"
            status = "inferred" if any(row.evidence_status == "inferred" for row in selected) else "verified"
            targeted_findings[category] = (membership, status, [row.evidence_id for row in selected])
    description = (company.description or "").lower()
    explicit_startup_context = any(term in description for term in (
        "startup", "start-up", "early-stage", "early stage", "venture-backed",
    ))
    categories = classify_icp_categories(
        company.employees,
        employee_count_status=size_evidence.evidence_status if size_evidence else "unavailable",
        employee_count_evidence_ids=[size_evidence.evidence_id] if size_evidence else [],
        startup_evidence_status=funding_evidence.evidence_status if funding_evidence else "unavailable",
        startup_evidence_ids=[funding_evidence.evidence_id] if funding_evidence else [],
        startup_stage=(
            _startup_stage(funding_evidence.claim.get("funding"))
            if funding_evidence and explicit_startup_context else None
        ),
        integrator_match=True if integrator_ids else None,
        integrator_evidence_status=(evidence[integrator_ids[0]].evidence_status
                                     if integrator_ids else "unavailable"),
        integrator_evidence_ids=integrator_ids,
        explicit_company_type=(company_type_evidence.claim.get("company_type")
                       if company_type_evidence else None),
        explicit_company_type_status=(company_type_evidence.evidence_status
                          if company_type_evidence else "unavailable"),
        explicit_company_type_evidence_ids=([company_type_evidence.evidence_id]
                            if company_type_evidence else []),
        category_findings=targeted_findings,
    )
    hiring_models = [HiringModelAssessment(
        model=model,
        openness="unknown",
        evidence_status="unavailable",
        confidence=None,
        evidence_ids=[],
        regional_hiring="open" if location_ids[model] else "unknown",
        regional_hiring_evidence_status=(
            "verified" if any(evidence[item].evidence_status == "verified" for item in location_ids[model])
            else "inferred" if location_ids[model]
            else "unavailable"
        ),
        regional_hiring_evidence_ids=sorted(set(location_ids[model])),
    ) for model in ("onshore", "nearshore", "offshore")]

    historical_automatic_role_criteria = {
        "role_link", "automatic_role_location", "automatic_system_integrator",
    }
    old_evidence = [
        row for row in (previous.evidence if previous else [])
        if row.criterion not in historical_automatic_role_criteria
    ]
    evidence.update({row.evidence_id: row for row in old_evidence})
    current_evidence_ids = set(evidence)
    old_categories = {
        row.category: row for row in (previous.category_assessments if previous else [])
        if set(row.evidence_ids) <= current_evidence_ids
        and not any(
            evidence.get(evidence_id) is not None
            and evidence[evidence_id].criterion == "automatic_company_type"
            for evidence_id in row.evidence_ids
        )
    }
    categories = _merge_categories(categories, old_categories)
    old_hiring = {
        row.model: row for row in (previous.hiring_model_assessments if previous else [])
        if set(row.evidence_ids + row.regional_hiring_evidence_ids) <= current_evidence_ids
    }
    hiring_models = _merge_hiring_models(hiring_models, old_hiring)
    decision_makers = _merge_decision_makers(
        db.get_contacts(conn, company.domain), previous.decision_makers if previous else [],
    )

    known_evidence = {row.evidence_id for row in evidence.values()}
    role_links = [row for row in evidence.values()
                  if row.criterion == "role_link" and row.claim.get("current") is True]
    relevant_roles = [row for row in role_links if row.claim.get("practice_area")]
    official_website_ids = [
        row.evidence_id for row in evidence.values()
        if row.criterion == "automatic_website" and row.source_url
    ]
    headquarters_ids = [
        row.evidence_id for row in evidence.values()
        if row.criterion == "automatic_headquarters"
        and str(row.claim.get("headquarters", {}).get("country", "")).lower()
        in {"us", "usa", "united states", "united states of america"}
    ]
    decision_maker_ids = [
        evidence_id for person in decision_makers
        if person.linkedin_validation == "validated" and person.official_site_validation == "validated"
        for evidence_id in person.evidence_ids
    ]
    criterion_evidence = {
        "website": official_website_ids,
        "us_hq": headquarters_ids,
        "company_size": [row.evidence_id for row in evidence.values()
                         if row.criterion == "automatic_company_size"],
        "role_links": [row.evidence_id for row in role_links],
        "csg_relevant_opening": [row.evidence_id for row in relevant_roles],
        "decision_maker": decision_maker_ids,
        "hiring_model": [evidence_id for model in hiring_models
                         for evidence_id in model.regional_hiring_evidence_ids],
        "service_fit": [row.evidence_id for row in evidence.values()
                if row.criterion == "service_fit"],
    }
    criteria = {
        row.criterion: row for row in (previous.criteria_assessments if previous else [])
        if row.result == "met" and row.evidence_ids and set(row.evidence_ids) <= known_evidence
    }
    for criterion, evidence_ids in criterion_evidence.items():
        required_count = 3 if criterion == "role_links" else 1
        if len(set(evidence_ids)) >= required_count:
            criteria[criterion] = {
                "criterion": criterion, "result": "met", "evidence_ids": sorted(set(evidence_ids)),
            }
        elif criterion not in criteria:
            criteria[criterion] = {"criterion": criterion, "result": "unknown", "evidence_ids": []}

    return ResearchAssessmentInput(
        prospecting_disposition=previous.prospecting_disposition if previous else "needs_review",
        rejection_reason=previous.rejection_reason if previous else None,
        category_assessments=categories,
        hiring_model_assessments=hiring_models,
        criteria_assessments=list(criteria.values()),
        decision_makers=decision_makers,
        evidence=sorted(evidence.values(), key=lambda row: row.evidence_id),
    )


def _semantic_assessment(assessment: Any) -> str:
    row = assessment.model_dump(mode="json")
    for key in ("assessment_id", "assessment_version", "assessed_at", "source_run_id"):
        row.pop(key, None)
    for key in ("category_assessments", "hiring_model_assessments", "criteria_assessments", "decision_makers", "evidence"):
        row[key] = sorted(row[key], key=lambda item: json.dumps(item, sort_keys=True))
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def generate_company_assessment(
    conn: SupabaseConn, domain: str, source_run_id: str | None = None,
) -> Any:
    company = db.get_company(conn, domain)
    if company is None:
        raise ValueError(f"cannot assess unknown company {domain}")
    payload = _assessment_payload(conn, company, source_run_id)
    previous = db.latest_research_assessment(conn, domain)
    assessment = build_assessment(
        domain,
        payload,
        assessment_version=db.next_research_assessment_version(conn, domain),
        source_run_id=source_run_id,
    )
    if previous and _semantic_assessment(previous) == _semantic_assessment(assessment):
        return previous
    db.save_research_assessment(conn, assessment)
    return assessment


def refresh_company_assessment_safely(
    conn: SupabaseConn, domain: str, source_run_id: str | None = None,
) -> bool:
    try:
        generate_company_assessment(conn, domain, source_run_id)
        return True
    except Exception as exc:
        print(
            f"  [WARN] automatic assessment failed for {domain}"
            f" (run={source_run_id or 'none'}): {type(exc).__name__}: {exc}"
        )
        return False