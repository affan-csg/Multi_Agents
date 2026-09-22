import { useEffect, useState, type MouseEvent } from "react";
import { researchApi, type IcpBasicsUpdate, type RunSummary, type SuppressedCompany } from "../../agents/research";
import { useCachedFetch, invalidate, patch } from "../../hooks/useCachedFetch";
import { EmptyState, Icon, Skeleton, TableRowsSkeleton, formatDate } from "../../components/ui";
import { ConfirmModal } from "../../components/ConfirmModal";
import { UsageTab } from "./UsageTab";

const WEIGHT_LABELS: Record<string, string> = {
  hiring_need: "Hiring need",
  fill_pain: "Fill pain",
  firmographic_fit: "Firmographic fit",
  buying_signals: "Buying signals",
};

const WEIGHT_COLORS: Record<string, string> = {
  hiring_need: "#5145e8",
  fill_pain: "#23c08b",
  firmographic_fit: "#8b5cf6",
  buying_signals: "#f0ac3f",
};

const DOMAIN_RE = /^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i;

// Mirrors the backend's normalize_domain (Research_Agent/src/csg/ats/__init__.py)
// so what we validate here is exactly what ends up stored.
function normalizeDomain(raw: string): string {
  let s = raw.trim().toLowerCase();
  if (!s) return "";
  if (s.includes("://")) {
    try {
      s = new URL(s).host;
    } catch {
      return "";
    }
  }
  s = s.split("/")[0];
  if (s.startsWith("www.")) s = s.slice(4);
  return s;
}

function isValidDomain(domain: string): boolean {
  return DOMAIN_RE.test(domain);
}

