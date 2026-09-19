import { researchApi, type CompanyDetail as Detail, type ProvenanceValue } from "../../agents/research";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import { AtsBadge, Avatar, CompanyDetailSkeleton, EmptyState, Icon, relativeTime } from "../../components/ui";

function provenanceTitle(p: ProvenanceValue<unknown>): string {
  const method = p.method ?? "unknown";
  const parts = [
    `${method === "reported" ? "Reported figure" : method === "estimated" ? "Estimated — not a reported figure" : "Provenance unknown"}`,
    `Source: ${p.source || "unknown"}`,
    `Confidence: ${p.confidence || "unknown"}`,
  ];
  if (p.fetched_at) parts.push(`Researched: ${new Date(p.fetched_at).toLocaleDateString()}`);
  if (p.url) parts.push(p.url);
  return parts.join(" | ");
}

function estimateMark(p: ProvenanceValue<unknown>) {
  if (p.method === "reported") return null;
  return (
    <span className="prov-estimate" aria-label="estimated" title="Estimated">
      ~
    </span>
  );
}

interface Props {
  runId: string;
  domain: string;
  busy: boolean;
  onBack: () => void;
  onQualify: (domain: string) => void;
  onContacts: (domain: string) => void;
  onSuppress: (domain: string) => void;
}

