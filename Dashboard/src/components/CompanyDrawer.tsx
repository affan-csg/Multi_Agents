import { useEffect, useState } from "react";
import { researchApi, type CompanyDetail } from "../agents/research";
import { AtsBadge, Avatar, DrawerSkeleton, Icon } from "./ui";

interface Props {
  runId: string;
  domain: string;
  onClose: () => void;
  onOpenFull: () => void;
}

export function CompanyDrawer({ runId, domain, onClose, onOpenFull }: Props) {
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    researchApi.companyDetail(runId, domain).then(setDetail).catch((e) => setError(String(e)));
  }, [runId, domain]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const score = detail?.score;
  const dims = score ? Object.entries(score.dimensions) : [];
  const maxDim = dims.reduce((m, [, v]) => Math.max(m, v), 0) || 1;

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} type="button" aria-label="Close">
          <Icon name="close" size={16} />
        </button>

        {error && <p className="error-text">{error}</p>}
        {!detail && !error && <DrawerSkeleton />}

        {detail && (
          <>
            <div className="drawer-head">
              <Avatar name={detail.company.name || detail.company.domain} />
              <div>
                <h2>{detail.company.name || detail.company.domain}</h2>
                <div className="cell-company-domain">{detail.company.domain}</div>
              </div>
            </div>

            <div className="drawer-meta">
              {detail.company.hq_state && (
                <span className="meta-chip">
                  <Icon name="pin" size={14} />
                  {detail.company.hq_state}
                </span>
              )}
              {detail.company.industry && <span className="meta-chip">{detail.company.industry}</span>}
              <AtsBadge provider={detail.company.ats_provider} />
              {detail.outreach_ready && (
                <span className="meta-chip" title="Has a verified contact email">
                  <Icon name="send" size={14} />
                  Outreach ready
                </span>
              )}
            </div>

            {score && (
              <>
                <div className="score-summary">
                  <span className="score-ring-sm">{score.score.toFixed(0)}</span>
                  <div style={{ flex: 1 }}>
                    <div className="score-summary-label">ICP Score</div>
                    <div className="score-summary-sub">
                      {score.score >= 75 ? "High fit for your target profile" : "Partial fit for your target profile"}
                    </div>
                  </div>
                  <span className={`tier-badge tier-${score.tier}`}>Tier {score.tier}</span>
                </div>

                <h3 className="drawer-section-title">Score breakdown</h3>
                <div className="dim-list">
                  {dims.map(([name, value]) => (
                    <div className="dim-item" key={name}>
                      <div className="dim-item-head">
                        <span className="dim-name">{name.replace(/_/g, " ")}</span>
                        <span className="dim-score">{value.toFixed(1)}</span>
                      </div>
                      <div className="dim-bar">
                        <div className="dim-bar-fill" style={{ width: `${(value / maxDim) * 100}%` }} />
                      </div>
                    </div>
                  ))}
                </div>

                {score.explanation.length > 0 && (
                  <div className="why-box">
                    <div className="why-box-title">
                      <Icon name="sparkle" size={15} />
                      Why this score?
                    </div>
                    <ul className="why-list">
                      {score.explanation.slice(0, 6).map((line, i) => (
                        <li key={i}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}

            {!score && <p className="hint">This company has no score for the current run.</p>}

            <div className="action-row" style={{ marginTop: 24 }}>
              <button type="button" className="primary" onClick={onOpenFull}>
                View full profile
                <Icon name="arrowRight" size={14} />
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