export function Settings({
  onRunDeleted,
  onRunResumed,
}: { onRunDeleted?: () => void; onRunResumed?: (jobId: string, runId: string) => void } = {}) {
  const { data: settings } = useCachedFetch("settings", () => researchApi.getSettings());

  const [editingIcp, setEditingIcp] = useState(false);
  const [icpForm, setIcpForm] = useState<{ sweet_min: string; sweet_max: string; revenue_sweet_min: string } | null>(null);
  const [icpSaving, setIcpSaving] = useState(false);
  const [icpError, setIcpError] = useState<string | null>(null);

  function startEditIcp() {
    if (!settings) return;
    const b = settings.icp_basics;
    setIcpForm({
      sweet_min: String(b.sweet_min),
      sweet_max: String(b.sweet_max),
      revenue_sweet_min: b.revenue_sweet_min != null ? String(b.revenue_sweet_min) : "",
    });
    setIcpError(null);
    setEditingIcp(true);
  }

  function cancelEditIcp() {
    setEditingIcp(false);
    setIcpForm(null);
    setIcpError(null);
  }

  async function saveIcp() {
    if (!icpForm) return;
    setIcpError(null);
    const fields: IcpBasicsUpdate = {
      sweet_min: Number(icpForm.sweet_min),
      sweet_max: Number(icpForm.sweet_max),
      revenue_sweet_min: Number(icpForm.revenue_sweet_min),
    };
    if (Object.values(fields).some((v) => Number.isNaN(v))) {
      setIcpError("All fields must be filled with valid numbers.");
      return;
    }
    setIcpSaving(true);
    try {
      const { icp_basics } = await researchApi.updateIcpBasics(fields);
      patch<typeof settings>("settings", (cur) => (cur ? { ...cur, icp_basics } : cur));
      setEditingIcp(false);
      setIcpForm(null);
    } catch (e) {
      setIcpError(e instanceof Error ? e.message : String(e));
    } finally {
      setIcpSaving(false);
    }
  }
  const { data: suppressedData, loading: suppressedLoading } = useCachedFetch<SuppressedCompany[]>(
    "suppression",
    () => researchApi.listSuppression(),
  );
  const suppressed = suppressedData ?? [];

  // Same cache key Campaigns/Prospects (Outreach) use for this exact
  // researchApi.listRuns() call, so a rename/delete here is reflected there
  // too without a page reload.
  const { data: runsData, loading: runsLoading } = useCachedFetch<RunSummary[]>(
    "research-runs",
    () => researchApi.listRuns(),
  );
  const runsList = runsData ?? [];

  const [renamingRun, setRenamingRun] = useState<RunSummary | null>(null);
  const [runRenameValue, setRunRenameValue] = useState("");
  const [runRenameSaving, setRunRenameSaving] = useState(false);
  const [runRenameError, setRunRenameError] = useState<string | null>(null);
  const [confirmDeleteRun, setConfirmDeleteRun] = useState<RunSummary | null>(null);
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null);
  const [resumingRunId, setResumingRunId] = useState<string | null>(null);
  const [runMenuOpenId, setRunMenuOpenId] = useState<string | null>(null);
  const [runMenuPos, setRunMenuPos] = useState<{ top: number; right: number } | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  function requestRenameRun(run: RunSummary) {
    setRunMenuOpenId(null);
    setRenamingRun(run);
    setRunRenameValue(run.label || "");
    setRunRenameError(null);
  }

  async function confirmRenameRun() {
    if (!renamingRun) return;
    const label = runRenameValue.trim();
    if (!label) return;
    setRunRenameSaving(true);
    setRunRenameError(null);
    try {
      await researchApi.renameRun(renamingRun.run_id, label);
      patch<RunSummary[]>("research-runs", (cur) =>
        (cur ?? []).map((r) => (r.run_id === renamingRun.run_id ? { ...r, label } : r)),
      );
      setRenamingRun(null);
    } catch (e) {
      setRunRenameError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunRenameSaving(false);
    }
  }

  function requestDeleteRun(run: RunSummary) {
    setRunMenuOpenId(null);
    setConfirmDeleteRun(run);
  }

  async function confirmDeleteRunAction() {
    if (!confirmDeleteRun) return;
    const run = confirmDeleteRun;
    setConfirmDeleteRun(null);
    setDeletingRunId(run.run_id);
    setRunError(null);
    try {
      await researchApi.deleteRun(run.run_id);
      patch<RunSummary[]>("research-runs", (cur) => (cur ?? []).filter((r) => r.run_id !== run.run_id));
      // Companies exclusively scored by this run are now fully gone --
      // scores/qualifications, and (DB cascade) their job postings,
      // contacts, saved-list entries, and suppression rows too. Every page
      // scoped to a run_id (or "all", which aggregates across runs) is stale.
      invalidate("companies");
      invalidate("company-detail");
      invalidate("contacts");
      invalidate("analytics");
      invalidate("suppression");
      // The backend also cascade-deletes this run's data on the Outreach
      // side (campaigns, campaign items, and cached prospects it imported --
      // see Research_Agent's api.delete_run / Outreach_Agent's DELETE
      // /research/runs/{run_id}), so every Outreach page reading that data
      // needs to refetch too, not just Research's own pages above.
      invalidate("prospects");
      invalidate("campaigns");
      invalidate("campaign-items");
      invalidate("outreach-analytics");
      // Saved companies are plain App-level state, not useCachedFetch --
      // invalidate() can't reach them, so the parent refetches directly.
      onRunDeleted?.();
    } catch (e) {
      setRunError(e instanceof Error ? e.message : String(e));
      invalidate("research-runs");
    } finally {
      setDeletingRunId(null);
    }
  }

  async function resumeRunAction(run: RunSummary) {
    setRunMenuOpenId(null);
    setResumingRunId(run.run_id);
    setRunError(null);
    try {
      const { job_id } = await researchApi.resumeRun(run.run_id);
      onRunResumed?.(job_id, run.run_id);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : String(e));
    } finally {
      setResumingRunId(null);
    }
  }

  const [newDomain, setNewDomain] = useState("");
  const [newReason, setNewReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  async function handleAdd() {
    setError(null);
    setSuccess(null);

    const raw = newDomain.trim();
    if (!raw) {
      setError("Enter a domain to suppress.");
      return;
    }

    const domain = normalizeDomain(raw);
    if (!domain || !isValidDomain(domain)) {
      setError(`"${raw}" doesn't look like a valid domain. Try something like example.com.`);
      return;
    }

    const reason = newReason.trim() || "manual";
    try {
      await researchApi.addSuppression(domain, reason);
      setNewDomain("");
      setNewReason("");
      setSuccess(`${domain} suppressed successfully.`);
      patch<SuppressedCompany[]>("suppression", (cur) => [
        { domain, reason, added_at: new Date().toISOString(), company_name: null },
        ...(cur ?? []).filter((s) => s.domain !== domain),
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleRemove(domain: string) {
    setError(null);
    setSuccess(null);
    try {
      await researchApi.removeSuppression(domain);
      setSuccess(`${domain} removed from suppression list.`);
      patch<SuppressedCompany[]>("suppression", (cur) => (cur ?? []).filter((s) => s.domain !== domain));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    if (!error && !success) return;
    const timer = setTimeout(() => {
      setError(null);
      setSuccess(null);
    }, 4000);
    return () => clearTimeout(timer);
  }, [error, success]);

  const [activeTab, setActiveTab] = useState<"general" | "usage">("general");

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Research Agent / <span>Settings</span></p>
          <h1>Settings</h1>
          <p>Scoring profile, API keys, research runs, and suppression list.</p>
        </div>
      </div>

      <div className="settings-tabs">
        <button
          type="button"
          className={`settings-tab${activeTab === "general" ? " settings-tab--active" : ""}`}
          onClick={() => setActiveTab("general")}
        >
          <Icon name="settings" size={14} />
          General
        </button>
        <button
          type="button"
          className={`settings-tab${activeTab === "usage" ? " settings-tab--active" : ""}`}
          onClick={() => setActiveTab("usage")}
        >
          <Icon name="database" size={14} />
          Usage &amp; Quota
        </button>
      </div>

      {activeTab === "usage" ? (
        <UsageTab />
      ) : (
      <div className="settings-grid">
        <div className="card">
          <h3 className="card-title">
            <Icon name="target" size={16} />
            Scoring profile
          </h3>
          <p className="card-sub">
            {settings ? `Profile: ${settings.profile || "default"} (read-only — edit config/icp.yaml to change)` : "Loading…"}
          </p>

          {!settings ? (
            <>
              <Skeleton width="100%" height={14} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={14} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={14} />
            </>
          ) : (
            <>
              <div className="weight-bars">
                {Object.entries(settings.weights).map(([key, value]) => (
                  <div className="weight-row" key={key}>
                    <div className="weight-row-head">
                      <span>{WEIGHT_LABELS[key] || key}</span>
                      <span className="weight-row-value">{value}%</span>
                    </div>
                    <div className="weight-bar">
                      <div
                        className="weight-bar-fill"
                        style={{ width: `${value}%`, background: WEIGHT_COLORS[key] || "#8e96a8" }}
                      />
                    </div>
                  </div>
                ))}
              </div>

              <h4 style={{ marginTop: 22 }}>Tier thresholds</h4>
              <div className="tier-thresholds">
                {Object.entries(settings.tiers).map(([tier, min]) => (
                  <div className="tier-threshold" key={tier}>
                    <span className={`tier-badge tier-${tier}`}>{tier}</span>
                    <span className="hint">score ≥ {min}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="database" size={16} />
            API keys
          </h3>
          <p className="card-sub">Which integrations are configured. Values are always masked — never shown in full.</p>

          {!settings ? (
            <TableRowsSkeleton columns={2} rows={5} />
          ) : (
            <div className="key-list">
              {settings.api_keys.map((k) => (
                <div className="key-row" key={k.name}>
                  <div className="key-row-name">
                    <span className={`key-dot${k.configured ? " key-dot--on" : ""}`} />
                    {k.name}
                  </div>
                  <div className="key-row-value">
                    {k.configured ? <code>{k.masked}</code> : <span className="hint">not set</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card settings-span">
          <div className="card-title-row">
            <h3 className="card-title">
              <Icon name="pin" size={16} />
              Built-in ICP
            </h3>
            {settings && !editingIcp && (
              <button type="button" className="drawer-section-action" onClick={startEditIcp}>
                <Icon name="edit" size={12} />
                Edit
              </button>
            )}
          </div>
          <p className="card-sub">
            Every ICP criterion built into scoring. Only the Employee Size Band and Revenue Target are editable
            here — everything else is shown for reference and requires editing <code>config/icp.yaml</code>{" "}
            directly to change.
          </p>

          {!settings ? (
            <>
              <Skeleton width="100%" height={14} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={14} style={{ marginBottom: 10 }} />
              <Skeleton width="100%" height={14} />
            </>
          ) : (
            <>
              {editingIcp && icpForm ? (
                <div className="drawer-panel" style={{ marginTop: 0, marginBottom: 18 }}>
                  <div className="field-row">
                    <div className="field-group">
                      <label>Employee size band — min</label>
                      <input
                        type="number" min={1}
                        value={icpForm.sweet_min}
                        onChange={(e) => setIcpForm({ ...icpForm, sweet_min: e.target.value })}
                      />
                    </div>
                    <div className="field-group">
                      <label>Employee size band — max</label>
                      <input
                        type="number" min={1}
                        value={icpForm.sweet_max}
                        onChange={(e) => setIcpForm({ ...icpForm, sweet_max: e.target.value })}
                      />
                    </div>
                    <div className="field-group">
                      <label>Revenue target ($)</label>
                      <input
                        type="number" min={1}
                        value={icpForm.revenue_sweet_min}
                        onChange={(e) => setIcpForm({ ...icpForm, revenue_sweet_min: e.target.value })}
                      />
                    </div>
                  </div>
                  <p className="hint">
                    Both are soft ranking signals, never disqualifiers — a company outside the size band or
                    below the revenue target still gets partial credit scaled to how close it is, and can still
                    score Tier A on hiring demand alone.
                  </p>
                  {icpError && <p className="error-text">{icpError}</p>}
                  <div className="action-row">
                    <button type="button" className="mint" onClick={saveIcp} disabled={icpSaving}>
                      <Icon name="check" size={14} />
                      {icpSaving ? "Saving…" : "Save"}
                    </button>
                    <button type="button" onClick={cancelEditIcp} disabled={icpSaving}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="drawer-fact-grid" style={{ marginBottom: 18 }}>
                  <div>
                    <span className="drawer-fact-label">Employee size band</span>
                    <span className="drawer-fact-value">
                      {settings.icp_basics.sweet_min}–{settings.icp_basics.sweet_max} employees
                    </span>
                  </div>
                  <div>
                    <span className="drawer-fact-label">Revenue target</span>
                    <span className="drawer-fact-value">
                      {settings.icp_basics.revenue_sweet_min != null
                        ? `$${(settings.icp_basics.revenue_sweet_min / 1_000_000).toFixed(0)}M+`
                        : "—"}
                    </span>
                  </div>
                </div>
              )}

              <h4 style={{ marginTop: 0, marginBottom: 12 }}>Read-only</h4>
              <div className="drawer-fact-grid">
                <div>
                  <span className="drawer-fact-label">Active region</span>
                  <span className="drawer-fact-value">
                    {settings.icp_basics.available_regions.find((r) => r.key === settings.icp_basics.active_region)?.label
                      ?? settings.icp_basics.active_region}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">Employee count hard limits</span>
                  <span className="drawer-fact-value">
                    {settings.icp_basics.hard_min.toLocaleString()}–{settings.icp_basics.hard_max.toLocaleString()}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">Funding</span>
                  <span className="drawer-fact-value">
                    {settings.icp_basics.funding_preferred ? "Preferred, not required" : "Not a factor"}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">Tier thresholds</span>
                  <span className="drawer-fact-value">
                    A≥{settings.icp_basics.tier_a_threshold} · B≥{settings.icp_basics.tier_b_threshold} · C≥{settings.icp_basics.tier_c_threshold}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">Target industries</span>
                  <span className="drawer-fact-value">
                    {settings.icp_basics.target_industries.length > 0
                      ? settings.icp_basics.target_industries.join(", ")
                      : "All (unrestricted)"}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">Require US presence</span>
                  <span className="drawer-fact-value">{settings.icp_basics.require_us_presence ? "Yes" : "No"}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Min relevant reqs</span>
                  <span className="drawer-fact-value">{settings.icp_basics.min_relevant_reqs ?? "—"}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Competitor recruiter-req share</span>
                  <span className="drawer-fact-value">
                    {settings.icp_basics.competitor_recruiter_req_share != null
                      ? `${(settings.icp_basics.competitor_recruiter_req_share * 100).toFixed(0)}%`
                      : "—"}
                  </span>
                </div>
              </div>

              <h4 style={{ marginTop: 22, marginBottom: 12 }}>Practice areas ({settings.icp_basics.practice_areas.length})</h4>
              <div className="chip-row">
                {settings.icp_basics.practice_areas.map((a) => (
                  <span key={a} className="chip chip-static chip-check">
                    <Icon name="check" size={12} />
                    {a.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="card settings-span">
          <h3 className="card-title">
            <Icon name="list" size={16} />
            Research runs
          </h3>
          <p className="card-sub">
            Every research run so far. Rename a run to make it easier to find later, or delete one to permanently
            remove its companies, job postings, contacts, and saved entries — anything also found by another run
            is kept, since deleting this one must not break that run's results.
          </p>

          {runError && <p className="error-text">{runError}</p>}

          {!runsLoading && runsList.length === 0 ? (
            <EmptyState icon="play" message="No research runs yet — start one from the Runs page." />
          ) : (
            <div className="table-scroll">
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Region</th>
                    <th>Started</th>
                    <th>Finished</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {runsLoading ? (
                    <TableRowsSkeleton columns={5} rows={3} />
                  ) : (
                    runsList.map((r) => (
                      <tr key={r.run_id}>
                        <td style={{ fontWeight: 550 }}>{r.label || r.run_id}</td>
                        <td className="cell-muted">{r.profile || "—"}</td>
                        <td className="cell-muted">{formatDate(r.started_at)}</td>
                        <td className="cell-muted">{r.finished_at ? formatDate(r.finished_at) : "in progress"}</td>
                        <td style={{ textAlign: "right" }}>
                          <div className="menu-wrap" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              className="ghost menu-trigger"
                              title="More actions"
                              disabled={deletingRunId === r.run_id || resumingRunId === r.run_id}
                              onClick={(e: MouseEvent<HTMLButtonElement>) => {
                                if (runMenuOpenId === r.run_id) {
                                  setRunMenuOpenId(null);
                                  return;
                                }
                                const rect = e.currentTarget.getBoundingClientRect();
                                setRunMenuPos({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
                                setRunMenuOpenId(r.run_id);
                              }}
                            >
                              <Icon name="more" size={16} />
                            </button>
                            {runMenuOpenId === r.run_id && runMenuPos && (
                              <>
                                <div className="menu-backdrop" onClick={() => setRunMenuOpenId(null)} />
                                <div
                                  className="menu-popover menu-popover--fixed"
                                  style={{ top: runMenuPos.top, right: runMenuPos.right }}
                                >
                                  <button type="button" className="menu-item" onClick={() => requestRenameRun(r)}>
                                    <Icon name="edit" size={14} />
                                    Rename run
                                  </button>
                                  {r.finished_at && (
                                    <>
                                      <a
                                        className="menu-item"
                                        style={{ textDecoration: "none" }}
                                        href={researchApi.exportExcelUrl(r.run_id)}
                                        onClick={() => setRunMenuOpenId(null)}
                                      >
                                        <Icon name="export" size={14} />
                                        Export: Excel — Companies &amp; Contacts
                                      </a>
                                      <a
                                        className="menu-item"
                                        style={{ textDecoration: "none" }}
                                        href={researchApi.exportPdfUrl(r.run_id)}
                                        onClick={() => setRunMenuOpenId(null)}
                                      >
                                        <Icon name="export" size={14} />
                                        Export: PDF Reports — All Companies
                                      </a>
                                    </>
                                  )}
                                  {r.stalled_count > 0 && (
                                    <button
                                      type="button"
                                      className="menu-item"
                                      onClick={() => resumeRunAction(r)}
                                      title={`${r.stalled_count} compan${r.stalled_count === 1 ? "y" : "ies"} stalled with no ATS board or firmographics -- likely an exhausted API provider. Resuming retries just these.`}
                                    >
                                      <Icon name="refresh" size={14} />
                                      Resume research ({r.stalled_count})
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    className="menu-item menu-item--danger"
                                    onClick={() => requestDeleteRun(r)}
                                  >
                                    <Icon name="trash" size={14} />
                                    Delete run
                                  </button>
                                </div>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="card settings-span">
          <h3 className="card-title">
            <Icon name="filter" size={16} />
            Suppression list
          </h3>
          <p className="card-sub">
            Suppressed domains are skipped on every future research run — existing clients, DNC requests, open deals.
          </p>

          <div className="field-row" style={{ marginBottom: 16 }}>
            <div className="field-group" style={{ flex: 2 }}>
              <label>Domain</label>
              <input
                value={newDomain}
                onChange={(e) => {
                  setNewDomain(e.target.value);
                  setError(null);
                  setSuccess(null);
                }}
                placeholder="e.g. example.com"
                onKeyDown={(e) => e.key === "Enter" && handleAdd()}
              />
            </div>
            <div className="field-group" style={{ flex: 2 }}>
              <label>Reason</label>
              <input
                value={newReason}
                onChange={(e) => setNewReason(e.target.value)}
                placeholder="e.g. existing client"
                onKeyDown={(e) => e.key === "Enter" && handleAdd()}
              />
            </div>
            <div className="field-group" style={{ flex: "0 0 auto" }}>
              <label style={{ visibility: "hidden" }}>Suppress</label>
              <button type="button" className="primary" onClick={handleAdd}>
                <Icon name="plus" size={14} />
                Suppress
              </button>
            </div>
          </div>
          {error && <p className="error-text">{error}</p>}
          {!error && success && <p className="success-text">{success}</p>}

          {!suppressedLoading && suppressed.length === 0 ? (
            <EmptyState icon="close" message="Nothing suppressed yet." />
          ) : (
            <div className="table-scroll">
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Domain</th>
                    <th>Reason</th>
                    <th>Added</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {suppressedLoading ? (
                    <TableRowsSkeleton columns={4} rows={3} />
                  ) : (
                    suppressed.map((s) => (
                      <tr key={s.domain}>
                        <td style={{ fontWeight: 550 }}>{s.company_name || s.domain}</td>
                        <td className="cell-muted">{s.reason || "—"}</td>
                        <td className="cell-muted">{formatDate(s.added_at)}</td>
                        <td style={{ textAlign: "right" }}>
                          <button
                            type="button"
                            className="danger"
                            onClick={() => handleRemove(s.domain)}
                            title="Remove from suppression list"
                          >
                            <Icon name="refresh" size={13} />
                            Un-suppress
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
      )}

      {renamingRun && (
        <div className="drawer-overlay" onClick={() => setRenamingRun(null)}>
          <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Rename run</h3>
            <div className="field-row">
              <div className="field-group">
                <input
                  type="text"
                  autoFocus
                  value={runRenameValue}
                  onChange={(e) => setRunRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") confirmRenameRun();
                  }}
                  placeholder="Run name"
                />
              </div>
            </div>
            {runRenameError && <p className="error-text">{runRenameError}</p>}
            <div className="action-row">
              <button
                type="button"
                className="mint"
                onClick={confirmRenameRun}
                disabled={runRenameSaving || !runRenameValue.trim()}
              >
                <Icon name="check" size={14} />
                {runRenameSaving ? "Saving…" : "Save"}
              </button>
              <button type="button" onClick={() => setRenamingRun(null)} disabled={runRenameSaving}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmDeleteRun && (
        <ConfirmModal
          title="Delete research run?"
          message={`Delete "${confirmDeleteRun.label || confirmDeleteRun.run_id}"? This permanently removes every company, job posting, contact, and saved entry that only this run found. Anything also found by another run is kept. This cannot be undone.`}
          danger
          confirmLabel="Delete"
          onConfirm={confirmDeleteRunAction}
          onCancel={() => setConfirmDeleteRun(null)}
        />
      )}
    </>
  );
}
