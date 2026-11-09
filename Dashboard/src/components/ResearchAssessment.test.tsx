import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { CompanyResearchAssessment } from "../agents/research";
import {
  ResearchAssessmentPanel,
  AssessmentSummary,
} from "./ResearchAssessment";
import {
  AssessmentFilterControls,
} from "./ResearchAssessmentFilters";
import {
  EMPTY_ASSESSMENT_FILTERS,
  matchesAssessmentFilters,
} from "./assessmentFilterLogic";

const assessment: CompanyResearchAssessment = {
  assessment_id: "assessment-1",
  domain: "example.com",
  assessment_version: 3,
  schema_version: 1,
  source_run_id: "run-1",
  readiness_status: "fully_researched",
  assessed_at: "2026-11-05T12:00:00Z",
  prospecting_disposition: "eligible",
  rejection_reason: null,
  category_assessments: [
    { category: "startup", membership: "match", evidence_status: "verified", evidence_ids: ["size"] },
    { category: "smb", membership: "match", evidence_status: "inferred", evidence_ids: ["size"] },
    { category: "system_integrator", membership: "conflicted", evidence_status: "inferred", evidence_ids: ["integrator"] },
  ],
  hiring_model_assessments: [
    { model: "onshore", regional_hiring: "open", regional_hiring_evidence_status: "verified", regional_hiring_evidence_ids: ["size"], openness: "unknown", evidence_status: "unavailable", confidence: null, evidence_ids: [] },
    { model: "nearshore", regional_hiring: "open", regional_hiring_evidence_status: "verified", regional_hiring_evidence_ids: ["latam"], openness: "unknown", evidence_status: "unavailable", confidence: null, evidence_ids: [] },
    { model: "offshore", regional_hiring: "open", regional_hiring_evidence_status: "inferred", regional_hiring_evidence_ids: ["pakistan"], openness: "open", evidence_status: "inferred", confidence: "low", evidence_ids: ["pakistan"] },
  ],
  criteria_assessments: [{ criterion: "website", result: "met", evidence_ids: ["size"], notes: null }],
  decision_makers: [{
    decision_maker_id: "person-1", name: "Jordan Example", title: "CTO", persona: "technical_executive",
    linkedin_validation: "validated", official_site_validation: "not_validated",
    linkedin_evidence_ids: ["person"], official_site_evidence_ids: [], evidence_ids: ["person"],
    hunter_outcome: "accept_all", hunter_email: "jordan@example.com", hunter_checked_at: "2026-11-04T10:00:00Z",
  }],
  evidence: [
    { evidence_id: "size", criterion: "company_size", subject: null, claim: { employees: 100 }, evidence_status: "verified", confidence: "high", source_name: "Company profile", source_type: "official_company", source_url: "https://example.com/about", source_published_at: null, retrieved_at: "2026-11-03T10:00:00Z", excerpt: "About 100 employees", conflict_group: null, unavailable_reason: null },
    { evidence_id: "integrator", criterion: "service_fit", subject: null, claim: {}, evidence_status: "inferred", confidence: "medium", source_name: "Industry directory", source_type: "third_party", source_url: "https://example.com/listing", source_published_at: null, retrieved_at: "2026-11-03T10:00:00Z", excerpt: "Integrator listing", conflict_group: "provider-type", unavailable_reason: null },
    { evidence_id: "unavailable", criterion: "hiring_model", subject: null, claim: {}, evidence_status: "unavailable", confidence: "low", source_name: null, source_type: null, source_url: null, source_published_at: null, retrieved_at: null, excerpt: null, conflict_group: null, unavailable_reason: "No regional hiring data found" },
    { evidence_id: "latam", criterion: "hiring_model", subject: null, claim: { region: "LATAM" }, evidence_status: "verified", confidence: "medium", source_name: "Careers page", source_type: "official_job_board", source_url: "https://example.com/jobs", source_published_at: null, retrieved_at: "2026-11-03T10:00:00Z", excerpt: "LATAM roles", conflict_group: null, unavailable_reason: null },
    { evidence_id: "pakistan", criterion: "hiring_model", subject: null, claim: { country: "Pakistan" }, evidence_status: "inferred", confidence: "low", source_name: "Hiring profile", source_type: "third_party", source_url: "https://example.com/hiring", source_published_at: null, retrieved_at: "2026-11-03T10:00:00Z", excerpt: "Regional hiring patterns", conflict_group: null, unavailable_reason: null },
    { evidence_id: "person", criterion: "decision_maker", subject: null, claim: { name: "Jordan Example", title: "CTO" }, evidence_status: "verified", confidence: "high", source_name: "LinkedIn", source_type: "linkedin", source_url: "https://linkedin.com/in/jordan", source_published_at: null, retrieved_at: "2026-11-03T10:00:00Z", excerpt: null, conflict_group: null, unavailable_reason: null },
  ],
};

