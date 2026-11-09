import {
  ASSESSMENT_CATEGORIES,
  ASSESSMENT_HIRING_MODELS,
  ASSESSMENT_READINESS,
  formatAssessmentValue,
  type AssessmentFilters,
} from "./assessmentFilterLogic";

export function AssessmentFilterControls({
  filters,
  onChange,
}: {
  filters: AssessmentFilters;
  onChange: (filters: AssessmentFilters) => void;
}) {
  function update<K extends keyof AssessmentFilters>(key: K, value: AssessmentFilters[K]) {
    onChange({ ...filters, [key]: value });
  }

  return (
    <div className="assessment-filter-controls">
      <label>Category
        <select aria-label="Filter by company category" value={filters.category} onChange={(e) => update("category", e.target.value as AssessmentFilters["category"])}>
          <option value="">All categories</option>
          {ASSESSMENT_CATEGORIES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select>
      </label>
      <label>Membership
        <select aria-label="Filter by category membership" value={filters.membership} onChange={(e) => update("membership", e.target.value as AssessmentFilters["membership"])}>
          <option value="">Any membership</option>
          {(["match", "no_match", "unknown", "conflicted"] as const).map((value) => <option value={value} key={value}>{formatAssessmentValue(value)}</option>)}
        </select>
      </label>
      <label>Hiring model
        <select aria-label="Filter by hiring model" value={filters.hiringModel} onChange={(e) => update("hiringModel", e.target.value as AssessmentFilters["hiringModel"])}>
          <option value="">All hiring models</option>
          {ASSESSMENT_HIRING_MODELS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select>
      </label>
      <label>Regional hiring
        <select aria-label="Filter by regional hiring" value={filters.regionalHiring} onChange={(e) => update("regionalHiring", e.target.value as AssessmentFilters["regionalHiring"])}>
          <option value="">Any regional hiring</option>
          {(["open", "not_open", "mixed", "unknown"] as const).map((value) => <option value={value} key={value}>{formatAssessmentValue(value)}</option>)}
        </select>
      </label>
      <label>Openness
        <select aria-label="Filter by hiring-model openness" value={filters.openness} onChange={(e) => update("openness", e.target.value as AssessmentFilters["openness"])}>
          <option value="">Any openness</option>
          {(["open", "not_open", "mixed", "unknown"] as const).map((value) => <option value={value} key={value}>{formatAssessmentValue(value)}</option>)}
        </select>
      </label>
      <label>Readiness
        <select aria-label="Filter by research readiness" value={filters.readiness} onChange={(e) => update("readiness", e.target.value as AssessmentFilters["readiness"])}>
          <option value="">All readiness</option>
          {ASSESSMENT_READINESS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select>
      </label>
      <label>Evidence
        <select aria-label="Filter by evidence status" value={filters.evidenceStatus} onChange={(e) => update("evidenceStatus", e.target.value as AssessmentFilters["evidenceStatus"])}>
          <option value="">Any evidence status</option>
          {(["verified", "inferred", "unavailable"] as const).map((value) => <option value={value} key={value}>{formatAssessmentValue(value)}</option>)}
        </select>
      </label>
    </div>
  );
}
