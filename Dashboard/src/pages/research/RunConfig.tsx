import { Fragment, useEffect, useState } from "react";
import type { DiscoverRequest, JobSnapshot, RegionOptions } from "../../agents/research";
import { Icon } from "../../components/ui";
import type { IconName } from "../../components/ui";

const STEPS: { label: string; sub: string; icon: IconName; tone: "mint" | "blue" | "accent" | "violet" }[] = [
  { label: "Discover", sub: "Find companies in region", icon: "search", tone: "mint" },
  { label: "Research", sub: "Size, revenue, funding", icon: "list", tone: "blue" },
  { label: "ATS + jobs", sub: "Detect ATS and open roles", icon: "database", tone: "accent" },
  { label: "Score", sub: "Rank against ICP", icon: "star", tone: "violet" },
  { label: "Qualify + contacts", sub: "AI qualify, Hunter.io lookup", icon: "sparkle", tone: "violet" },
  { label: "Prospects", sub: "Import into Outreach", icon: "target", tone: "accent" },
  { label: "Campaign", sub: "Draft outreach campaign", icon: "mail", tone: "mint" },
];

interface Props {
  regions: RegionOptions | null;
  activeJob: JobSnapshot | null;
  error: string | null;
  onDiscover: (req: DiscoverRequest) => void;
  /** Bumped by the parent after a run completes successfully, so this form
   * clears back to defaults for the next run instead of showing stale
   * values from the one that just finished. */
  resetKey?: number;
}

