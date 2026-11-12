import type {
  CompanyEvidence,
  CompanyResearchAssessment,
  CompanyResearchAssessmentSummary,
} from "../agents/research";
import type { CompanyDetail } from "../agents/research";
import {
  ASSESSMENT_CATEGORIES,
  ASSESSMENT_HIRING_MODELS,
  formatAssessmentValue,
} from "./assessmentFilterLogic";

function EvidenceReferences({ ids, evidenceById }: { ids: string[]; evidenceById: Map<string, CompanyEvidence> }) {
  if (ids.length === 0) return <p className="assessment-muted">No linked evidence recorded.</p>;
  return (
    <ul className="assessment-evidence-links">
      {ids.map((id) => {
        const evidence = evidenceById.get(id);
        return <li key={id}>{evidence ? <>
          {evidence.source_url ? <a href={evidence.source_url} target="_blank" rel="noreferrer">{evidence.source_name || evidence.source_url}</a> : evidence.source_name || "Source unavailable"}
          {` · ${formatAssessmentValue(evidence.evidence_status)} · ${formatAssessmentValue(evidence.confidence)} confidence · ${evidenceDate(evidence.retrieved_at)}`}
        </> : `Evidence ${id} is not available in this assessment.`}</li>;
      })}
    </ul>
  );
}

