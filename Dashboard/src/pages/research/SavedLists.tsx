import type { SavedCompany } from "../../agents/research";
import { AtsBadge, Avatar, EmptyState, Icon, SavedCardSkeleton, ScorePill, relativeTime } from "../../components/ui";

interface Props {
  saved: SavedCompany[];
  loading: boolean;
  onUnsave: (domain: string) => void;
  onOpen: (domain: string) => void;
  onBrowse: () => void;
}

export function SavedLists({ saved, loading, onUnsave, onOpen, onBrowse }: Props) {
  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Research Agent / <span>Saved</span></p>
          <h1>Saved lists</h1>
          <p>Companies you've bookmarked for outreach.</p>
        </div>
        <div className="page-head-actions">
          {saved.length > 0 && <span className="run-badge">{saved.length} saved</span>}
          <button type="button" onClick={onBrowse}>
            <Icon name="building" size={15} />
            Browse companies
          </button>
        </div>
      </div>

      {loading ? (
        <div className="saved-grid">
          {Array.from({ length: 6 }).map((_, i) => (
            <SavedCardSkeleton key={i} />
          ))}
        </div>
      ) : saved.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="bookmark"
            message="Nothing saved yet — bookmark a company from the Companies table to see it here."
          />
        </div>
      ) : (
        <div className="saved-grid">
          {saved.map((item) => {
            const name = item.company?.name || item.domain;
            return (
              <div className="saved-card" key={item.domain}>
                <button
                  type="button"
                  className="saved-remove"
                  onClick={() => onUnsave(item.domain)}
                  aria-label={`Remove ${name} from saved`}
                  title="Remove from saved"
                >
                  <Icon name="close" size={14} />
                </button>

                <div className="saved-card-head" onClick={() => onOpen(item.domain)}>
                  <Avatar name={name} />
                  <div style={{ minWidth: 0 }}>
                    <div className="cell-company-name">{name}</div>
                    <div className="cell-company-domain">{item.domain}</div>
                  </div>
                </div>

                <div className="saved-card-meta">
                  {item.company?.hq_state && (
                    <span className="meta-chip">
                      <Icon name="pin" size={13} />
                      {item.company.hq_state}
                    </span>
                  )}
                  {item.company?.industry && <span className="meta-chip">{item.company.industry}</span>}
                  <AtsBadge provider={item.company?.ats_provider ?? null} />
                </div>

                <div className="saved-card-foot">
                  {item.score ? (
                    <span className="saved-score">
                      <ScorePill score={item.score.score} />
                      <span className={`tier-badge tier-${item.score.tier}`}>Tier {item.score.tier}</span>
                    </span>
                  ) : (
                    <span className="hint">No score</span>
                  )}
                  <span className="saved-date">Saved {relativeTime(item.saved_at)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
