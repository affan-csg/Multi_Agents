import { useMemo, useState } from "react";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import { outreachApi, type BookingEntry } from "../../agents/outreach";
import { Avatar, EmptyState, Icon, StatCard, TableRowsSkeleton } from "../../components/ui";

// Who moved the card, per outreach_events.actor -- 'system' never appears
// here (the Outreach Agent never sets 'booked' on its own).
const BOOKED_BY_LABEL: Record<string, string> = {
  conversation_agent: "Conversation Agent",
  user: "Manually",
};

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
  });
}

interface Props {
  onOpenCampaign: (campaignId: string, campaignName?: string | null) => void;
}

export function Bookings({ onOpenCampaign }: Props) {
  const { data, loading } = useCachedFetch<BookingEntry[]>("bookings", () => outreachApi.listBookings());
  const bookings = data ?? [];
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    if (!search.trim()) return bookings;
    const needle = search.toLowerCase();
    return bookings.filter(
      (b) =>
        (b.company_name || b.research_domain).toLowerCase().includes(needle) ||
        b.research_domain.toLowerCase().includes(needle) ||
        (b.contact_name || "").toLowerCase().includes(needle) ||
        (b.contact_email || "").toLowerCase().includes(needle) ||
        (b.campaign_name || "").toLowerCase().includes(needle),
    );
  }, [bookings, search]);

  const stats = useMemo(() => {
    const now = Date.now();
    const last7Days = bookings.filter((b) => b.booked_at && now - new Date(b.booked_at).getTime() < 7 * 86400_000).length;
    const viaAgent = bookings.filter((b) => b.booked_by === "conversation_agent").length;
    return { total: bookings.length, last7Days, viaAgent };
  }, [bookings]);

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Outreach Agent / <span>Bookings</span></p>
          <h1>Bookings</h1>
          <p>Every prospect who has booked a call — via the Conversation Agent or moved by hand, across every campaign.</p>
        </div>
      </div>

      <div className="stat-grid stat-grid--compact">
        <StatCard compact icon="calendar" tone="violet" value={stats.total.toLocaleString()} label="Total bookings" />
        <StatCard compact icon="clock" tone="mint" value={stats.last7Days.toLocaleString()} label="Booked in the last 7 days" />
        <StatCard compact icon="mail" tone="accent" value={stats.viaAgent.toLocaleString()} label="Booked via Conversation Agent" />
      </div>

      <div className="filter-bar">
        <div className="search-wrap">
          <Icon name="search" size={16} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search company, contact, campaign…"
          />
          {search && (
            <button type="button" className="search-wrap-clear" aria-label="Clear search" onClick={() => setSearch("")}>
              <Icon name="close" size={13} />
            </button>
          )}
        </div>
      </div>

      <div className="table-card">
        {!loading && bookings.length === 0 ? (
          <EmptyState
            icon="calendar"
            message="No bookings yet — they'll show up here once a prospect agrees to a call, whether the Conversation Agent moves the card or you do it by hand."
          />
        ) : !loading && filtered.length === 0 ? (
          <EmptyState icon="search" message="No bookings match your search." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Contact</th>
                  <th>Campaign</th>
                  <th>Booked</th>
                  <th>By</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <TableRowsSkeleton columns={5} />
                ) : (
                  filtered.map((b) => (
                    <tr
                      key={b.item_id}
                      onClick={() => {
                        if (b.calendly_url) {
                          window.open(b.calendly_url, "_blank", "noopener,noreferrer");
                        } else {
                          onOpenCampaign(b.campaign_id, b.campaign_name);
                        }
                      }}
                      style={{ cursor: "pointer" }}
                      title={b.calendly_url ? "Open in Calendly" : `Open ${b.campaign_name || "campaign"}`}
                    >
                      <td>
                        <div className="cell-company">
                          <Avatar name={b.company_name || b.research_domain} size="sm" />
                          <div>
                            <div className="cell-company-name">
                              {b.company_name || b.research_domain}
                              {b.calendly_url && <Icon name="calendar" size={13} />}
                            </div>
                            <div className="cell-company-domain">{b.research_domain}</div>
                          </div>
                        </div>
                      </td>
                      <td>
                        {b.contact_email ? (
                          <div>
                            <div>{b.contact_name || b.contact_email}</div>
                            <div className="cell-company-domain">{b.contact_email}</div>
                          </div>
                        ) : (
                          <span className="cell-muted">—</span>
                        )}
                      </td>
                      <td>
                        <a
                          href={`#campaign-${b.campaign_id}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            onOpenCampaign(b.campaign_id, b.campaign_name);
                          }}
                        >
                          {b.campaign_name || <span className="cell-muted">—</span>}
                        </a>
                      </td>
                      <td>{formatWhen(b.booked_at)}</td>
                      <td>
                        <span className="cell-muted">
                          {b.booked_by ? BOOKED_BY_LABEL[b.booked_by] || b.booked_by : "—"}
                        </span>
                      </td>
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
