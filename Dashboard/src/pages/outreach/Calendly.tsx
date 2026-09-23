import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useCachedFetch, invalidate } from "../../hooks/useCachedFetch";
import { outreachApi, type BookingEntry, type BookingsQuery } from "../../agents/outreach";
import { calendlyApi, type EventType } from "../../agents/calendly";
import { Avatar, EmptyState, Icon, StatCard, TableRowsSkeleton } from "../../components/ui";
import { ConfirmModal } from "../../components/ConfirmModal";

type CalendlyTab = "bookings" | "event-types";

const TABS: { id: CalendlyTab; label: string; icon: "calendar" | "list" }[] = [
  { id: "bookings", label: "Bookings", icon: "calendar" },
  { id: "event-types", label: "Event Types", icon: "list" },
];

type TimeFilter = "all" | "upcoming" | "past";
type SortField = "booked_at" | "meeting_starts_at";
type SortOrder = "asc" | "desc";

const TIME_FILTERS: { id: TimeFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "upcoming", label: "Upcoming" },
  { id: "past", label: "Past" },
];

// Whether this booking's actual meeting time is still ahead of now --
// Calendly itself refuses to cancel a past event ("Permission Denied: Event
// in the past") and only considers an invitee a no-show once a meeting has
// happened, so both the cancel and no-show actions are gated on this.
// Requires a real meeting_starts_at (not the booked_at fallback the Booked
// column displays) -- a booking with no known Calendly start time has
// nothing to safely assume is future or past.
function isFuture(b: BookingEntry): boolean {
  return !!b.meeting_starts_at && new Date(b.meeting_starts_at).getTime() > Date.now();
}

function isPast(b: BookingEntry): boolean {
  return !!b.meeting_starts_at && new Date(b.meeting_starts_at).getTime() <= Date.now();
}

// Who moved the card, per outreach_events.actor -- 'system' never appears
// here (the Outreach Agent never sets 'booked' on its own). 'calendly' is
// the real API sync/webhook integration confirming or reverting a booking
// independently of any reply (see calendly/sync.py's ACTOR).
const BOOKED_BY_LABEL: Record<string, string> = {
  conversation_agent: "Conversation Agent",
  user: "Manually",
  calendly: "Calendly",
};

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
  });
}

// Time-filter tabs (Upcoming/Past/All) translate to date_from/date_to on the
// server query -- the same boundary the old client-side whenOf() comparison
// used, now expressed as an ISO instant the backend filters meeting_starts_at
// (falling back to booked_at) against.
function timeFilterToDateRange(filter: TimeFilter): { dateFrom?: string; dateTo?: string } {
  const now = new Date().toISOString();
  if (filter === "upcoming") return { dateFrom: now };
  if (filter === "past") return { dateTo: now };
  return {};
}

const WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function formatIntervals(rule: Record<string, unknown>): string {
  const intervals = rule.intervals as { from?: string; to?: string }[] | undefined;
  if (!intervals || intervals.length === 0) return "Unavailable";
  return intervals.map((i) => `${i.from || "?"}–${i.to || "?"}`).join(", ");
}

function ruleLabel(rule: Record<string, unknown>): string {
  if (typeof rule.wday === "string") return rule.wday;
  if (typeof rule.date === "string") return rule.date;
  return "Rule";
}

interface Props {
  activeTab: CalendlyTab;
  onTabChange: (tab: CalendlyTab) => void;
  onOpenCampaign: (campaignId: string, campaignName?: string | null) => void;
}

export function Calendly({ activeTab, onTabChange, onOpenCampaign }: Props) {
  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Outreach Agent / <span>Calendly</span></p>
          <h1>Calendly</h1>
          <p>Bookings synced from Calendly, and your account's meeting types &amp; availability.</p>
        </div>
        {activeTab === "bookings" && <BookingsSyncButton />}
      </div>

      <div className="settings-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`settings-tab${activeTab === t.id ? " settings-tab--active" : ""}`}
            onClick={() => onTabChange(t.id)}
          >
            <Icon name={t.icon} size={14} />
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === "bookings" ? (
        <BookingsTab onOpenCampaign={onOpenCampaign} />
      ) : (
        <EventTypesTab />
      )}
    </>
  );
}

