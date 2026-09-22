import { useEffect, useMemo, useState } from "react";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { researchApi, type RunSummary } from "../../agents/research";
import { outreachApi, type ExcludedProspect, type ImportedProspect, type JobSnapshot } from "../../agents/outreach";
import { Avatar, EmptyState, Icon, ScorePill, StatCard, TableRowsSkeleton } from "../../components/ui";
import { RunOverlay } from "../../components/RunOverlay";
import { ConfirmModal } from "../../components/ConfirmModal";
import { invalidate } from "../../hooks/useCachedFetch";

const columnHelper = createColumnHelper<ImportedProspect>();

function makeColumns(runLabels: Map<string, string>) {
  return [
  columnHelper.accessor((r) => r.name || r.research_domain, {
    id: "company",
    header: "Company",
    cell: (info) => {
      const row = info.row.original;
      const runCount = row.research_run_ids?.length ?? 0;
      return (
        <div className="cell-company">
          <Avatar name={row.name || row.research_domain} size="sm" />
          <div>
            <div className="cell-company-name">
              {row.name || row.research_domain}
              {runCount > 1 && (
                <span
                  className="run-count-badge"
                  title={`Imported from ${runCount} research runs: ${row.research_run_ids
                    .map((id) => runLabels.get(id) || id)
                    .join(", ")}`}
                >
                  in {runCount} runs
                </span>
              )}
            </div>
            <div className="cell-company-domain">{row.research_domain}</div>
          </div>
        </div>
      );
    },
  }),
  columnHelper.accessor("tier", {
    header: "Tier",
    cell: (info) => (info.getValue() ? <span className={`tier-badge tier-${info.getValue()}`}>{info.getValue()}</span> : <span className="cell-muted">—</span>),
  }),
  columnHelper.accessor("score", {
    header: "Score",
    cell: (info) => {
      const v = info.getValue();
      return v != null ? <ScorePill score={v} /> : <span className="cell-muted">—</span>;
    },
  }),
  columnHelper.accessor("industry", {
    header: "Industry",
    cell: (info) => <span className="cell-muted">{info.getValue() || "—"}</span>,
  }),
  columnHelper.accessor("contact_name", {
    header: "Contact",
    cell: (info) => {
      const row = info.row.original;
      return row.contact_email ? (
        <div>
          <div>{row.contact_name || row.contact_email}</div>
          <div className="cell-company-domain">{row.contact_email}</div>
        </div>
      ) : (
        <span className="cell-muted">no contact</span>
      );
    },
  }),
  columnHelper.display({
    id: "pain",
    header: "Qualified",
    cell: (info) => {
      const row = info.row.original;
      if (row.pain_summary) {
        return (
          <span className="tier-badge tier-A">
            <Icon name="sparkle" size={12} /> yes
          </span>
        );
      }
      if (row.qualification_failed) {
        return (
          <span
            className="tier-badge tier-D"
            title="AI qualification failed after retries in the Research Agent -- not silently marked complete. Retry it from the company's profile in Research, then re-import here."
          >
            <Icon name="info" size={12} /> failed
          </span>
        );
      }
      return <span className="cell-muted">—</span>;
    },
  }),
  ];
}

const TIER_OPTIONS = ["A", "B", "C", "D"] as const;
const CONTACT_OPTIONS = [
  { value: "with-contact", label: "With contact" },
  { value: "qualified", label: "LLM-qualified" },
  { value: "qualification-failed", label: "Qualification failed" },
] as const;
type ContactFilter = (typeof CONTACT_OPTIONS)[number]["value"] | "all";

