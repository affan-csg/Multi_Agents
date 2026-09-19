import { useCallback, useEffect, useState } from "react";
import { useCachedFetch, invalidate, patchPrefix } from "./hooks/useCachedFetch";
import {
  researchApi,
  type CompanyFilter,
  type Contact,
  type DiscoverRequest,
  type JobSnapshot as ResearchJobSnapshot,
  type RegionOptions,
  type RunResult,
  type RunSummary,
  type SavedCompany,
  type ScoredCompany,
} from "./agents/research";
import { outreachApi, type Campaign } from "./agents/outreach";
import { Nav } from "./components/Nav";
import { CompanyDrawer } from "./components/CompanyDrawer";
import { ConfirmModal } from "./components/ConfirmModal";
import { RunOverlay } from "./components/RunOverlay";
import { ToastStack, type ToastData } from "./components/Toast";
import { RunSelector } from "./components/RunSelector";
import { CampaignSelector } from "./components/CampaignSelector";
import { Home } from "./pages/Home";
import { RunConfig } from "./pages/research/RunConfig";
import { Companies } from "./pages/research/Companies";
import { CompanyDetail } from "./pages/research/CompanyDetail";
import { Contacts } from "./pages/research/Contacts";
import { SavedLists } from "./pages/research/SavedLists";
import { Settings } from "./pages/research/Settings";
import { Analytics } from "./pages/research/Analytics";
import { Prospects } from "./pages/outreach/Prospects";
import { Campaigns } from "./pages/outreach/Campaigns";
import { CampaignDetail } from "./pages/outreach/CampaignDetail";
import { Analytics as OutreachAnalytics } from "./pages/outreach/Analytics";
import { useRoute } from "./router";
import "./App.css";

type PendingAction = { kind: "qualify" | "contacts"; domains?: string[] };

const JOB_KIND_LABEL: Record<string, string> = {
  discover: "Research run",
  qualify: "Qualification",
  contacts: "Contact discovery",
};