export function CompanyDetail({ runId, domain, busy, onBack, onQualify, onContacts, onSuppress }: Props) {
  const { data: detail, error: fetchError } = useCachedFetch<Detail>(
    `company-detail:${runId}:${domain}`,
    () => researchApi.companyDetail(runId, domain),
  );

  if (fetchError) return <p className="error-text">{String(fetchError)}</p>;
  if (!detail) return <CompanyDetailSkeleton />;

  const { company, score, qualification, contacts, postings } = detail;
  const dims = score ? Object.entries(score.dimensions) : [];
  const maxDim = dims.reduce((m, [, v]) => Math.max(m, v), 0) || 1;

  return (
    <>
      <button type="button" className="back-link" onClick={onBack}>
        <Icon name="chevronLeft" size={15} />
        Back to results
      </button>

      <div className="detail-head">
        <Avatar name={company.name || company.domain} size="lg" />
        <div className="detail-head-text">
          <h1>{company.name || company.domain}</h1>
          <div className="detail-meta-row">
            {company.region && (
              <span className="meta-chip">
                <Icon name="pin" size={14} />
                {company.region}
              </span>
            )}
            {company.industry && <span className="meta-chip">{company.industry}</span>}
            {!company.ats_provider && company.hiring_signal === "unknown" ? (
              <span
                className="meta-chip"
                title="Every ATS/careers-page detection method was exhausted for this company (e.g. a fully JS-rendered site) -- not confirmed as having no ATS or no jobs."
              >
                <Icon name="chart" size={14} />
                ATS unknown
              </span>
            ) : (
              <AtsBadge provider={company.ats_provider} />
            )}
            {detail.outreach_ready && (
              <span className="meta-chip" title="Has a verified contact email — ready for Outreach">
                <Icon name="send" size={14} />
                Outreach ready
              </span>
            )}
            {company.careers_url && (
              <a href={company.careers_url} target="_blank" rel="noreferrer">
                <Icon name="link" size={14} />
                Careers page
              </a>
            )}
          </div>
          {company.description && <p className="detail-description" style={{ marginTop: 8, color: "var(--text-faint)" }}>{company.description}</p>}
          <div className="detail-meta-row" style={{ marginTop: 12 }}>
            {company.employee_count && (
              <span className="meta-chip" title={provenanceTitle(company.employee_count)}>
                <Icon name="users" size={14} />
                {company.employee_count.value.toLocaleString()} emp.
                {estimateMark(company.employee_count)}
              </span>
            )}
            {company.revenue && (
              <span className="meta-chip" title={provenanceTitle(company.revenue)}>
                <Icon name="briefcase" size={14} />
                {company.revenue.value} (Rev)
                {estimateMark(company.revenue)}
              </span>
            )}
            {company.funding && (
              <span className="meta-chip" title={provenanceTitle(company.funding)}>
                <Icon name="chart" size={14} />
                {company.funding.value} (Fund)
                {estimateMark(company.funding)}
              </span>
            )}
          </div>
        </div>
        {score && (
          <div className="tier-callout">
            <div className="tier-callout-label">Tier {score.tier}</div>
            <div className="tier-callout-score">
              <Icon name="crown" size={19} />
              {score.score.toFixed(0)}
            </div>
          </div>
        )}
      </div>

      <div className="action-row" style={{ marginBottom: 24 }}>
        <button type="button" className="primary" onClick={() => onQualify(company.domain)} disabled={busy}>
          <Icon name="sparkle" size={15} />
          Qualify with AI
        </button>
        <button type="button" onClick={() => onContacts(company.domain)} disabled={busy}>
          <Icon name="users" size={15} />
          Find contacts
        </button>
        <button type="button" className="danger" onClick={() => onSuppress(company.domain)} disabled={busy}>
          Suppress
        </button>
      </div>

      <div className="detail-grid">
        <div className="card">
          <h3 className="card-title">
            <Icon name="target" size={16} />
            ICP Score
          </h3>
          <p className="card-sub">How well {company.name || company.domain} matches your ideal customer profile.</p>

          {score ? (
            <div className="score-ring-wrap">
              <div className="score-ring">
                <ScoreRing value={score.score} />
                <div className="score-ring-center">
                  <div className="score-ring-value">{score.score.toFixed(0)}</div>
                  <div className="score-ring-tier">Tier {score.tier}</div>
                </div>
              </div>
              <div className="breakdown-list">
                {dims.map(([name, value]) => (
                  <div className="breakdown-row" key={name}>
                    <span className="breakdown-label">{name.replace(/_/g, " ")}</span>
                    <span className="breakdown-pct">{value.toFixed(1)}</span>
                    <span className="dim-bar">
                      <span className="dim-bar-fill" style={{ width: `${(value / maxDim) * 100}%`, display: "block", height: "100%" }} />
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <EmptyState icon="chart" message="No score for this company in the current run." />
          )}

          {score && score.explanation.length > 0 && (
            <div className="why-box">
              <div className="why-box-title">
                <Icon name="sparkle" size={15} />
                Why this score?
              </div>
              <ul className="why-list">
                {score.explanation.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="lightbulb" size={16} />
            Pain summary
          </h3>
          <p className="card-sub">What they're likely struggling with and how to position.</p>

          {qualification ? (
            <>
              <div className="quote-box">{qualification.pain_summary}</div>
              <div className="angle-box">
                <div className="angle-title">
                  <Icon name="target" size={15} />
                  Outreach angle
                </div>
                <p className="angle-text">{qualification.recommended_angle}</p>
              </div>
              <p className="hint" style={{ marginTop: 12 }}>
                Generated by {qualification.model} · {relativeTime(qualification.qualified_at)}
              </p>
            </>
          ) : (
            <>
              <EmptyState icon="lightbulb" message="Not qualified yet. Run AI qualification to generate a summary." />
              <button
                type="button"
                className="primary"
                style={{ width: "100%", justifyContent: "center" }}
                onClick={() => onQualify(company.domain)}
                disabled={busy}
              >
                <Icon name="sparkle" size={15} />
                Generate summary
              </button>
            </>
          )}
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="users" size={16} />
            Contacts
            {contacts.length > 0 && (
              <span className="tier-badge tier-A" style={{ marginLeft: "auto" }}>
                {contacts.length}
              </span>
            )}
          </h3>
          <p className="card-sub">Senior HR & talent contacts discovered for this domain.</p>

          {contacts.length === 0 ? (
            <>
              <EmptyState icon="users" message="No contacts found yet." />
              <button
                type="button"
                style={{ width: "100%", justifyContent: "center" }}
                onClick={() => onContacts(company.domain)}
                disabled={busy}
              >
                <Icon name="search" size={15} />
                Find contacts
              </button>
            </>
          ) : (
            contacts.map((c) => (
              <div className="contact-card" key={c.email}>
                <div className="contact-head">
                  <Avatar name={`${c.first_name || ""} ${c.last_name || c.email}`.trim()} size="sm" />
                  <div>
                    <div className="contact-name">
                      {[c.first_name, c.last_name].filter(Boolean).join(" ") || c.email}
                    </div>
                    <div className="contact-role">{c.position || c.seniority || "—"}</div>
                  </div>
                </div>
                <div className="contact-line">
                  <Icon name="mail" size={14} />
                  <a href={`mailto:${c.email}`}>{c.email}</a>
                </div>
                {c.confidence !== null && (
                  <div className="contact-line">
                    <Icon name="check" size={14} />
                    Email confidence {c.confidence}%
                  </div>
                )}
                {c.linkedin_url && (
                  <div className="contact-line">
                    <Icon name="link" size={14} />
                    <a href={c.linkedin_url} target="_blank" rel="noreferrer">
                      LinkedIn profile
                    </a>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      <div className="card">
        <div className="run-card-head">
          <div>
            <h3 className="card-title">
              <Icon name="briefcase" size={16} />
              Open requisitions
            </h3>
            <p className="card-sub" style={{ marginBottom: 0 }}>
              Recent job postings at {company.name || company.domain}.
            </p>
          </div>
          <span className="run-badge">{postings.length} total</span>
        </div>

        {postings.length === 0 ? (
          company.hiring_signal === "unknown" ? (
            <EmptyState
              icon="briefcase"
              message="Job data unavailable — every ATS/careers-page/search method was exhausted for this company. This is NOT a confirmed zero; their actual hiring status is unknown."
            />
          ) : (
            <EmptyState icon="briefcase" message="No open requisitions found (confirmed — a job board or careers page was checked)." />
          )
        ) : (
          <div className="table-scroll">
            <table className="mini-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Location</th>
                  <th>Department</th>
                  <th>Posted</th>
                  <th>Practice Area</th>
                  <th>Delivery</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {postings.slice(0, 40).map((p) => (
                  <tr key={p.external_id}>
                    <td style={{ fontWeight: 550 }}>{p.title}</td>
                    <td className="cell-muted">
                      {p.location ? (
                        <span className="meta-chip">
                          <Icon name="pin" size={13} />
                          {p.location}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="cell-muted">{p.department || "—"}</td>
                    <td className="cell-muted">{relativeTime(p.posted_at)}</td>
                    <td className="cell-muted">{p.practice_area || "—"}</td>
                    <td className="cell-muted">
                      {p.delivery_fit ? (
                         <span className="meta-chip">{p.delivery_fit}</span>
                      ) : "—"}
                    </td>
                    <td>
                      {p.url && (
                        <a href={p.url} target="_blank" rel="noreferrer" style={{ color: "var(--text-faint)", display: "flex" }}>
                          <Icon name="chevronRight" size={15} />
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function ScoreRing({ value }: { value: number }) {
  const R = 58;
  const C = 2 * Math.PI * R;
  const pct = Math.max(0, Math.min(100, value)) / 100;
  return (
    <svg width="132" height="132" viewBox="0 0 132 132">
      <circle cx="66" cy="66" r={R} fill="none" stroke="var(--surface-sunken)" strokeWidth="13" />
      <circle
        cx="66"
        cy="66"
        r={R}
        fill="none"
        stroke="var(--mint)"
        strokeWidth="13"
        strokeLinecap="round"
        strokeDasharray={`${C * pct} ${C}`}
        transform="rotate(-90 66 66)"
      />
    </svg>
  );
}