export function Prospects() {
  const { data: runsData } = useCachedFetch<RunSummary[]>("research-runs", () => researchApi.listRuns());
  const runs = runsData ?? [];
  const runLabels = useMemo(() => new Map(runs.map((r) => [r.run_id, r.label || r.run_id])), [runs]);
  const columns = useMemo(() => makeColumns(runLabels), [runLabels]);
  const [selectedRun, setSelectedRun] = useState<string>("");
  const [tiers, setTiers] = useState("A,B");
  const [sorting, setSorting] = useState<SortingState>([{ id: "score", desc: true }]);
  const [activeJob, setActiveJob] = useState<JobSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<{ tone: "success" | "warning"; message: string } | null>(null);

  const [tierFilter, setTierFilter] = useState<Set<string>>(new Set());
  const [contactFilter, setContactFilter] = useState<ContactFilter>("all");
  const [search, setSearch] = useState("");
  // "" = view prospects from every research run pooled together (the
  // previous, only, behavior); any other value scopes the table to just
  // that run -- independent from `selectedRun` above, which only drives
  // the Import card and would otherwise force filtering to whatever run
  // someone last picked to import from.
  const [viewRun, setViewRun] = useState<string>("");
  const [confirmDeleteRun, setConfirmDeleteRun] = useState(false);
  const [deletingRun, setDeletingRun] = useState(false);
  const [deleteRunError, setDeleteRunError] = useState<string | null>(null);
  const [deleteRunResult, setDeleteRunResult] = useState<string | null>(null);

  const { data: prospectsData, loading, refresh: refreshProspects } = useCachedFetch<ImportedProspect[]>(
    viewRun ? `prospects:${viewRun}` : "prospects:all",
    () => outreachApi.listProspects(viewRun || undefined),
  );
  const allProspects = prospectsData ?? [];

  const prospects = useMemo(() => {
    let rows = allProspects;
    if (tierFilter.size > 0) rows = rows.filter((p) => p.tier && tierFilter.has(p.tier));
    if (contactFilter === "with-contact") rows = rows.filter((p) => p.contact_email);
    if (contactFilter === "qualified") rows = rows.filter((p) => p.pain_summary);
    if (contactFilter === "qualification-failed") rows = rows.filter((p) => p.qualification_failed);
    if (search.trim()) {
      const needle = search.toLowerCase();
      rows = rows.filter(
        (p) =>
          (p.name || p.research_domain).toLowerCase().includes(needle) ||
          p.research_domain.toLowerCase().includes(needle) ||
          (p.industry || "").toLowerCase().includes(needle),
      );
    }
    return rows;
  }, [allProspects, tierFilter, contactFilter, search]);

  function toggleTier(tier: string) {
    setTierFilter((prev) => {
      const next = new Set(prev);
      if (next.has(tier)) next.delete(tier);
      else next.add(tier);
      return next;
    });
  }

  const hasActiveFilters = tierFilter.size > 0 || contactFilter !== "all" || search.trim() !== "" || viewRun !== "";

  function clearFilters() {
    setTierFilter(new Set());
    setContactFilter("all");
    setSearch("");
    setViewRun("");
  }

  useEffect(() => {
    if (runs.length > 0 && !selectedRun) setSelectedRun(runs[0].run_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs]);

  const table = useReactTable({
    data: prospects,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const stats = useMemo(() => ({
    total: allProspects.length,
    withContact: allProspects.filter((p) => p.contact_email).length,
    qualified: allProspects.filter((p) => p.pain_summary).length,
    qualificationFailed: allProspects.filter((p) => p.qualification_failed).length,
    tierA: allProspects.filter((p) => p.tier === "A").length,
  }), [allProspects]);

  async function handleImport() {
    if (!selectedRun) return;
    setError(null);
    setImportResult(null);
    const selectedTiers = tiers.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean);
    const runLabel = runs.find((r) => r.run_id === selectedRun)?.label || selectedRun;
    try {
      const { job_id } = await outreachApi.startImport({ run_id: selectedRun, tiers: selectedTiers });
      setActiveJob({
        job_id, kind: "import", campaign_id: null, status: "pending",
        done: 0, total: 0, current_domain: null, result: null, error: null,
      });
      const ws = outreachApi.jobSocket(job_id, (snap) => {
        setActiveJob(snap);
        if (snap.status === "done" || snap.status === "error") {
          ws.close();
          refreshProspects();
          if (snap.status === "error") {
            setError(snap.error || "Import failed for an unknown reason.");
          } else {
            const result = snap.result as { imported?: number; excluded_no_contact?: ExcludedProspect[] } | null;
            const imported = result?.imported ?? 0;
            const excluded = result?.excluded_no_contact ?? [];
            const excludedNote = excluded.length
              ? " " + excluded.map((e) => `No contacts found for ${e.name}, so it was not added to prospects.`).join(" ")
              : "";
            if (imported === 0) {
              setImportResult({
                tone: "warning",
                message: `No prospects imported from "${runLabel}" for tier(s) ${selectedTiers.join(", ") || "(none)"}. `
                  + "Either no companies in this run scored in those tiers, or none of the matching companies have a contact email yet "
                  + "(contact discovery is a separate step from scoring)."
                  + excludedNote,
              });
            } else {
              setImportResult({
                tone: excluded.length ? "warning" : "success",
                message: `Imported ${imported} prospect${imported === 1 ? "" : "s"} from "${runLabel}".` + excludedNote,
              });
            }
          }
          setTimeout(() => setActiveJob((cur) => (cur?.job_id === job_id ? null : cur)), 4000);
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDeleteRunProspects() {
    if (!viewRun) return;
    setConfirmDeleteRun(false);
    setDeletingRun(true);
    setDeleteRunError(null);
    setDeleteRunResult(null);
    const runLabel = runs.find((r) => r.run_id === viewRun)?.label || viewRun;
    try {
      const result = await outreachApi.deleteProspectsByRun(viewRun);
      invalidate("prospects");
      invalidate("campaigns");
      invalidate("campaign-items");
      invalidate("outreach-analytics");
      setDeleteRunResult(
        `Removed ${result.prospects_removed} prospect${result.prospects_removed === 1 ? "" : "s"}`
          + (result.campaigns_removed > 0
            ? ` and ${result.campaigns_removed} campaign${result.campaigns_removed === 1 ? "" : "s"} built from "${runLabel}".`
            : ` from "${runLabel}".`),
      );
      setViewRun("");
    } catch (e) {
      setDeleteRunError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingRun(false);
    }
  }

  const busy = activeJob?.status === "running" || activeJob?.status === "pending";

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Outreach Agent / <span>Prospects</span></p>
          <h1>Prospects</h1>
          <p>Qualified companies imported from the Research Agent, cached locally for outreach.</p>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <h3 className="card-title">Import from a research run</h3>
        <p className="card-sub">
          Pulls scored companies from the Research Agent's API — never touches its database directly.
        </p>
        <div className="field-row">
          <div className="field-group">
            <label>Research run</label>
            <select
              value={selectedRun}
              onChange={(e) => {
                setSelectedRun(e.target.value);
                setImportResult(null);
                setError(null);
              }}
            >
              {runs.length === 0 && <option value="">No runs found</option>}
              {runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.label || r.run_id}
                </option>
              ))}
            </select>
          </div>
          <div className="field-group">
            <label>Tiers to import</label>
            <input value={tiers} onChange={(e) => setTiers(e.target.value)} placeholder="A,B" />
          </div>
        </div>
        {error && <p className="error-text">{error}</p>}
        {!error && importResult && (
          <p className={importResult.tone === "success" ? "success-text" : "warning-text"}>{importResult.message}</p>
        )}
        <button type="button" className="primary" onClick={handleImport} disabled={busy || !selectedRun}>
          <Icon name="refresh" size={15} />
          {busy ? "Importing…" : "Import qualified prospects"}
        </button>
      </div>

      <div className="stat-grid stat-grid--compact">
        <StatCard compact icon="target" tone="accent" value={stats.total.toLocaleString()} label="Cached prospects" />
        <StatCard compact icon="mail" tone="mint" value={stats.withContact.toLocaleString()} label="With contact email" />
        <StatCard compact icon="sparkle" tone="violet" value={stats.qualified.toLocaleString()} label="LLM-qualified" />
        <StatCard compact icon="star" tone="amber" value={stats.tierA.toLocaleString()} label="Tier A" />
        {stats.qualificationFailed > 0 && (
          <StatCard
            compact
            icon="info"
            tone="amber"
            value={stats.qualificationFailed.toLocaleString()}
            label="Qualification failed"
          />
        )}
      </div>

      <div className="filter-bar">
        <div className="filter-bar-row">
          <Icon name="filter" size={14} />
          <select
            value={viewRun}
            onChange={(e) => setViewRun(e.target.value)}
            style={{ maxWidth: 220 }}
            aria-label="Filter prospects by research run"
          >
            <option value="">All research runs</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.label || r.run_id}
              </option>
            ))}
          </select>
          {viewRun && (
            <button
              type="button"
              className="danger"
              onClick={() => setConfirmDeleteRun(true)}
              disabled={deletingRun}
              title="Delete every prospect (and campaign built from them) imported from this research run"
            >
              <Icon name="trash" size={13} />
              {deletingRun ? "Removing…" : "Delete this run's prospects"}
            </button>
          )}
          <span className="filter-bar-divider" />
          <div className="chip-row">
            {TIER_OPTIONS.map((t) => (
              <button
                key={t}
                type="button"
                className={`chip${tierFilter.has(t) ? " chip-active" : ""}`}
                onClick={() => toggleTier(t)}
              >
                Tier {t}
              </button>
            ))}
            <span className="filter-bar-divider" />
            {CONTACT_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                className={`chip${contactFilter === o.value ? " chip-active" : ""}`}
                onClick={() => setContactFilter((cur) => (cur === o.value ? "all" : o.value))}
              >
                {o.label}
              </button>
            ))}
          </div>
          {hasActiveFilters && (
            <button type="button" className="filter-bar-clear" onClick={clearFilters}>
              <Icon name="close" size={12} />
              Clear filters
            </button>
          )}
        </div>
        <div className="search-wrap">
          <Icon name="search" size={16} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search company, domain, industry…"
          />
          {search && (
            <button type="button" className="search-wrap-clear" aria-label="Clear search" onClick={() => setSearch("")}>
              <Icon name="close" size={13} />
            </button>
          )}
        </div>
      </div>

      {deleteRunError && <p className="error-text">{deleteRunError}</p>}
      {deleteRunResult && <p className="success-text">{deleteRunResult}</p>}

      <div className="table-card">
        {!loading && allProspects.length === 0 ? (
          <EmptyState icon="target" message="No prospects imported yet — import from a research run above." />
        ) : !loading && prospects.length === 0 ? (
          <EmptyState icon="search" message="No prospects match your filters." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => (
                      <th key={header.id} onClick={header.column.getToggleSortingHandler()}>
                        <span className="th-inner">
                          {flexRender(header.column.columnDef.header, header.getContext())}
                        </span>
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {loading ? (
                  <TableRowsSkeleton columns={columns.length} />
                ) : (
                  table.getRowModel().rows.map((row) => (
                    <tr key={row.id}>
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                      ))}
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {activeJob && busy && <RunOverlay job={activeJob} />}

      {confirmDeleteRun && viewRun && (
        <ConfirmModal
          title="Delete this run's prospects?"
          message={
            `Removes every prospect imported from "${runs.find((r) => r.run_id === viewRun)?.label || viewRun}" `
            + "from this cache, along with any campaign (and its items) built from them. The research run itself "
            + "and its data in the Research Agent are not affected. This cannot be undone."
          }
          danger
          confirmLabel="Delete prospects"
          onConfirm={handleDeleteRunProspects}
          onCancel={() => setConfirmDeleteRun(false)}
        />
      )}
    </>
  );
}
