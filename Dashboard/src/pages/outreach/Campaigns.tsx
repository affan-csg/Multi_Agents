import { useState, type MouseEvent } from "react";
import { researchApi, type RunSummary } from "../../agents/research";
import { outreachApi, type Campaign, type ExcludedProspect, type ImportedProspect } from "../../agents/outreach";
import { EmptyState, Icon, StatCard } from "../../components/ui";
import { useCachedFetch, invalidate, patch } from "../../hooks/useCachedFetch";
import { ConfirmModal } from "../../components/ConfirmModal";

interface Props {
  onOpenCampaign: (campaignId: string, campaignName?: string | null) => void;
}

export function Campaigns({ onOpenCampaign }: Props) {
  const { data: campaignsData, loading } = useCachedFetch<Campaign[]>(
    "campaigns",
    () => outreachApi.listCampaigns(),
  );
  const campaigns = campaignsData ?? [];
  const { data: runsData } = useCachedFetch<RunSummary[]>("research-runs", () => researchApi.listRuns());
  const runs = runsData ?? [];
  const runLabelById = new Map(runs.map((r) => [r.run_id, r.label || r.run_id]));
  // A research run can only back one real campaign -- creating a second
  // would just re-select the same top-N prospects into a duplicate. The
  // synthetic Test Campaign (research_run_id is always null, selection.test)
  // is exempt since it's not tied to any run at all.
  const usedRunIds = new Set(campaigns.filter((c) => c.research_run_id).map((c) => c.research_run_id));

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [runId, setRunId] = useState("__test__");
  const [limit, setLimit] = useState("25");
  const [tiers, setTiers] = useState("A,B");
  const [creating, setCreating] = useState(false);
  const isTestMode = runId === "__test__";
  const [error, setError] = useState<string | null>(null);
  const [excludedNotice, setExcludedNotice] = useState<
    { campaignId: string; campaignName: string; excluded: ExcludedProspect[] } | null
  >(null);

  // A run's prospects aren't ready for a campaign until (a) the research run
  // itself has finished, and (b) at least one prospect from it has actually
  // been imported into Outreach's cache (see Prospects.tsx's "Import
  // qualified prospects" step) -- selecting a run that's still discovering,
  // or one nobody has imported yet, would otherwise silently create a
  // campaign with zero items.
  const selectedRun = runs.find((r) => r.run_id === runId);
  const { data: selectedRunProspects, loading: prospectsLoading } = useCachedFetch<ImportedProspect[]>(
    !isTestMode && runId ? `prospects:${runId}` : null,
    () => outreachApi.listProspects(runId),
  );
  const researchDone = isTestMode || !!selectedRun?.finished_at;
  const prospectsReady = isTestMode || (researchDone && !prospectsLoading && (selectedRunProspects?.length ?? 0) > 0);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Campaign | null>(null);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(null);
  const [renaming, setRenaming] = useState<Campaign | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);

  function requestDelete(c: Campaign) {
    setMenuOpenId(null);
    setConfirmDelete(c);
  }

  function requestRename(c: Campaign) {
    setMenuOpenId(null);
    setRenaming(c);
    setRenameValue(c.name || "");
    setRenameError(null);
  }

  async function confirmRename() {
    if (!renaming) return;
    const name = renameValue.trim();
    if (!name) return;
    setRenameSaving(true);
    setRenameError(null);
    try {
      await outreachApi.renameCampaign(renaming.campaign_id, name);
      patch<Campaign[]>("campaigns", (cur) =>
        (cur ?? []).map((c) => (c.campaign_id === renaming.campaign_id ? { ...c, name } : c)),
      );
      invalidate("outreach-analytics");
      setRenaming(null);
    } catch (e) {
      setRenameError(e instanceof Error ? e.message : String(e));
    } finally {
      setRenameSaving(false);
    }
  }

  async function confirmDeleteCampaign() {
    if (!confirmDelete) return;
    const c = confirmDelete;
    setConfirmDelete(null);
    setDeletingId(c.campaign_id);
    setError(null);
    try {
      await outreachApi.deleteCampaign(c.campaign_id);
      patch<Campaign[]>("campaigns", (cur) => (cur ?? []).filter((x) => x.campaign_id !== c.campaign_id));
      invalidate("outreach-analytics");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingId(null);
    }
  }

  async function handleCreate() {
    if (!isTestMode && usedRunIds.has(runId)) {
      setError("A campaign already exists for this research run.");
      return;
    }
    if (!isTestMode && !prospectsReady) {
      setError("Prospects for this research run aren't ready yet — import them from the Prospects page first.");
      return;
    }
    setCreating(true);
    setError(null);
    setExcludedNotice(null);
    try {
      const { campaign, excluded_no_contact } = isTestMode
        ? { ...(await outreachApi.createTestCampaign()), excluded_no_contact: [] as ExcludedProspect[] }
        : await outreachApi.createCampaign({
            name: name.trim() || undefined,
            research_run_id: runId || undefined,
            limit: Number(limit) || undefined,
            tiers: tiers.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean),
          });
      setShowCreate(false);
      setName("");
      invalidate("campaigns");
      invalidate("outreach-analytics");
      invalidate("prospects:all");
      if (excluded_no_contact.length) {
        // Hold navigation so the exclusion message is actually seen instead
        // of flashing past on the way to Campaign Detail -- the person opens
        // the campaign themselves once they've read who was left out.
        setExcludedNotice({
          campaignId: campaign.campaign_id,
          campaignName: campaign.name || "the campaign",
          excluded: excluded_no_contact,
        });
      } else {
        onOpenCampaign(campaign.campaign_id, campaign.name);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Outreach Agent / <span>Campaigns</span></p>
          <h1>Campaigns</h1>
          <p>Select the configured top prospects and send research-based personalized outreach.</p>
        </div>
        <div className="page-head-actions">
          <button type="button" className="primary" onClick={() => setShowCreate((v) => !v)}>
            <Icon name="plus" size={15} />
            New campaign
          </button>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard icon="send" tone="accent" value={campaigns.length.toLocaleString()} label="Campaigns" />
      </div>

      {excludedNotice && (
        <div className="card" style={{ marginBottom: 20 }}>
          <h3 className="card-title">"{excludedNotice.campaignName}" created</h3>
          <p className="warning-text" style={{ marginTop: 4 }}>
            {excludedNotice.excluded.length} Tier A/B compan{excludedNotice.excluded.length === 1 ? "y was" : "ies were"} left
            out because no contact was found:
          </p>
          <ul style={{ marginTop: 8, marginBottom: 12, paddingLeft: 20 }}>
            {excludedNotice.excluded.map((e) => (
              <li key={e.domain} className="warning-text">
                No contacts found for {e.name}, so it was not added to the campaign.
              </li>
            ))}
          </ul>
          <div className="action-row">
            <button
              type="button"
              className="mint"
              onClick={() => {
                const { campaignId, campaignName } = excludedNotice;
                setExcludedNotice(null);
                onOpenCampaign(campaignId, campaignName);
              }}
            >
              Open campaign
            </button>
          </div>
        </div>
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 20 }}>
          <h3 className="card-title">Create a campaign</h3>
          <p className="card-sub">
            {isTestMode
              ? "Creates a fixed Test Campaign with two synthetic prospects — one in Pending, one in Manual Review — to safely exercise campaign creation, sending, and the full 7-stage pipeline. Nothing is sent automatically, and these never mix with real prospects."
              : "Selects the configured top-N cached prospects (by Research's own score, descending) and creates a pending outreach item for each. Import prospects first from the Prospects page."}
          </p>
          <div className="field-row">
            <div className="field-group" style={{ maxWidth: isTestMode ? 320 : undefined }}>
              <label>Research run</label>
              <select value={runId} onChange={(e) => setRunId(e.target.value)}>
                <option value="__test__">Test Campaign</option>
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id} disabled={usedRunIds.has(r.run_id) || !r.finished_at}>
                    {r.label || r.run_id}
                    {usedRunIds.has(r.run_id)
                      ? " (campaign already created)"
                      : !r.finished_at
                        ? " (research still running)"
                        : ""}
                  </option>
                ))}
              </select>
              {!isTestMode && usedRunIds.has(runId) && (
                <p className="hint" style={{ marginTop: 6 }}>
                  A campaign already exists for this research run — open it from the list below instead of
                  creating a duplicate.
                </p>
              )}
              {!isTestMode && !usedRunIds.has(runId) && researchDone && !prospectsLoading && !prospectsReady && (
                <p className="hint" style={{ marginTop: 6 }}>
                  No prospects have been imported for this research run yet — import qualified prospects from the
                  Prospects page first.
                </p>
              )}
              {!isTestMode && !usedRunIds.has(runId) && prospectsLoading && (
                <p className="hint" style={{ marginTop: 6 }}>Checking prospect availability…</p>
              )}
            </div>
            {!isTestMode && (
              <div className="field-group">
                <label>Name</label>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. GA Q4 push" />
              </div>
            )}
          </div>
          {!isTestMode && (
            <div className="field-row">
              <div className="field-group">
                <label>Top N</label>
                <input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} min={1} />
              </div>
              <div className="field-group">
                <label>Tiers</label>
                <input value={tiers} onChange={(e) => setTiers(e.target.value)} placeholder="A,B" />
              </div>
            </div>
          )}
          {error && <p className="error-text">{error}</p>}
          <div className="action-row" style={{ marginTop: 16 }}>
            <button
              type="button"
              className="mint"
              onClick={handleCreate}
              disabled={creating || (!isTestMode && (usedRunIds.has(runId) || !prospectsReady))}
            >
              <Icon name={creating ? "refresh" : "play"} size={15} className={creating ? "icon-spin" : undefined} />
              {creating ? "Creating…" : isTestMode ? "Create test campaign" : "Create campaign"}
            </button>
          </div>
        </div>
      )}

      <div className="table-card">
        {!loading && campaigns.length === 0 ? (
          <EmptyState icon="send" message="No campaigns yet — create one to select top prospects and start sending." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Channel</th>
                  <th>Research run</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {campaigns.map((c) => (
                  <tr key={c.campaign_id} onClick={() => onOpenCampaign(c.campaign_id, c.name)}>
                    <td style={{ fontWeight: 550 }}>{c.name || c.campaign_id}</td>
                    <td className="cell-muted">{c.channel}</td>
                    <td className="cell-muted">{c.research_run_id ? runLabelById.get(c.research_run_id) || c.research_run_id : "—"}</td>
                    <td className="cell-muted">{c.created_at ? new Date(c.created_at).toLocaleDateString() : "—"}</td>
                    <td>
                      <div className="menu-wrap" onClick={(e) => e.stopPropagation()}>
                        <button
                          type="button"
                          className="ghost menu-trigger"
                          title="More actions"
                          disabled={deletingId === c.campaign_id}
                          onClick={(e: MouseEvent<HTMLButtonElement>) => {
                            if (menuOpenId === c.campaign_id) {
                              setMenuOpenId(null);
                              return;
                            }
                            const rect = e.currentTarget.getBoundingClientRect();
                            setMenuPos({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
                            setMenuOpenId(c.campaign_id);
                          }}
                        >
                          <Icon name="more" size={16} />
                        </button>
                        {menuOpenId === c.campaign_id && menuPos && (
                          <>
                            <div className="menu-backdrop" onClick={() => setMenuOpenId(null)} />
                            <div
                              className="menu-popover menu-popover--fixed"
                              style={{ top: menuPos.top, right: menuPos.right }}
                            >
                              <button type="button" className="menu-item" onClick={() => requestRename(c)}>
                                <Icon name="edit" size={14} />
                                Rename campaign
                              </button>
                              <button
                                type="button"
                                className="menu-item menu-item--danger"
                                onClick={() => requestDelete(c)}
                              >
                                <Icon name="trash" size={14} />
                                Delete campaign
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {confirmDelete && (
        <ConfirmModal
          title="Delete campaign?"
          message={`Delete campaign "${confirmDelete.name || confirmDelete.campaign_id}" and all its items? This cannot be undone.`}
          danger
          confirmLabel="Delete"
          onConfirm={confirmDeleteCampaign}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      {renaming && (
        <div className="drawer-overlay" onClick={() => setRenaming(null)}>
          <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Rename campaign</h3>
            <div className="field-row">
              <div className="field-group">
                <input
                  type="text"
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") confirmRename();
                  }}
                  placeholder="Campaign name"
                />
              </div>
            </div>
            {renameError && <p className="error-text">{renameError}</p>}
            <div className="action-row">
              <button
                type="button"
                className="mint"
                onClick={confirmRename}
                disabled={renameSaving || !renameValue.trim()}
              >
                <Icon name="check" size={14} />
                {renameSaving ? "Saving…" : "Save"}
              </button>
              <button type="button" onClick={() => setRenaming(null)} disabled={renameSaving}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
