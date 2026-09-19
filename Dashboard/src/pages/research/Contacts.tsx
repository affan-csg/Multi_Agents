import { useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import type { Contact } from "../../agents/research";
import { Avatar, EmptyState, Icon, StatCard, StatCardSkeleton, TableRowsSkeleton, relativeTime } from "../../components/ui";

const columnHelper = createColumnHelper<Contact>();

function fullName(c: Contact): string {
  return [c.first_name, c.last_name].filter(Boolean).join(" ") || c.email;
}

const columns = [
  columnHelper.accessor((c) => fullName(c), {
    id: "name",
    header: "Contact",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div className="cell-company">
          <Avatar name={fullName(row)} size="sm" />
          <div className="cell-company-name">{fullName(row)}</div>
        </div>
      );
    },
  }),
  columnHelper.accessor("email", {
    header: "Email",
    cell: (info) => {
      const email = info.getValue();
      return (
        <a
          href={`mailto:${email}`}
          className="contact-email-link"
          onClick={(e) => e.stopPropagation()}
        >
          <Icon name="mail" size={14} />
          {email}
        </a>
      );
    },
  }),
  columnHelper.accessor("position", {
    header: "Position",
    cell: (info) => <span className="cell-muted">{info.getValue() || "—"}</span>,
  }),
  columnHelper.accessor((c) => c.company_name || c.domain, {
    id: "company",
    header: "Company",
    cell: (info) => {
      const row = info.row.original;
      return (
        <div>
          <div>{row.company_name || row.domain}</div>
          <div className="cell-company-domain">{row.domain}</div>
        </div>
      );
    },
  }),
  columnHelper.accessor("industry", {
    header: "Industry",
    cell: (info) => <span className="cell-muted">{info.getValue() || "—"}</span>,
  }),
  columnHelper.accessor("seniority", {
    header: "Seniority",
    cell: (info) => <span className="cell-muted">{info.getValue() || "—"}</span>,
  }),
  columnHelper.accessor("confidence", {
    header: "Confidence",
    cell: (info) => {
      const v = info.getValue();
      return typeof v === "number" ? <span className="cell-muted">{v}%</span> : <span className="cell-muted">—</span>;
    },
  }),
  columnHelper.accessor("verification", {
    header: "Verified",
    cell: (info) => {
      const v = info.getValue();
      if (v === "valid") return <span className="tier-badge tier-A">valid</span>;
      if (!v) return <span className="cell-muted">—</span>;
      return <span className="cell-muted">{v}</span>;
    },
  }),
  columnHelper.accessor("found_at", {
    header: "Found",
    cell: (info) => <span className="cell-muted">{relativeTime(info.getValue())}</span>,
  }),
  columnHelper.display({
    id: "linkedin",
    header: "LinkedIn",
    cell: (info) => {
      const url = info.row.original.linkedin_url;
      if (!url) return <span className="cell-muted">—</span>;
      return (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="contact-linkedin-link"
          onClick={(e) => e.stopPropagation()}
        >
          <Icon name="linkedin" size={14} />
          Profile
        </a>
      );
    },
  }),
];

interface Props {
  contacts: Contact[];
  loading: boolean;
  onOpenCompany: (domain: string) => void;
}

export function Contacts({ contacts, loading, onOpenCompany }: Props) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "found_at", desc: true }]);
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    if (!search.trim()) return contacts;
    const needle = search.toLowerCase();
    return contacts.filter(
      (c) =>
        fullName(c).toLowerCase().includes(needle) ||
        c.email.toLowerCase().includes(needle) ||
        (c.company_name || c.domain).toLowerCase().includes(needle) ||
        (c.position || "").toLowerCase().includes(needle),
    );
  }, [contacts, search]);

  const table = useReactTable({
    data: filtered,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const stats = useMemo(() => {
    const companies = new Set(contacts.map((c) => c.domain)).size;
    const executives = contacts.filter((c) => c.seniority === "executive").length;
    const verified = contacts.filter((c) => c.verification === "valid").length;
    return { total: contacts.length, companies, executives, verified };
  }, [contacts]);

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Research Agent / <span>Contacts</span></p>
          <h1>Contacts</h1>
          <p>HR and talent contacts discovered across your research runs.</p>
        </div>
        <div className="page-head-actions">
          <div className="search-wrap">
            <Icon name="search" size={16} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search name, email, company…"
            />
            {search && (
              <button type="button" className="search-wrap-clear" aria-label="Clear search" onClick={() => setSearch("")}>
                <Icon name="close" size={13} />
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="stat-grid">
        {loading ? (
          <>
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
          </>
        ) : (
          <>
            <StatCard icon="contact" tone="accent" value={stats.total.toLocaleString()} label="Contacts found" />
            <StatCard icon="building" tone="mint" value={stats.companies.toLocaleString()} label="Companies covered" />
            <StatCard icon="crown" tone="violet" value={stats.executives.toLocaleString()} label="Executives" />
            <StatCard icon="check" tone="amber" value={stats.verified.toLocaleString()} label="Verified emails" />
          </>
        )}
      </div>

      <div className="table-card">
        {!loading && contacts.length === 0 ? (
          <EmptyState
            icon="contact"
            message="No contacts yet — open a company from Companies or Research runs and click “Find contacts.”"
          />
        ) : !loading && filtered.length === 0 ? (
          <EmptyState icon="search" message="No contacts match your search." />
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
                              <Icon name="chevronDown" size={12} />
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
                    <tr key={row.id} onClick={() => onOpenCompany(row.original.domain)}>
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
    </>
  );
}