// Lives in the page head (next to the title, above the tab bar) rather than
// inside BookingsTab itself, matching where it sat before Bookings/Event
// Types were merged into one page -- switching to the Event Types tab hides
// it entirely (see Calendly()'s activeTab check) since it's a bookings-only
// action.
function BookingsSyncButton() {
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

  const runSync = async () => {
    setSyncing(true);
    setSyncMessage(null);
    try {
      const status = await calendlyApi.status();
      if (!status.api_configured) {
        setSyncMessage("Calendly API token not configured — set CALENDLY_API_TOKEN in the Outreach Agent's .env.");
        return;
      }
      const result = await calendlyApi.sync();
      setSyncMessage(
        `Synced: ${result.fetched} events checked, ${result.matched} matched, ${result.unmatched} unmatched` +
          (result.errors.length ? ` (${result.errors.length} error${result.errors.length === 1 ? "" : "s"})` : ""),
      );
      invalidate("bookings");
    } catch (err) {
      setSyncMessage(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
      <div className="page-head-actions">
        <button
          type="button"
          className="ghost"
          onClick={runSync}
          disabled={syncing}
          title="Pull the latest bookings and cancellations from Calendly's API"
        >
          <Icon name="refresh" size={15} />
          {syncing ? "Syncing…" : "Sync Calendly"}
        </button>
      </div>
      {syncMessage && <p className="cell-muted" style={{ margin: 0, textAlign: "right" }}>{syncMessage}</p>}
    </div>
  );
}

function BookingsTab({ onOpenCampaign }: { onOpenCampaign: Props["onOpenCampaign"] }) {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [timeFilter, setTimeFilter] = useState<TimeFilter>("all");
  const [showCanceled, setShowCanceled] = useState(false);
  const [sortField, setSortField] = useState<SortField>("booked_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [actingUri, setActingUri] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState<BookingEntry | null>(null);
  const [confirmNoShow, setConfirmNoShow] = useState<BookingEntry | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<BookingEntry | null>(null);
  const [menuOpenUri, setMenuOpenUri] = useState<string | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(null);

  // Search is debounced before it flows into the cache key / server call --
  // unlike the rest of this app's client-side-only search filters, this one
  // now triggers a network request per keystroke without it.
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
  }, [search]);

  // Every filter/sort dimension folds into one query object, baked into the
  // cache key -- same trick Prospects.tsx uses for `prospects:${viewRun}` to
  // let useCachedFetch treat each distinct filter combination as its own
  // independently-cached entry.
  const query: BookingsQuery = useMemo(() => {
    const { dateFrom, dateTo } = timeFilterToDateRange(timeFilter);
    return {
      status: showCanceled ? "canceled" : "active",
      dateFrom,
      dateTo,
      search: debouncedSearch || undefined,
      sort: sortField,
      order: sortOrder,
    };
  }, [timeFilter, showCanceled, debouncedSearch, sortField, sortOrder]);

  const cacheKey = `bookings:${JSON.stringify(query)}`;
  const { data, loading, refresh } = useCachedFetch(cacheKey, () => outreachApi.listBookingsFiltered(query));
  const bookings = data ?? [];
  const total = bookings.length;
  const [actionMessage, setActionMessage] = useState<string | null>(null);

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder((o) => (o === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortOrder("desc");
    }
  };

  const runAction = async (b: BookingEntry, action: () => Promise<unknown>) => {
    if (!b.event_uri) return;
    setActingUri(b.event_uri);
    try {
      await action();
      // Server-computed filters/sort mean there's no reliable local patch to
      // apply (a canceled row might need to leave this exact result set) --
      // refetch this query, and invalidate the "bookings" key the reminder
      // banner reads separately so it doesn't keep showing a meeting that
      // was just canceled.
      refresh();
      invalidate("bookings");
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : "Action failed");
    } finally {
      setActingUri(null);
    }
  };

  const cancelBooking = (b: BookingEntry) => runAction(b, () => calendlyApi.cancel(b.event_uri!));
  const markNoShow = (b: BookingEntry) => runAction(b, () => calendlyApi.markNoShow(b.event_uri!));
  const unmarkNoShow = (b: BookingEntry) => runAction(b, () => calendlyApi.unmarkNoShow(b.event_uri!));
  const deleteBooking = (b: BookingEntry) => runAction(b, () => calendlyApi.deleteBooking(b.event_uri!));

  const stats = useMemo(() => {
    const now = Date.now();
    const last7Days = bookings.filter((b) => b.booked_at && now - new Date(b.booked_at).getTime() < 7 * 86400_000).length;
    const viaAgent = bookings.filter((b) => b.booked_by === "conversation_agent").length;
    return { total, last7Days, viaAgent };
  }, [bookings, total]);

  const openMenu = (b: BookingEntry, e: MouseEvent<HTMLButtonElement>) => {
    if (menuOpenUri === b.event_uri) {
      setMenuOpenUri(null);
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    setMenuPos({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
    setMenuOpenUri(b.event_uri);
  };

  return (
    <>
      {actionMessage && <p className="cell-muted" style={{ margin: "0 0 16px" }}>{actionMessage}</p>}

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
        <div className="tabs" role="tablist">
          {TIME_FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={timeFilter === f.id}
              className={`tab${timeFilter === f.id ? " tab--active" : ""}`}
              onClick={() => setTimeFilter(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="chip-row">
          <button
            type="button"
            className={`chip${showCanceled ? " chip-active" : ""}`}
            onClick={() => setShowCanceled((v) => !v)}
          >
            Show canceled
          </button>
        </div>
      </div>

      <div className="table-card">
        {!loading && total === 0 ? (
          debouncedSearch || showCanceled || timeFilter !== "all" ? (
            <EmptyState icon="search" message="No bookings match your filters." />
          ) : (
            <EmptyState
              icon="calendar"
              message="No bookings yet — they'll show up here once a prospect agrees to a call, whether the Conversation Agent moves the card or you do it by hand."
            />
          )
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Contact</th>
                  <th>Campaign</th>
                  <th onClick={() => toggleSort("booked_at")} style={{ cursor: "pointer" }}>
                    Booked
                    <span className={`sort-caret${sortField === "booked_at" ? " sort-caret--active" : ""}`}>
                      {sortField === "booked_at" && sortOrder === "asc" ? "▲" : "▼"}
                    </span>
                  </th>
                  <th onClick={() => toggleSort("meeting_starts_at")} style={{ cursor: "pointer" }}>
                    Meeting time
                    <span className={`sort-caret${sortField === "meeting_starts_at" ? " sort-caret--active" : ""}`}>
                      {sortField === "meeting_starts_at" && sortOrder === "asc" ? "▲" : "▼"}
                    </span>
                  </th>
                  <th>By</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <TableRowsSkeleton columns={7} />
                ) : (
                  bookings.map((b, i) => (
                    <tr
                      key={b.item_id ?? `unmatched-${b.event_uri ?? i}`}
                      onClick={() => {
                        if (b.calendly_url) {
                          window.open(b.calendly_url, "_blank", "noopener,noreferrer");
                        } else if (b.campaign_id) {
                          onOpenCampaign(b.campaign_id, b.campaign_name);
                        }
                      }}
                      style={{ cursor: b.calendly_url || b.campaign_id ? "pointer" : "default" }}
                      title={
                        b.calendly_url
                          ? "Open in Calendly"
                          : b.campaign_id
                            ? `Open ${b.campaign_name || "campaign"}`
                            : "Not matched to a campaign item"
                      }
                    >
                      <td>
                        {b.matched ? (
                          <div className="cell-company">
                            <Avatar name={b.company_name || b.research_domain || "?"} size="sm" />
                            <div>
                              <div className="cell-company-name">
                                {b.company_name || b.research_domain}
                                {b.calendly_url && <Icon name="calendar" size={13} />}
                                {b.meeting_status === "canceled" && <span className="cell-muted"> · canceled</span>}
                                {b.no_show && <span className="cell-muted"> · no-show</span>}
                              </div>
                              <div className="cell-company-domain">{b.research_domain}</div>
                            </div>
                          </div>
                        ) : (
                          <div className="cell-company">
                            <Avatar name={b.contact_name || b.contact_email || "?"} size="sm" />
                            <div>
                              <div className="cell-company-name">
                                Unmatched booking
                                {b.calendly_url && <Icon name="calendar" size={13} />}
                                {b.meeting_status === "canceled" && <span className="cell-muted"> · canceled</span>}
                                {b.no_show && <span className="cell-muted"> · no-show</span>}
                              </div>
                              <div className="cell-company-domain">not in this pipeline</div>
                            </div>
                          </div>
                        )}
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
                        {b.campaign_id ? (
                          <a
                            href={`#campaign-${b.campaign_id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              onOpenCampaign(b.campaign_id!, b.campaign_name);
                            }}
                          >
                            {b.campaign_name || <span className="cell-muted">—</span>}
                          </a>
                        ) : (
                          <span className="cell-muted">—</span>
                        )}
                      </td>
                      <td>{formatWhen(b.booked_at)}</td>
                      <td>{formatWhen(b.meeting_starts_at)}</td>
                      <td>
                        <span className="cell-muted">
                          {b.booked_by ? BOOKED_BY_LABEL[b.booked_by] || b.booked_by : "—"}
                        </span>
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {b.event_uri && (
                          <div className="menu-wrap">
                            <button
                              type="button"
                              className="ghost menu-trigger"
                              title="More actions"
                              disabled={actingUri === b.event_uri}
                              onClick={(e) => openMenu(b, e)}
                            >
                              <Icon name="more" size={16} />
                            </button>
                            {menuOpenUri === b.event_uri && menuPos && (
                              <>
                                <div className="menu-backdrop" onClick={() => setMenuOpenUri(null)} />
                                <div
                                  className="menu-popover menu-popover--fixed"
                                  style={{ top: menuPos.top, right: menuPos.right }}
                                >
                                  {isPast(b) && b.meeting_status !== "canceled" && (
                                    b.no_show ? (
                                      <button
                                        type="button"
                                        className="menu-item"
                                        onClick={() => {
                                          setMenuOpenUri(null);
                                          unmarkNoShow(b);
                                        }}
                                      >
                                        <Icon name="check" size={14} />
                                        Unmark no-show
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        className="menu-item"
                                        onClick={() => {
                                          setMenuOpenUri(null);
                                          setConfirmNoShow(b);
                                        }}
                                      >
                                        <Icon name="ban" size={14} />
                                        Mark no-show
                                      </button>
                                    )
                                  )}
                                  {isFuture(b) && b.meeting_status !== "canceled" && (
                                    <button
                                      type="button"
                                      className="menu-item menu-item--danger"
                                      onClick={() => {
                                        setMenuOpenUri(null);
                                        setConfirmCancel(b);
                                      }}
                                    >
                                      <Icon name="trash" size={14} />
                                      Cancel booking
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    className="menu-item menu-item--danger"
                                    onClick={() => {
                                      setMenuOpenUri(null);
                                      setConfirmDelete(b);
                                    }}
                                  >
                                    <Icon name="trash" size={14} />
                                    Delete
                                  </button>
                                </div>
                              </>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {confirmCancel && (
        <ConfirmModal
          title="Cancel booking?"
          message={`Cancel the Calendly booking for ${confirmCancel.contact_name || confirmCancel.contact_email || "this contact"}? This cannot be undone.`}
          danger
          confirmLabel="Cancel booking"
          onConfirm={() => {
            const b = confirmCancel;
            setConfirmCancel(null);
            cancelBooking(b);
          }}
          onCancel={() => setConfirmCancel(null)}
        />
      )}

      {confirmNoShow && (
        <ConfirmModal
          title="Mark as no-show?"
          message={`Mark ${confirmNoShow.contact_name || confirmNoShow.contact_email || "this contact"} as a no-show for this meeting? You can undo this later.`}
          confirmLabel="Mark no-show"
          onConfirm={() => {
            const b = confirmNoShow;
            setConfirmNoShow(null);
            markNoShow(b);
          }}
          onCancel={() => setConfirmNoShow(null)}
        />
      )}

      {confirmDelete && (
        <ConfirmModal
          title="Delete booking?"
          message={`Permanently delete the booking for ${confirmDelete.contact_name || confirmDelete.contact_email || "this contact"}? If it's still an upcoming meeting, it will be canceled on Calendly first. This removes it everywhere and cannot be undone.`}
          danger
          confirmLabel="Delete"
          onConfirm={() => {
            const b = confirmDelete;
            setConfirmDelete(null);
            deleteBooking(b);
          }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </>
  );
}

function EventTypesTab() {
  const { data: eventTypes, loading: loadingTypes } = useCachedFetch("calendly-event-types", () => calendlyApi.listEventTypes());
  const { data: schedules, loading: loadingAvailability } = useCachedFetch("calendly-availability", () => calendlyApi.listAvailability());
  const [generatingUri, setGeneratingUri] = useState<string | null>(null);
  const [linksByUri, setLinksByUri] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);

  const generateLink = async (et: EventType) => {
    setGeneratingUri(et.uri);
    setMessage(null);
    try {
      const result = await calendlyApi.createSchedulingLink(et.uri);
      setLinksByUri((cur) => ({ ...cur, [et.uri]: result.booking_url }));
      try {
        await navigator.clipboard.writeText(result.booking_url);
        setMessage("Scheduling link generated and copied to clipboard.");
      } catch {
        setMessage("Scheduling link generated.");
      }
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to generate scheduling link");
    } finally {
      setGeneratingUri(null);
    }
  };

  const copyExistingLink = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setMessage("Scheduling link copied to clipboard.");
    } catch {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  };

  const sortedSchedules = (schedules ?? []).slice();

  return (
    <>
      {message && <p className="cell-muted" style={{ margin: "-8px 0 16px" }}>{message}</p>}

      {loadingTypes ? (
        <p className="hint">Loading event types…</p>
      ) : !eventTypes || eventTypes.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="calendar"
            message="No active event types found on this Calendly account. If you have existing bookings, their event type may have been deleted on Calendly -- deleted event types no longer appear here even though past bookings still reference them. Create or restore one in Calendly directly."
          />
        </div>
      ) : (
        <div className="event-type-grid">
          {eventTypes.map((et) => (
            <div className="event-type-card" key={et.uri}>
              <div className="event-type-card-head">
                <div style={{ display: "flex", alignItems: "center", minWidth: 0 }}>
                  <span className="event-type-color-dot" style={{ background: et.color || "var(--text-faint)" }} />
                  <span className="cell-company-name">{et.name}</span>
                </div>
                <span className={`chip${et.active ? " chip-active" : ""}`}>{et.active ? "Active" : "Inactive"}</span>
              </div>

              <div className="event-type-card-meta">
                <span className="meta-chip">
                  <Icon name="clock" size={13} />
                  {formatDuration(et.duration)}
                </span>
              </div>

              <div className="event-type-card-foot">
                {et.scheduling_url && (
                  <a href={et.scheduling_url} target="_blank" rel="noopener noreferrer">
                    Edit in Calendly
                  </a>
                )}
                {linksByUri[et.uri] ? (
                  <button type="button" className="ghost" onClick={() => copyExistingLink(linksByUri[et.uri])}>
                    <Icon name="link" size={13} />
                    Copy link
                  </button>
                ) : (
                  <button
                    type="button"
                    className="ghost"
                    disabled={generatingUri === et.uri}
                    onClick={() => generateLink(et)}
                  >
                    <Icon name="link" size={13} />
                    {generatingUri === et.uri ? "Generating…" : "Get scheduling link"}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <h2 style={{ marginTop: 32 }}>Availability</h2>
      {loadingAvailability ? (
        <p className="hint">Loading availability…</p>
      ) : sortedSchedules.length === 0 ? (
        <div className="card">
          <EmptyState icon="clock" message="No availability schedules found on this Calendly account." />
        </div>
      ) : (
        sortedSchedules.map((schedule) => {
          const rules = schedule.rules.slice().sort((a, b) => {
            const ai = WEEKDAY_ORDER.indexOf(String(a.wday));
            const bi = WEEKDAY_ORDER.indexOf(String(b.wday));
            if (ai === -1 || bi === -1) return 0;
            return ai - bi;
          });
          return (
            <div className="card" key={schedule.uri} style={{ marginBottom: 16 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <strong>{schedule.name}{schedule.default && <span className="cell-muted"> · default</span>}</strong>
                <span className="cell-muted">{schedule.timezone}</span>
              </div>
              <div className="availability-rules">
                {rules.length === 0 ? (
                  <span className="cell-muted">No rules configured.</span>
                ) : (
                  rules.map((rule, i) => (
                    <div className="availability-rule-row" key={i}>
                      <span className="rule-day">{ruleLabel(rule)}</span>
                      <span className="cell-muted">{formatIntervals(rule)}</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          );
        })
      )}
    </>
  );
}