describe("ResearchAssessmentPanel", () => {
  it("renders overlapping categories, actual unknown/conflicted states, provenance, readiness, and Hunter outcome", () => {
    const html = renderToStaticMarkup(<ResearchAssessmentPanel assessment={assessment} />);

    expect(html).toContain("Startup");
    expect(html).toContain("SMB");
    expect(html).toContain("Startup</strong><span class=\"assessment-state state-match\">Match");
    expect(html).toContain("System Integrator</strong><span class=\"assessment-state state-conflicted\">Conflicted");
    expect(html).toContain("Unknown");
    expect(html).toContain("Inferred");
    expect(html).toContain("Verified");
    expect(html).toContain("India-only evidence does not establish Pakistan hiring.");
    expect(html).toContain("does not establish openness to external staffing");
    expect(html).toContain("https://example.com/about");
    expect(html).toContain("Retrieved:");
    expect(html).toContain("Fully Researched");
    expect(html).toContain("Jordan Example");
    expect(html).toContain("Accept All");
    expect(html).toContain("Assessment version");
    expect(html).toContain("1 unique record");
  });

  it("shows legacy or missing assessment as unavailable instead of inventing a status", () => {
    const html = renderToStaticMarkup(<ResearchAssessmentPanel assessment={null} />);
    expect(html).toContain("No research assessment is available");
    expect(html).not.toContain("No Match");
  });

  it("shows backend rejection reason and explicit unknown openness option", () => {
    const rejected = {
      ...assessment,
      readiness_status: "rejected" as const,
      rejection_reason: "Existing client",
    };
    const panel = renderToStaticMarkup(<ResearchAssessmentPanel assessment={rejected} />);
    const controls = renderToStaticMarkup(
      <AssessmentFilterControls filters={EMPTY_ASSESSMENT_FILTERS} onChange={() => {}} />,
    );

    expect(panel).toContain("Rejection reason: Existing client");
    expect(controls).toContain("value=\"conflicted\"");
    expect(controls).toContain("value=\"unknown\"");
    expect(controls).toContain("Unverified Pipeline");
    expect(controls).toContain("Fully Researched");
    expect(controls).toContain("Filter by evidence status");
  });

  it("shows regional hiring separately from external-staffing openness in summaries", () => {
    const html = renderToStaticMarkup(<AssessmentSummary assessment={assessment} />);

    expect(html).toContain("Onshore — US talent: Regional Open (Verified) · Staffing Unknown (Unavailable)");
    expect(html).toContain("Nearshore — LATAM talent: Regional Open (Verified) · Staffing Unknown (Unavailable)");
    expect(html).toContain("Offshore — Pakistan talent: Regional Open (Inferred) · Staffing Open (Inferred)");
  });
});

describe("matchesAssessmentFilters", () => {
  it("combines category, membership, model, openness, readiness, and evidence filters", () => {
    expect(matchesAssessmentFilters(assessment, {
      ...EMPTY_ASSESSMENT_FILTERS,
      category: "startup",
      membership: "match",
      hiringModel: "offshore",
      regionalHiring: "open",
      openness: "open",
      readiness: "fully_researched",
      evidenceStatus: "inferred",
    })).toBe(true);

    expect(matchesAssessmentFilters(assessment, {
      ...EMPTY_ASSESSMENT_FILTERS,
      category: "startup",
      membership: "unknown",
    })).toBe(false);

    expect(matchesAssessmentFilters(assessment, {
      ...EMPTY_ASSESSMENT_FILTERS,
      hiringModel: "onshore",
      regionalHiring: "open",
      openness: "unknown",
      evidenceStatus: "verified",
    })).toBe(true);
  });

  it("keeps legacy rows when filters are clear and excludes them when an assessment filter is active", () => {
    expect(matchesAssessmentFilters(undefined, EMPTY_ASSESSMENT_FILTERS)).toBe(true);
    expect(matchesAssessmentFilters(null, { ...EMPTY_ASSESSMENT_FILTERS, readiness: "fully_researched" })).toBe(false);
  });
});