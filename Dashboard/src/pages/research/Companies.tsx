import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import type { CompanyFilter, ScoredCompany } from "../../agents/research";
import { AtsBadge, Avatar, EmptyState, Icon, ScorePill, StatCard, TableRowsSkeleton, relativeTime } from "../../components/ui";

const columnHelper = createColumnHelper<ScoredCompany>();

function metric(row: ScoredCompany, key: string): unknown {
  return (row.metrics as Record<string, unknown>)?.[key];
}

interface TableMeta {
  savedDomains: Set<string>;
  onToggleSave: (domain: string) => void;
}

const columns = [
  columnHelper.accessor((r) => r.name || r.domain, {
    id: "company",
    header: "Company",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div className="cell-company">
          <Avatar name={row.name || row.domain} size="sm" />
          <div>
            <div className="cell-company-name">{row.name || row.domain}</div>
            <div className="cell-company-domain">{row.domain}</div>
          </div>
        </div>
      );
    },
  }),
  columnHelper.accessor("industry", {
    header: "Industry",
    cell: (info) => <span className="cell-muted">{info.getValue() || "—"}</span>,
  }),
  columnHelper.accessor("ats_provider", {
    header: "ATS",
    cell: (info) => <AtsBadge provider={info.getValue()} />,
  }),
  columnHelper.accessor((r) => metric(r, "req_count") ?? 0, {
    id: "req_count",
    header: "Open roles",
    cell: (info) => {
      const v = info.getValue();
      return typeof v === "number" && v > 0 ? v : <span className="cell-muted">—</span>;
    },
  }),
  columnHelper.accessor("score", {
    header: "ICP score",
    cell: (info) => <ScorePill score={info.getValue()} />,
  }),
  columnHelper.accessor("tier", {
    header: "Tier",
    cell: (info) => <span className={`tier-badge tier-${info.getValue()}`}>{info.getValue()}</span>,
  }),
  columnHelper.accessor("scored_at", {
    header: "Scored",
    cell: (info) => <span className="cell-muted">{relativeTime(info.getValue())}</span>,
  }),
  columnHelper.display({
    id: "saved",
    header: "",
    cell: (info) => {
      const { savedDomains, onToggleSave } = info.table.options.meta as TableMeta;
      const domain = info.row.original.domain;
      const isSaved = savedDomains.has(domain);
      return (
        <button
          type="button"
          className={`save-btn${isSaved ? " save-btn--on" : ""}`}
          aria-label={isSaved ? "Remove from saved" : "Save company"}
          title={isSaved ? "Remove from saved" : "Save company"}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSave(domain);
          }}
        >
          <Icon name="bookmark" size={16} />
        </button>
      );
    },
  }),
];

interface Props {
  rows: ScoredCompany[];
  loading: boolean;
  hasRun: boolean;
  filter: CompanyFilter;
  selectedDomain: string | null;
  savedDomains: Set<string>;
  onFilterChange: (f: CompanyFilter) => void;
  onSelect: (domain: string) => void;
  onToggleSave: (domain: string) => void;
  onNewRun: () => void;
}

export function Companies({
  rows,
  loading,
  hasRun,
  filter,
  selectedDomain,
  savedDomains,
  onFilterChange,
  onSelect,
  onToggleSave,
  onNewRun,
}: Props) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "score", desc: true }]);
  const [showFilters, setShowFilters] = useState(false);

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    meta: { savedDomains, onToggleSave } satisfies TableMeta,
  });

  const stats = useMemo(() => {
    const hiring = rows.filter((r) => {
      const n = metric(r, "req_count");
      return typeof n === "number" && n > 0;
    }).length;
    return {
      screened: rows.length,
      hiring,
      tierA: rows.filter((r) => r.tier === "A").length,
      tierB: rows.filter((r) => r.tier === "B").length,
    };
  }, [rows]);

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Research Agent / <span>Companies</span></p>
          <h1>Companies</h1>
          <p>Explore and track companies in your research pipeline.</p>
        </div>
        <div className="page-head-actions">
          <div className="search-wrap">
            <Icon name="search" size={16} />
            <input
              value={filter.keyword || ""}
              onChange={(e) => onFilterChange({ ...filter, keyword: e.target.value || undefined })}
              placeholder="Search job titles or descriptions…"
            />
            {filter.keyword && (
              <button
                type="button"
                className="search-wrap-clear"
                aria-label="Clear search"
                onClick={() => onFilterChange({ ...filter, keyword: undefined })}
              >
                <Icon name="close" size={13} />
              </button>
            )}
          </div>
          <button type="button" onClick={() => setShowFilters((v) => !v)}>
            <Icon name="filter" size={15} />
            Filters
          </button>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard icon="building" tone="accent" value={stats.screened.toLocaleString()} label="Screened" />
        <StatCard icon="users" tone="mint" value={stats.hiring.toLocaleString()} label="Hiring now" />
        <StatCard icon="star" tone="violet" value={stats.tierA.toLocaleString()} label="Tier A" />
        <StatCard icon="tag" tone="amber" value={stats.tierB.toLocaleString()} label="Tier B" />
      </div>

      {showFilters && (
        <div className="card" style={{ marginBottom: 20 }}>
          <div className="field-row">
            <div className="field-group">
              <label>Tier</label>
              <select
                value={filter.tier || ""}
                onChange={(e) => onFilterChange({ ...filter, tier: e.target.value || undefined })}
              >
                <option value="">All tiers</option>
                {["A", "B", "C", "D"].map((t) => (
                  <option key={t} value={t}>
                    Tier {t}
                  </option>
                ))}
              </select>
            </div>
            <div className="field-group">
              <label>Min score</label>
              <input
                type="number"
                value={filter.min_score ?? ""}
                onChange={(e) =>
                  onFilterChange({ ...filter, min_score: e.target.value ? Number(e.target.value) : undefined })
                }
                placeholder="0"
              />
            </div>
            <div className="field-group">
              <label>State</label>
              <input
                value={filter.state || ""}
                onChange={(e) => onFilterChange({ ...filter, state: e.target.value || undefined })}
                placeholder="e.g. CA"
              />
            </div>
            <div className="field-group">
              <label>Industry contains</label>
              <input
                value={filter.industry || ""}
                onChange={(e) => onFilterChange({ ...filter, industry: e.target.value || undefined })}
                placeholder="e.g. Healthcare"
              />
            </div>
          </div>
        </div>
      )}

      <div className="table-card">
        {!hasRun ? (
          <EmptyState icon="play" message="No completed run yet — start a research run to see companies." />
        ) : !loading && rows.length === 0 ? (
          <EmptyState icon="search" message="No companies match these filters." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => {
                      const sorted = header.column.getIsSorted();
                      return (
                        <th key={header.id} onClick={header.column.getToggleSortingHandler()}>
                          <span className="th-inner">
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            <span className={`sort-caret${sorted ? " sort-caret--active" : ""}`}>
                              <Icon name={sorted === "asc" ? "chevronDown" : "chevronDown"} size={12} />
                            </span>
                          </span>
                        </th>
                      );
                    })}
                  </tr>
                ))}
              </thead>
              <tbody>
                {loading ? (
                  <TableRowsSkeleton columns={columns.length} />
                ) : (
                  table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => onSelect(row.original.domain)}
                      className={row.original.domain === selectedDomain ? "row--selected" : ""}
                    >
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

      {!hasRun && (
        <div style={{ marginTop: 18 }}>
          <button type="button" className="primary" onClick={onNewRun}>
            <Icon name="plus" size={15} />
            New research run
          </button>
        </div>
      )}
    </>
  );
}
