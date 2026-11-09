import type {
  AssessmentEvidenceStatus,
  AssessmentHiringModel,
  AssessmentMembership,
  AssessmentOpenness,
  CompanyResearchAssessmentSummary,
  ResearchReadiness,
} from "../agents/research";

export interface AssessmentFilters {
  category: "" | "startup" | "smb" | "system_integrator";
  membership: "" | AssessmentMembership;
  hiringModel: "" | AssessmentHiringModel;
  regionalHiring: "" | AssessmentOpenness;
  openness: "" | AssessmentOpenness;
  readiness: "" | ResearchReadiness;
  evidenceStatus: "" | AssessmentEvidenceStatus;
}

export const EMPTY_ASSESSMENT_FILTERS: AssessmentFilters = {
  category: "",
  membership: "",
  hiringModel: "",
  regionalHiring: "",
  openness: "",
  readiness: "",
  evidenceStatus: "",
};

export const ASSESSMENT_CATEGORIES = [
  ["startup", "Startup"],
  ["smb", "SMB"],
  ["system_integrator", "System Integrator"],
] as const;

export const ASSESSMENT_HIRING_MODELS = [
  ["onshore", "Onshore — US talent"],
  ["nearshore", "Nearshore — LATAM talent"],
  ["offshore", "Offshore — Pakistan talent"],
] as const;

export const ASSESSMENT_READINESS: Array<[ResearchReadiness, string]> = [
  ["unverified_pipeline", "Unverified Pipeline"],
  ["contact_enrichment_pending", "Contact Enrichment Pending"],
  ["fully_researched", "Fully Researched"],
  ["rejected", "Rejected"],
];

export function formatAssessmentValue(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function matchesAssessmentFilters(
  assessment: CompanyResearchAssessmentSummary | null | undefined,
  filters: AssessmentFilters,
): boolean {
  if (!Object.values(filters).some(Boolean)) return true;
  if (!assessment) return false;

  const categories = assessment.category_assessments ?? [];
  if (filters.category || filters.membership) {
    const categoryMatch = categories.some((row) =>
      (!filters.category || row.category === filters.category)
      && (!filters.membership || row.membership === filters.membership),
    );
    if (!categoryMatch) return false;
  }

  const hiringModels = assessment.hiring_model_assessments ?? [];
  if (filters.hiringModel || filters.regionalHiring || filters.openness) {
    const hiringMatch = hiringModels.some((row) =>
      (!filters.hiringModel || row.model === filters.hiringModel)
      && (!filters.regionalHiring || row.regional_hiring === filters.regionalHiring)
      && (!filters.openness || row.openness === filters.openness),
    );
    if (!hiringMatch) return false;
  }

  if (filters.readiness && assessment.readiness_status !== filters.readiness) return false;
  if (filters.evidenceStatus) {
    const statuses = [
      ...categories.map((row) => row.evidence_status),
      ...hiringModels.flatMap((row) => [row.regional_hiring_evidence_status, row.evidence_status]),
    ];
    if (!statuses.includes(filters.evidenceStatus)) return false;
  }
  return true;
}