function evidenceDate(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function evidenceGroups(evidence: CompanyEvidence[]): CompanyEvidence[][] {
  const groups = new Map<string, CompanyEvidence[]>();
  for (const row of evidence) {
    const key = [row.criterion, row.source_name || "Source unavailable", row.source_type || ""].join("\u001f");
    groups.set(key, [...(groups.get(key) || []), row]);
  }
  return [...groups.values()];
}

const CRITERIA: Array<[string, string]> = [
  ["website", "Official company website identified"],
  ["us_hq", "US headquarters supported by evidence"],
  ["company_size", "Company size supported by evidence"],
  ["role_links", "At least three current role links recorded"],
  ["csg_relevant_opening", "Relevant current opening recorded"],
  ["decision_maker", "Named decision-maker with dated evidence"],
  ["hiring_model", "Hiring-model assessment supported or explicitly unavailable"],
  ["service_fit", "Service fit supported by evidence"],
];

const RESEARCH_FIELDS: Array<[string, string]> = [
  ["official_website", "Official website"],
  ["headquarters", "Headquarters"],
  ["employee_range", "Employee range"],
  ["company_classification", "Company classification"],
  ["leadership", "Leadership"],
  ["services", "Services"],
  ["hiring_service_fit", "Hiring and service fit"],
];

type ResearchOutcomes = NonNullable<CompanyDetail["company"]["research_outcomes"]>;

export function ResearchAssessmentPanel({
  assessment,
  outcomes,
}: {
  assessment: CompanyResearchAssessment | null | undefined;
  outcomes?: ResearchOutcomes;
}) {
  if (!assessment) {
    return (
      <section className="card research-assessment" aria-label="Company research assessment">
        <h3 className="card-title">Company targeting & research assessment</h3>
        <p className="assessment-muted">No research assessment is available. This may be a legacy company record or an assessment has not been recorded.</p>
      </section>
    );
  }

  const evidenceById = new Map(assessment.evidence.map((row) => [row.evidence_id, row]));
  const categoryRows = new Map(assessment.category_assessments.map((row) => [row.category, row]));
  const modelRows = new Map(assessment.hiring_model_assessments.map((row) => [row.model, row]));
  const criteriaRows = new Map(assessment.criteria_assessments.map((row) => [row.criterion, row]));

  return (
    <section className="card research-assessment" aria-label="Company research assessment">
      <div className="research-assessment-heading">
        <div>
          <h3 className="card-title">Company targeting & research assessment</h3>
          <p className="card-sub">Company-level evidence; individual posting delivery fit is shown separately under Open requisitions.</p>
        </div>
        <span className="assessment-version">Assessment version {assessment.assessment_version} · {evidenceDate(assessment.assessed_at)}</span>
      </div>

      <div className="assessment-block">
        <h4>Company categories</h4>
        <div className="assessment-grid">
          {ASSESSMENT_CATEGORIES.map(([category, label]) => {
            const row = categoryRows.get(category);
            return (
              <article className="assessment-item" key={category}>
                <div className="assessment-item-head"><strong>{label}</strong><span className={`assessment-state state-${row?.membership || "not_assessed"}`}>{row ? formatAssessmentValue(row.membership) : "Not assessed"}</span></div>
                <p>Evidence: {row ? formatAssessmentValue(row.evidence_status) : "Not available"}</p>
                {row && <EvidenceReferences ids={row.evidence_ids} evidenceById={evidenceById} />}
              </article>
            );
          })}
        </div>
      </div>

      <div className="assessment-block">
        <h4>Hiring models</h4>
        <p className="assessment-muted">Regional hiring evidence describes where a company has roles. It does not establish openness to external staffing. Offshore refers to Pakistan; India-only evidence does not establish Pakistan hiring.</p>
        <div className="assessment-grid">
          {ASSESSMENT_HIRING_MODELS.map(([model, label]) => {
            const row = modelRows.get(model);
            return (
              <article className="assessment-item" key={model}>
                <div className="assessment-item-head"><strong>{label}</strong><span className={`assessment-state state-${row?.regional_hiring || "not_assessed"}`}>{row ? `Regional hiring: ${formatAssessmentValue(row.regional_hiring)}` : "Not assessed"}</span></div>
                <p>Regional evidence: {row ? formatAssessmentValue(row.regional_hiring_evidence_status) : "Not available"}</p>
                {row && <EvidenceReferences ids={row.regional_hiring_evidence_ids} evidenceById={evidenceById} />}
                <p>External staffing openness: {row ? formatAssessmentValue(row.openness) : "Not assessed"} · Evidence: {row ? formatAssessmentValue(row.evidence_status) : "Not available"} · Confidence: {row?.confidence ? formatAssessmentValue(row.confidence) : "Not reported"}</p>
                {row && <EvidenceReferences ids={row.evidence_ids} evidenceById={evidenceById} />}
              </article>
            );
          })}
        </div>
      </div>

      <div className="assessment-block assessment-readiness">
        <div className="assessment-item-head">
          <h4>Research readiness</h4>
          <span className={`assessment-state state-${assessment.readiness_status}`}>{formatAssessmentValue(assessment.readiness_status)}</span>
        </div>
        {assessment.readiness_status === "rejected" && <p>Rejection reason: {assessment.rejection_reason || "No rejection reason recorded."}</p>}
        <p className="assessment-muted">Readiness is the status recorded by Research; the Dashboard does not recalculate it.</p>
        <h5>Completion criteria</h5>
        <ul className="assessment-criteria">
          {CRITERIA.map(([criterion, description]) => {
            const row = criteriaRows.get(criterion);
            return <li key={criterion}><span className={row?.result === "met" ? "criterion-met" : undefined}>{description}</span><strong>{row ? formatAssessmentValue(row.result) : "Not assessed"}</strong>{row?.notes && <small>{row.notes}</small>}</li>;
          })}
        </ul>
      </div>

      {outcomes && Object.keys(outcomes).length > 0 && (
        <div className="assessment-block">
          <h4>Field research outcomes</h4>
          <ul className="assessment-criteria">
            {RESEARCH_FIELDS.map(([field, label]) => {
              const outcome = outcomes[field];
              if (!outcome) return null;
              return <li key={field}>
                <span>{label}</span>
                <strong>{formatAssessmentValue(outcome.status)}</strong>
                {outcome.reason && <small>{outcome.reason}</small>}
              </li>;
            })}
          </ul>
        </div>
      )}

      <div className="assessment-block">
        <h4>Evidence records</h4>
        {assessment.evidence.length === 0 ? <p className="assessment-muted">No evidence records are available.</p> : (
          <div className="assessment-evidence-list">
            {evidenceGroups(assessment.evidence).map((group) => {
              const first = group[0];
              return <article className="assessment-evidence" key={`${first.criterion}-${first.source_url || first.source_name}`}>
                <div className="assessment-item-head"><strong>{formatAssessmentValue(first.criterion)} · {first.source_name || "Source unavailable"}</strong><span>{group.length} unique record{group.length === 1 ? "" : "s"}</span></div>
                <p>{formatAssessmentValue(first.evidence_status)} · {formatAssessmentValue(first.confidence)} confidence</p>
                {first.source_url ? <a href={first.source_url} target="_blank" rel="noreferrer">{first.source_url}</a> : <span className="assessment-muted">No source URL recorded.</span>}
                <details>
                  <summary>Show individual evidence</summary>
                  <ul className="assessment-evidence-links">
                    {group.map((row) => <li key={row.evidence_id}>
                      <strong>{row.subject || String(row.claim.title || "Job or company record")}</strong>
                      {` · ${formatAssessmentValue(row.evidence_status)} · ${formatAssessmentValue(row.confidence)} confidence · Retrieved: ${evidenceDate(row.retrieved_at)}${row.source_published_at ? ` · Published: ${evidenceDate(row.source_published_at)}` : ""}`}
                      {row.source_url ? <> · <a href={row.source_url} target="_blank" rel="noreferrer">source</a></> : null}
                      {row.excerpt ? ` · ${row.excerpt}` : ""}
                    </li>)}
                  </ul>
                </details>
              </article>;
            })}
          </div>
        )}
      </div>

      <div className="assessment-block">
        <h4>Decision makers & Hunter outcomes</h4>
        {assessment.decision_makers.length === 0 ? <p className="assessment-muted">No decision-maker records are available.</p> : (
          <div className="assessment-grid">
            {assessment.decision_makers.map((person) => (
              <article className="assessment-item" key={person.decision_maker_id}>
                <strong>{person.name}</strong><p>{person.title} · {formatAssessmentValue(person.persona)}</p>
                <p>LinkedIn: {formatAssessmentValue(person.linkedin_validation)} · Company site: {formatAssessmentValue(person.official_site_validation)}</p>
                <p>Hunter: {person.hunter_outcome ? formatAssessmentValue(person.hunter_outcome) : "Not checked"}{person.hunter_checked_at ? ` · checked ${evidenceDate(person.hunter_checked_at)}` : ""}</p>
                {person.hunter_email && <p>Hunter email: {person.hunter_email}</p>}
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

export function AssessmentSummary({ assessment }: { assessment: CompanyResearchAssessmentSummary | null | undefined }) {
  if (!assessment) return <span className="assessment-muted">No assessment</span>;
  const categories = ASSESSMENT_CATEGORIES.map(([category, label]) => {
    const row = assessment.category_assessments.find((item) => item.category === category);
    return <span key={category} className={`assessment-state state-${row?.membership || "not_assessed"}`}>{label}: {row ? formatAssessmentValue(row.membership) : "Not assessed"}</span>;
  });
  const hiringModels = ASSESSMENT_HIRING_MODELS.map(([model, label]) => {
    const row = assessment.hiring_model_assessments.find((item) => item.model === model);
    const status = row ? `Regional ${formatAssessmentValue(row.regional_hiring)} (${formatAssessmentValue(row.regional_hiring_evidence_status)}) · Staffing ${formatAssessmentValue(row.openness)} (${formatAssessmentValue(row.evidence_status)})` : "Not assessed";
    return <span key={model} className={`assessment-state state-${row?.regional_hiring || "not_assessed"}`}>{label}: {status}</span>;
  });
  return <div className="assessment-summary">{categories}{hiringModels}<span className={`assessment-state state-${assessment.readiness_status}`}>{formatAssessmentValue(assessment.readiness_status)}</span></div>;
}