export function RunConfig({ regions, activeJob, error, onDiscover, resetKey }: Props) {
  const [label, setLabel] = useState("");
  const [region, setRegion] = useState<string | null>(null);
  const [limit, setLimit] = useState("100");
  const [topN, setTopN] = useState("25");

  useEffect(() => {
    if (resetKey === undefined || resetKey === 0) return;
    setLabel("");
    setRegion(null);
    setLimit("100");
    setTopN("25");
  }, [resetKey]);

  const running = activeJob?.status === "running" || activeJob?.status === "pending";
  const activeRegion = region ?? regions?.active ?? null;
  const selectedRegion = regions?.regions.find((r) => r.key === activeRegion) ?? null;

  // Map the job's current backend stage (prefixed onto current_domain as
  // "stage: detail" by api.py's job progress callback) onto the 7 UI steps,
  // so the pipeline strip actually advances/highlights live instead of
  // sitting frozen on step 2 for the whole run. "pipeline" covers both ATS+
  // jobs and Score on the backend (see pipeline.py _process_one), so within
  // that stage we split by progress: first half reads as "ATS + jobs",
  // second half as "Score" -- an approximation, not a real 6th backend stage.
  // "outreach" is the Research -> Outreach hand-off (see discover.py's
  // _handoff_to_outreach): done=0 is importing prospects (step 5), done>=1
  // is drafting the campaign (step 6) -- it only fires when the run found
  // qualifying prospects and OUTREACH_API_BASE is configured, so a run
  // without a hand-off just stops advancing after "Qualify + contacts".
  const backendStage = activeJob?.current_domain?.split(":")[0]?.trim();
  const stepIndex = !activeJob
    ? 0
    : activeJob.status === "done"
      ? STEPS.length - 1
      : backendStage === "discover"
        ? 0
        : backendStage === "enrich"
          ? 1
          : backendStage === "pipeline"
            ? (activeJob.total > 0 && activeJob.done / activeJob.total >= 0.5 ? 3 : 2)
            : backendStage === "qualify" || backendStage === "contacts"
              ? 4
              : backendStage === "outreach"
                ? (activeJob.done >= 1 ? 6 : 5)
                : 0;
  const target = Number(limit) || 0;
  const topNTarget = Number(topN) || 0;
  const estimatedCalls = target * 7;

  function start() {
    onDiscover({
      region: activeRegion,
      limit: target || 100,
      label: label.trim() || undefined,
      // An empty field means "use the automatic default cap" (send
      // undefined, so the backend's auto_pipeline default applies) --
      // an explicit 0 means "skip qualify/contacts entirely" and must be
      // sent as a real 0, not collapsed into undefined the same way, or the
      // backend can't tell "not provided" from "explicitly zero".
      auto_qualify_top: topN.trim() === "" ? undefined : topNTarget,
    });
  }

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <span className="eyebrow-pill">CSG Research Agent</span>
          <h1>Research runs</h1>
          <p>
            Pick a region and a Top N, and the agent discovers, researches, scores, and ranks matching
            companies on its own — using the ICP built into its configuration — then qualifies the top
            prospects with AI and finds their contacts via Hunter.io.
          </p>
        </div>
        <div className="page-head-actions">
          <div className="step-count">
            <span>{stepIndex + 1} of {STEPS.length}</span>
            <span className="step-count-dots">
              {STEPS.map((_, i) => (
                <span
                  key={i}
                  className={`step-count-dot${i === stepIndex ? " step-count-dot--active" : ""}${i < stepIndex ? " step-count-dot--done" : ""}${i === stepIndex && running ? " step-count-dot--running" : ""}`}
                >
                  {i < stepIndex ? <Icon name="check" size={11} /> : i + 1}
                </span>
              ))}
            </span>
          </div>
          <span className={`status-pill status-pill--live${running ? " status-pill--glow" : ""}`}>
            <Icon name={running ? "refresh" : "check"} size={12} className={running ? "icon-spin" : undefined} />
            {running ? "Running…" : "Pipeline ready"}
          </span>
        </div>
      </div>

      <div className="pipeline-strip">
        {STEPS.map((step, i) => (
          <Fragment key={step.label}>
            <div
              className={`pipeline-step pipeline-step--${step.tone}${i === stepIndex ? " pipeline-step--active" : ""}${i < stepIndex ? " pipeline-step--done" : ""}${i === stepIndex && running ? " pipeline-step--running" : ""}`}
            >
              <span className="pipeline-step-num">{i < stepIndex ? <Icon name="check" size={12} /> : i + 1}</span>
              <span className="pipeline-step-icon">
                <Icon name={step.icon} size={17} />
              </span>
              <div>
                <div className="pipeline-step-label">{step.label}</div>
                <div className="pipeline-step-sub">{step.sub}</div>
              </div>
            </div>
            {i < STEPS.length - 1 && (
              <div className={`pipeline-chevron pipeline-chevron--${STEPS[i + 1].tone}`} />
            )}
          </Fragment>
        ))}
      </div>

      <div className="run-layout">
        <div className="card">
          <h3 className="card-title">
            <span className="card-title-icon">
              <Icon name="pin" size={16} />
            </span>
            Region brief
          </h3>
          <p className="card-sub">
            The region is the only thing you choose. Company size, revenue, hiring signals, and
            priority rules are already built in.
          </p>

          <div className="field-group">
            <label htmlFor="run-label">Name this research</label>
            <input
              id="run-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Georgia Q4"
            />
            <p className="hint" style={{ marginTop: 8 }}>
              Optional — lets you find this run later in Companies, Saved lists, Contacts, and Analytics.
            </p>
          </div>

          <div className="field-group">
            <label>Region</label>
            <div className="region-grid">
              {(regions?.regions ?? []).map((r) => (
                <button
                  key={r.key}
                  type="button"
                  className={`region-card${r.key === activeRegion ? " region-card--active" : ""}`}
                  onClick={() => setRegion(r.key)}
                >
                  <span className="region-card-radio" aria-hidden="true" />
                  <span className="region-card-label">{r.label}</span>
                  <span className="region-card-hubs">{r.hubs.slice(0, 3).join(", ")}</span>
                </button>
              ))}
              {!regions && <span className="hint">Loading regions…</span>}
            </div>
            {selectedRegion && (
              <p className="hint" style={{ marginTop: 8 }}>
                Searching {selectedRegion.hubs.slice(0, 4).join(", ")}
                {selectedRegion.hubs.length > 4 && ` +${selectedRegion.hubs.length - 4} more`} ·{" "}
                {selectedRegion.states.length} state{selectedRegion.states.length === 1 ? "" : "s"}
              </p>
            )}
          </div>

          <div className="field-row" style={{ marginTop: 22 }}>
            <div className="field-group">
              <label htmlFor="discover-limit">Max companies to discover</label>
              <input
                id="discover-limit"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
                type="number"
                min={1}
                placeholder="100"
              />
            </div>
            <div className="field-group">
              <label htmlFor="top-n">Top N to qualify + find contacts</label>
              <input
                id="top-n"
                value={topN}
                onChange={(e) => setTopN(e.target.value)}
                type="number"
                min={0}
                placeholder="25"
              />
              <p className="hint" style={{ marginTop: 8 }}>
                Every Tier A/B company is automatically qualified with AI and looked up on
                Hunter.io after scoring — this just caps how many (highest score first). Set to 0
                to skip qualify/contacts entirely for this run. Hunter.io's free tier is 25
                searches/month.
              </p>
            </div>
          </div>

          {regions && (
            <div className="field-group" style={{ marginTop: 22 }}>
              <label style={{ marginBottom: 12 }}>Built-in ICP</label>
              <div className="chip-row">
                <span className="chip chip-static chip-check">
                  <Icon name="check" size={12} />
                  {regions.icp.size_band[0]} to {regions.icp.size_band[1]} employees
                </span>
                {regions.icp.revenue_min != null && (
                  <span className="chip chip-static chip-check">
                    <Icon name="check" size={12} />
                    ${(regions.icp.revenue_min / 1_000_000).toFixed(0)}M+ revenue
                  </span>
                )}
                <span className="chip chip-static chip-check">
                  <Icon name="check" size={12} />
                  {regions.icp.practice_areas.length} practice areas
                </span>
              </div>
              <p className="hint" style={{ marginTop: 8 }}>
                Edit <code>config/icp.yaml</code> to change these — they apply to every run.
              </p>
            </div>
          )}

          {error && (
            <p className="error-text" style={{ display: "flex", alignItems: "flex-start", gap: 7 }}>
              <Icon name="close" size={13} className="error-text-icon" />
              <span style={{ whiteSpace: "pre-line" }}>
                <strong style={{ marginRight: 4 }}>Run failed:</strong>
                {error}
              </span>
            </p>
          )}
        </div>

        <div className="summary-card">
          <div className="summary-title">
            <Icon name="target" size={17} />
            Run summary
          </div>

          <div className="summary-stat">
            <span className="summary-stat-icon">
              <Icon name="building" size={16} />
            </span>
            <div>
              <div className="summary-stat-label">Discovery target</div>
              <div className="summary-stat-value">{target.toLocaleString()}</div>
              <div className="summary-stat-note">companies</div>
            </div>
          </div>

          <div className="summary-stat">
            <span className="summary-stat-icon">
              <Icon name="database" size={16} />
            </span>
            <div>
              <div className="summary-stat-label">Estimated API calls</div>
              <div className="summary-stat-value">~{estimatedCalls.toLocaleString()}</div>
              <div className="summary-stat-note">varies by results</div>
            </div>
          </div>

          <div className="summary-info-row">
            <div className="summary-info-box">
              <Icon name="refresh" size={15} />
              <div>
                <div className="summary-info-title">Resumable</div>
                <div className="summary-info-sub">Pause and continue</div>
              </div>
            </div>
            <div className="summary-info-box">
              <Icon name="briefcase" size={15} />
              <div>
                <div className="summary-info-title">ICP built in</div>
                <div className="summary-info-sub">No manual setup</div>
              </div>
            </div>
          </div>

          <div className="summary-filters">
            <div className="summary-filter-row">
              <div>
                <div className="summary-filter-label">Region</div>
                <div className="summary-filter-value">
                  {selectedRegion?.label ?? activeRegion ?? "—"}
                </div>
              </div>
              {selectedRegion && <span className="summary-count">{selectedRegion.states.length}</span>}
            </div>
          </div>

          <div className="summary-ready">
            <div className="summary-ready-title">
              <Icon name="sparkle" size={15} />
              {running ? "Run in progress" : "Ready to run"}
            </div>
            <p>
              {running
                ? backendStage === "outreach"
                  // done/total here are the 2-step outreach hand-off (0/1, 1/2, 2/2),
                  // not discovery counts -- showing "1 of 2" would be more confusing
                  // than the detail text alone (see discover.py's _handoff_to_outreach).
                  ? `${activeJob?.current_domain?.split(":").slice(1).join(":").trim() ?? "Handing off to Outreach"}…`
                  : activeJob?.current_domain
                    ? `${activeJob.current_domain} — ${activeJob.done ?? 0} of ${activeJob.total ?? 0}.`
                    : `Processing ${activeJob?.done ?? 0} of ${activeJob?.total ?? 0}.`
                : topNTarget > 0
                  ? `The agent will search the region, research and rank each company, then qualify and find contacts for the top ${topNTarget} on its own.`
                  : "The agent will search the region, research each company, and rank them against the built-in ICP."}
            </p>
          </div>

          <button type="button" className="mint" onClick={start} disabled={running}>
            <Icon name="play" size={15} />
            {running ? "Running…" : "Start discovery"}
          </button>

          <p className="summary-footnote">
            <Icon name="info" size={13} />
            {topNTarget > 0
              ? `Qualify + Hunter.io contacts run automatically for the top ${topNTarget} Tier A/B companies (free tier: 25 Hunter.io searches/month). Set "Top N" to 0 to skip.`
              : "Qualify and Hunter.io contacts are skipped this run (Top N is 0) — you can still run them later from a company's page."}
          </p>
        </div>
      </div>

      <div className="preview-strip">
        <div className="preview-strip-text">
          <div className="preview-strip-title">
            <Icon name="sparkle" size={16} />
            Preview of what you will get
          </div>
          <p>Each run turns your region into a prioritized list of ready-to-engage prospects.</p>
        </div>
        <div className="preview-cards">
          <div className="preview-card preview-card--mint">
            <span className="preview-card-icon">
              <Icon name="building" size={17} />
            </span>
            <div>
              <div className="preview-card-head">
                <span className="preview-card-label">Companies researched</span>
                <span className="preview-card-count">{target ? `${target}+` : "100+"}</span>
              </div>
              <div className="preview-card-sub">Size, revenue, funding, and more.</div>
            </div>
          </div>
          <div className="preview-card preview-card--blue">
            <span className="preview-card-icon">
              <Icon name="database" size={17} />
            </span>
            <div>
              <div className="preview-card-head">
                <span className="preview-card-label">ATS and live roles</span>
                <span className="preview-card-count">{target ? `${target}+` : "100+"}</span>
              </div>
              <div className="preview-card-sub">Detect ATS, find open positions.</div>
            </div>
          </div>
          <div className="preview-card preview-card--violet">
            <span className="preview-card-icon">
              <Icon name="sparkle" size={17} />
            </span>
            <div>
              <div className="preview-card-head">
                <span className="preview-card-label">Qualified + contacted</span>
                <span className="preview-card-count">{topNTarget || 0}</span>
              </div>
              <div className="preview-card-sub">Top Tier A/B prospects, AI pain summary + Hunter.io contact.</div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