function App() {
  const [route, navigate] = useRoute();
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [apiStatus, setApiStatus] = useState<Record<string, boolean | null>>({ research: null, outreach: null });

  const [regions, setRegions] = useState<RegionOptions | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);

  const { data: campaignsData } = useCachedFetch<Campaign[]>("campaigns", () => outreachApi.listCampaigns());
  const campaigns = campaignsData ?? [];
  const [scopeCampaign, setScopeCampaign] = useState<string | "all">("all");

  const [activeJob, setActiveJob] = useState<ResearchJobSnapshot | null>(null);
  const [scopeRun, setScopeRun] = useState<string | "all">("all");
  const runId = scopeRun === "all" ? "all" : scopeRun;
  const actionRunId = scopeRun === "all" ? (runs[0]?.run_id ?? null) : scopeRun;

  const [companyFilter, setCompanyFilter] = useState<CompanyFilter>({});
  const companiesActive = route.name === "research-companies" || route.name === "research-company";
  const companiesKey = companiesActive && runId ? `companies:${runId}:${JSON.stringify(companyFilter)}` : null;
  const { data: rowsData, loading: rowsLoading } = useCachedFetch<ScoredCompany[]>(
    companiesKey,
    () => researchApi.runCompanies(runId, companyFilter),
  );
  const rows = rowsData ?? [];

  const [selectedDomain, setSelectedDomain] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [runFormResetKey, setRunFormResetKey] = useState(0);

  const [toasts, setToasts] = useState<ToastData[]>([]);
  const pushToast = useCallback((toast: Omit<ToastData, "id">) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setToasts((cur) => [...cur, { ...toast, id }]);
  }, []);
  const dismissToast = useCallback((id: string) => {
    setToasts((cur) => cur.filter((t) => t.id !== id));
  }, []);

  const [saved, setSaved] = useState<SavedCompany[]>([]);
  const savedDomains = new Set(saved.map((s) => s.domain));
  const [, setSavedLoading] = useState(false);

  const contactsActive = route.name === "research-contacts";
  const { data: contactsData, loading: contactsLoading } = useCachedFetch<Contact[]>(
    contactsActive ? `contacts:${scopeRun}` : null,
    () => researchApi.listContacts(scopeRun),
  );
  const contacts = contactsData ?? [];

  const refreshSaved = useCallback((scope: string | "all") => {
    setSavedLoading(true);
    return researchApi
      .listSaved(scope)
      .then(setSaved)
      .catch(() => {})
      .finally(() => setSavedLoading(false));
  }, []);

  useEffect(() => {
    researchApi.regions().then(setRegions).catch(() => {});
    researchApi
      .listRuns()
      .then((list) => {
        setRuns(list);
        setApiStatus((s) => ({ ...s, research: true }));
      })
      .catch(() => setApiStatus((s) => ({ ...s, research: false })));
    // Outreach has no lightweight "am I up" route of its own yet -- listing
    // campaigns doubles as the health check, same as Research's listRuns().
    outreachApi
      .listCampaigns()
      .then(() => setApiStatus((s) => ({ ...s, outreach: true })))
      .catch(() => setApiStatus((s) => ({ ...s, outreach: false })));
    refreshSaved("all");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function toggleSave(domain: string) {
    const isSaved = savedDomains.has(domain);
    const previous = saved;

    if (isSaved) {
      setSaved((prev) => prev.filter((s) => s.domain !== domain));
    } else {
      const row = rows.find((r) => r.domain === domain);
      setSaved((prev) => [
        {
          domain,
          saved_from_run: actionRunId,
          note: null,
          saved_at: new Date().toISOString(),
          company: null,
          score: row ?? null,
        },
        ...prev,
      ]);
    }

    try {
      if (isSaved) {
        await researchApi.unsave(domain);
      } else {
        await researchApi.save(domain, actionRunId);
      }
    } catch {
      setSaved(previous);
    }
  }

  // Builds the toast body for a finished "discover" job -- the run itself
  // always either fully failed (job.status === "error", handled by the
  // caller) or fully succeeded, but the optional auto-qualify-top add-on
  // (qualify + Hunter.io contacts for the top N) can partially fail on its
  // own without failing the run -- see discover.py's _auto_qualify_and_contact,
  // which records {"error": ...} on stats.qualify/contacts rather than
  // raising. Surface that distinction instead of a blanket "success".
  function describeDiscoverResult(result: RunResult | Record<string, unknown> | null): { tone: ToastData["tone"]; message: string } {
    const r = (result || {}) as Partial<RunResult>;
    const parts: string[] = [];
    const problems: string[] = [];

    if (typeof r.discovered === "number") {
      parts.push(`${r.discovered} compan${r.discovered === 1 ? "y" : "ies"} discovered`);
    }
    if (typeof r.scored === "number") {
      parts.push(`${r.scored} scored`);
    }
    if (r.tiers) {
      const ab = (r.tiers.A ?? 0) + (r.tiers.B ?? 0);
      if (ab > 0) parts.push(`${ab} Tier A/B`);
    }

    // Enrichment (Gemini firmographics) failing for every company usually means
    // a missing/exhausted API key -- surface it plainly instead of the run just
    // silently coming back with no employee/revenue data and no explanation.
    const ENRICH_FAILURE_LABEL: Record<string, string> = {
      no_api_key: "no GEMINI_API_KEY (and no GROQ_API_KEY/TAVILY_API_KEY fallback) configured",
      quota_exhausted: "Gemini quota exhausted and Tavily+Groq fallback also failed or is unconfigured",
      rate_limited: "rate limited (429) — retries exhausted",
      connection_error: "connection/TLS error reaching the API",
      timeout: "request timed out",
      parse_error: "enrichment response could not be parsed",
      request_error: "enrichment request failed",
    };
    if (r.enrich_failures && typeof r.enrich_failures === "object") {
      const total = Object.values(r.enrich_failures).reduce((a, b) => a + (b || 0), 0);
      if (total > 0) {
        const reasons = Object.entries(r.enrich_failures)
          .filter(([, n]) => n > 0)
          .map(([reason, n]) => `${ENRICH_FAILURE_LABEL[reason] || reason} (${n})`);
        problems.push(`Enrichment skipped for ${total} compan${total === 1 ? "y" : "ies"}: ${reasons.join(", ")}`);
      }
    }

    // A run can succeed end-to-end and still score nothing new -- most
    // commonly because these companies were already scored in a previous run
    // (discovery re-surfaced the same candidates) or none had ATS/job data
    // worth a high score. Call that out instead of it reading as silent
    // failure when the Companies tab for this run turns up empty.
    if (r.scored === 0 && (r.discovered ?? 0) > 0) {
      problems.push("No companies were newly scored in this run -- they may already be scored from an earlier run, or none had usable job data.");
    }

    // Fill Pain / Hiring Need / Buying Signals / CSG Delivery Fit are entirely
    // job-posting-derived -- a company with no confirmed job signal scores
    // those at 0 and is capped near D-tier regardless of how good a fit it
    // actually is. ATS detection is just the first (highest-precision) rung
    // of a fallback chain -- careers-page scraping and an external web
    // search are tried before a company is ever marked UNKNOWN -- so
    // hiring_signal_unknown (every method exhausted), not no_ats (no ATS
    // board specifically), is what actually means "we have no real job
    // signal for this company." See Research_Agent's jobs_discovery.py and
    // WORKFLOW.md's ATS detection section for the full chain.
    if (typeof r.hiring_signal_unknown === "number" && typeof r.scored === "number" && r.scored > 0) {
      const total = r.scored + (r.disqualified ?? 0);
      if (total > 0 && r.hiring_signal_unknown / total >= 0.5) {
        problems.push(
          `${r.hiring_signal_unknown} of ${total} compan${total === 1 ? "y" : "ies"} had no confirmed job signal ` +
          `after checking ATS, their careers page, and an external search -- ` +
          `their scores are capped low regardless of fit (job-posting signal counts for most of the score). ` +
          `Not necessarily a sign these are poor-fit companies.`,
        );
      }
    }

    const qualify = r.qualify as { qualified: number; errors: number; error?: string } | undefined;
    const contacts = r.contacts as { found: number; contacts_saved: number; error?: string } | undefined;

    if (qualify && "error" in qualify && qualify.error) {
      problems.push(`Qualify step failed: ${qualify.error}`);
    } else if (qualify && qualify.qualified) {
      parts.push(`${qualify.qualified} qualified`);
      if (qualify.errors) problems.push(`${qualify.errors} qualification lookup(s) failed`);
    }

    if (contacts && "error" in contacts && contacts.error) {
      problems.push(`Contacts step failed: ${contacts.error}`);
    } else if (contacts && contacts.contacts_saved) {
      parts.push(`${contacts.contacts_saved} contact(s) found`);
    }

    const summary = parts.length ? parts.join(" · ") : "Run completed.";
    if (problems.length) {
      return { tone: "error", message: `${summary}\n${problems.join("\n")}` };
    }
    return { tone: "success", message: summary };
  }

  const watchJob = useCallback((jobId: string) => {
    const ws = researchApi.jobSocket(jobId, (snap) => {
      setActiveJob(snap);
      if (snap.status === "done" || snap.status === "error") {
        ws.close();
        if (snap.run_id && snap.kind === "discover") setScopeRun(snap.run_id);
        researchApi.listRuns().then(setRuns).catch(() => {});
        // discover/qualify/contacts all change data the cache is holding
        // stale copies of (new/rescored companies, qualification status,
        // new contacts) -- invalidate broadly rather than tracking exactly
        // which company changed.
        invalidate("companies");
        invalidate("company-detail");
        invalidate("contacts");
        invalidate("analytics");
        invalidate("research-runs");

        const label = JOB_KIND_LABEL[snap.kind] || snap.kind;
        if (snap.status === "error") {
          const message = snap.error || "Failed for an unknown reason.";
          setRunError(message);
          pushToast({ tone: "error", title: `${label} failed`, message });
        } else if (snap.kind === "discover") {
          const { tone, message } = describeDiscoverResult(snap.result);
          setRunError(null);
          pushToast({ tone, title: tone === "success" ? `${label} completed` : `${label} completed with issues`, message });
          if (tone === "success") setRunFormResetKey((k) => k + 1);
        } else {
          pushToast({ tone: "success", title: `${label} completed` });
        }

        setTimeout(() => {
          setActiveJob((current) => (current?.job_id === jobId ? null : current));
        }, 5000);
      }
    });
  }, [pushToast]);

  async function handleDiscover(req: DiscoverRequest) {
    setRunError(null);
    try {
      const { job_id } = await researchApi.startDiscover(req);
      setActiveJob({
        job_id,
        kind: "discover",
        run_id: null,
        status: "pending",
        done: 0,
        total: req.limit ?? 100,
        current_domain: null,
        result: null,
        error: null,
      });
      navigate({ name: "research-runs" });
      watchJob(job_id);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setRunError(message);
      pushToast({ tone: "error", title: "Couldn't start research run", message });
    }
  }

  function handleResumeRun(jobId: string, runId: string) {
    setActiveJob({
      job_id: jobId,
      kind: "discover",
      run_id: runId,
      status: "pending",
      done: 0,
      total: 1,
      current_domain: null,
      result: null,
      error: null,
    });
    navigate({ name: "research-runs" });
    watchJob(jobId);
  }

  async function handleSuppress(domain: string) {
    if (!actionRunId) return;
    setSelectedDomain(null);
    navigate({ name: "research-companies" });
    patchPrefix<ScoredCompany[]>("companies", (cur) => cur.filter((c) => c.domain !== domain));
    try {
      await researchApi.suppress(actionRunId, domain, "suppressed via UI");
    } catch {
      invalidate("companies");
    }
  }

  async function confirmPendingAction() {
    if (!pendingAction || !actionRunId) return;
    const tiers = pendingAction.domains ? ["A", "B", "C", "D"] : ["A", "B"];
    const kind = pendingAction.kind;
    setPendingAction(null);

    try {
      const resp = kind === "qualify" ? await researchApi.startQualify(actionRunId, tiers) : await researchApi.startContacts(actionRunId, tiers);
      if (!resp.job_id) {
        pushToast({ tone: "success", title: resp.message || "Nothing to do" });
        return;
      }
      setActiveJob({
        job_id: resp.job_id,
        kind,
        run_id: actionRunId,
        status: "pending",
        done: 0,
        total: resp.companies || 0,
        current_domain: null,
        result: null,
        error: null,
      });
      watchJob(resp.job_id);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      pushToast({ tone: "error", title: `Couldn't start ${JOB_KIND_LABEL[kind] || kind}`, message });
    }
  }

  const busy = activeJob?.status === "running" || activeJob?.status === "pending";
  const showResearchSelector =
    route.name.startsWith("research-") && route.name !== "research-runs" && route.name !== "research-company";
  const showCampaignSelector = route.name === "outreach-analytics";

  return (
    <div className={`shell${navCollapsed ? " shell--collapsed" : ""}`}>
      <Nav
        currentRoute={route}
        onNavigate={navigate}
        collapsed={navCollapsed}
        onToggle={() => setNavCollapsed((v) => !v)}
        apiStatus={apiStatus}
      />

      <main className="main">
        {showResearchSelector && <RunSelector runs={runs} value={scopeRun} onChange={setScopeRun} />}
        {showCampaignSelector && <CampaignSelector campaigns={campaigns} value={scopeCampaign} onChange={setScopeCampaign} />}

        {route.name === "home" && <Home onNavigate={navigate} />}

        {route.name === "research-company" && (
          runs.length > 0 ? (
            <CompanyDetail
              runId={runId}
              domain={route.domain}
              busy={busy}
              onBack={() => navigate({ name: "research-companies" })}
              onQualify={(d) => setPendingAction({ kind: "qualify", domains: [d] })}
              onContacts={(d) => setPendingAction({ kind: "contacts", domains: [d] })}
              onSuppress={handleSuppress}
            />
          ) : (
            <p className="hint">Loading…</p>
          )
        )}

        {route.name === "research-runs" && (
          <RunConfig
            regions={regions}
            onDiscover={handleDiscover}
            activeJob={activeJob}
            error={runError}
            resetKey={runFormResetKey}
          />
        )}

        {route.name === "research-companies" && (
          <Companies
            rows={rows}
            loading={rowsLoading}
            hasRun={runs.length > 0}
            filter={companyFilter}
            selectedDomain={selectedDomain}
            savedDomains={savedDomains}
            onFilterChange={setCompanyFilter}
            onSelect={setSelectedDomain}
            onToggleSave={toggleSave}
            onNewRun={() => navigate({ name: "research-runs" })}
          />
        )}

        {route.name === "research-analytics" && <Analytics runId={scopeRun} runs={runs} />}

        {route.name === "research-saved" && (
          <SavedLists
            saved={saved}
            loading={false}
            onUnsave={toggleSave}
            onOpen={(d) => navigate({ name: "research-company", domain: d })}
            onBrowse={() => navigate({ name: "research-companies" })}
          />
        )}

        {route.name === "research-contacts" && (
          <Contacts
            contacts={contacts}
            loading={contactsLoading}
            onOpenCompany={(d) => navigate({ name: "research-company", domain: d })}
          />
        )}

        {route.name === "research-settings" && (
          <Settings onRunDeleted={() => refreshSaved(scopeRun)} onRunResumed={handleResumeRun} />
        )}

        {route.name === "outreach-prospects" && <Prospects />}

        {route.name === "outreach-campaigns" && (
          <Campaigns onOpenCampaign={(id, campaignName) => navigate({ name: "outreach-campaign", campaignId: id, campaignName })} />
        )}

        {route.name === "outreach-campaign" && (
          <CampaignDetail
            campaignId={route.campaignId}
            campaignName={route.campaignName}
            onBack={() => navigate({ name: "outreach-campaigns" })}
          />
        )}

        {route.name === "outreach-analytics" && <OutreachAnalytics campaignId={scopeCampaign} campaigns={campaigns} />}
      </main>

      {selectedDomain && runs.length > 0 && route.name !== "research-company" && (
        <CompanyDrawer
          runId={runId}
          domain={selectedDomain}
          onClose={() => setSelectedDomain(null)}
          onOpenFull={() => {
            navigate({ name: "research-company", domain: selectedDomain });
            setSelectedDomain(null);
          }}
        />
      )}

      {pendingAction && (
        <ConfirmModal
          title={pendingAction.kind === "qualify" ? "Run LLM qualification?" : "Run contact discovery?"}
          message={
            pendingAction.kind === "qualify"
              ? "This spends LLM API tokens/quota (Gemini or Anthropic, whichever is configured)."
              : "This spends Hunter.io search quota (25/month on the free tier)."
          }
          onConfirm={confirmPendingAction}
          onCancel={() => setPendingAction(null)}
        />
      )}

      {activeJob && (activeJob.status === "running" || activeJob.status === "pending") && (
        <RunOverlay job={activeJob} />
      )}

      <ToastStack toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

export default App;
