import { useMemo, useState, type ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import { outreachApi, type Campaign } from "../../agents/outreach";
import {
  analyticsApi,
  type AccountRow,
  type AnalyticsFilters,
  type AnalyticsSummary,
  type BookingRow,
  type CampaignRow,
  type ProspectRow,
  type Rate,
  type TrendPoint,
} from "../../agents/analytics";
import { AXIS, GRID, METRIC_COLORS, UNKNOWN_COLOR, senderColor, tooltipStyle } from "../../components/charts";
import {
  ChartCardSkeleton,
  EmptyState,
  Icon,
  StatCard,
  StatCardSkeleton,
  formatDate,
} from "../../components/ui";

type Tab = "overview" | "accounts" | "campaigns" | "bookings";
type Range = "7" | "30" | "90" | "all" | "custom";

const TABS: Array<{ id: Tab; label: string; icon: "chart" | "mail" | "send" | "calendar" }> = [
  { id: "overview", label: "Overview", icon: "chart" },
  { id: "accounts", label: "Email Accounts", icon: "mail" },
  { id: "campaigns", label: "Campaigns", icon: "send" },
  { id: "bookings", label: "Bookings", icon: "calendar" },
];

const RANGES: Array<{ id: Range; label: string }> = [
  { id: "7", label: "Last 7 days" },
  { id: "30", label: "Last 30 days" },
  { id: "90", label: "Last 90 days" },
  { id: "all", label: "All time" },
  { id: "custom", label: "Custom range" },
];

function isoDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function presetFrom(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - (days - 1));
  return isoDay(d);
}

const pct = (r: Rate | undefined): string => (r && r.pct != null ? `${r.pct}%` : "—");
const frac = (r: Rate | undefined): string => (r ? `${r.n} of ${r.d}` : "");
const num = (n: number | undefined): string => (n ?? 0).toLocaleString();

function RateCell({ rate }: { rate: Rate }) {
  return (
    <span
      title={`${frac(rate)}${rate.low_sample ? " — small sample, read with caution" : ""}`}
      style={rate.low_sample ? { opacity: 0.55 } : undefined}
    >
      {pct(rate)}
    </span>
  );
}

interface Props {
  campaignId: string | "all";
  campaigns: Campaign[];
  onCampaignChange: (id: string | "all") => void;
  /** The pre-existing pipeline view (funnel, tiers, industries ...), kept
   *  unchanged beneath the new Overview. */
  pipeline: ReactNode;
}

export function AnalyticsDashboard({ campaignId, campaigns, onCampaignChange, pipeline }: Props) {
  const [tab, setTab] = useState<Tab>("overview");
  const [sender, setSender] = useState<string>("all");
  const [range, setRange] = useState<Range>("30");
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [openItem, setOpenItem] = useState<{ itemId: string; title: string; sender: string | null } | null>(null);

  const filters: AnalyticsFilters = useMemo(() => {
    const f: AnalyticsFilters = {};
    if (sender !== "all") f.sender = sender;
    if (campaignId !== "all") f.campaignId = campaignId;
    if (range === "custom") {
      if (custom.from) f.dateFrom = custom.from;
      if (custom.to) f.dateTo = custom.to;
    } else if (range !== "all") {
      f.dateFrom = presetFrom(Number(range));
    }
    return f;
  }, [sender, campaignId, range, custom]);

  const key = `outreach-analytics2:${sender}:${campaignId}:${filters.dateFrom ?? ""}:${filters.dateTo ?? ""}`;
  const { data, error } = useCachedFetch<AnalyticsSummary>(key, () => analyticsApi.summary(filters));

  // The sender list comes with every response; remember the last one so the
  // selector does not blank out while a new filter set is loading.
  const [senders, setSenders] = useState<AnalyticsSummary["senders"]>([]);
  if (data && data.senders !== senders) setSenders(data.senders);

  const senderLabel = (s: string | null): string =>
    !s ? "—" : senders.find((x) => x.sender === s)?.label ?? s;
  const campaignLabel =
    campaignId === "all" ? null : campaigns.find((c) => c.campaign_id === campaignId)?.name || campaignId;

  // Colour per account by stable position, so it is the same on every chart.
  const colorOf = (s: string): string => {
    if (s === "unknown") return UNKNOWN_COLOR;
    const idx = senders.filter((x) => x.sender !== "unknown").findIndex((x) => x.sender === s);
    return senderColor(idx < 0 ? 0 : idx);
  };

  function drillAccount(s: string) {
    setSender(s);
    setTab("campaigns");
  }

  const crumbs = (
    <div className="analytics-crumbs">
      <button type="button" onClick={() => { setSender("all"); onCampaignChange("all"); }}>All accounts</button>
      {sender !== "all" && (
        <>
          <span>›</span>
          <button type="button" onClick={() => onCampaignChange("all")}>{senderLabel(sender)}</button>
        </>
      )}
      {campaignLabel && (
        <>
          <span>›</span>
          <button type="button" disabled>{campaignLabel}</button>
        </>
      )}
    </div>
  );

  return (
    <>
      <div className="analytics-filters">
        <div className="run-selector">
          <Icon name="mail" size={14} />
          <span className="run-selector-label">Account:</span>
          <select value={sender} onChange={(e) => setSender(e.target.value)}>
            <option value="all">All accounts</option>
            {senders.map((s) => (
              <option key={s.sender} value={s.sender}>{s.label}</option>
            ))}
          </select>
        </div>
        <div className="run-selector">
          <Icon name="calendar" size={14} />
          <span className="run-selector-label">Dates:</span>
          <select value={range} onChange={(e) => setRange(e.target.value as Range)}>
            {RANGES.map((r) => (
              <option key={r.id} value={r.id}>{r.label}</option>
            ))}
          </select>
          {range === "custom" && (
            <>
              <input
                type="date"
                aria-label="From date"
                value={custom.from}
                onChange={(e) => setCustom({ ...custom, from: e.target.value })}
              />
              <input
                type="date"
                aria-label="To date"
                value={custom.to}
                onChange={(e) => setCustom({ ...custom, to: e.target.value })}
              />
            </>
          )}
        </div>
      </div>

      {(sender !== "all" || campaignId !== "all") && crumbs}

      <div className="tabs" role="tablist" style={{ marginBottom: 20 }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`tab${tab === t.id ? " tab--active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            <Icon name={t.icon} size={15} />
            {t.label}
          </button>
        ))}
      </div>

      {error && <p className="error-text">{String(error)}</p>}
      {!error && !data && <DashboardSkeleton />}

      {data && (
        <>
          {Object.keys(data.errors).length > 0 && (
            <p className="hint" role="status">
              Some sections could not be loaded ({Object.keys(data.errors).join(", ")}); the rest is shown.
            </p>
          )}
          {tab === "overview" && (
            <>
              <OverviewTab data={data} />
              <h2 className="analytics-section-title">Pipeline</h2>
              <p className="hint">
                Current stage of every prospect (all time, campaign filter only). Before/after trends and
                strategy insights live under Learning &amp; Optimization.
              </p>
              {pipeline}
            </>
          )}
          {tab === "accounts" && <AccountsTab data={data} colorOf={colorOf} onDrill={drillAccount} />}
          {tab === "campaigns" && (
            <CampaignsTab
              data={data}
              filters={filters}
              campaignId={campaignId}
              senderLabel={senderLabel}
              onPickCampaign={onCampaignChange}
              onOpenItem={(p) => setOpenItem({ itemId: p.item_id, title: p.company_name || p.item_id, sender: p.sender })}
            />
          )}
          {tab === "bookings" && (
            <BookingsTab
              data={data}
              senderLabel={senderLabel}
              onOpenItem={(b) =>
                b.item_id && setOpenItem({ itemId: b.item_id, title: b.company_name || b.contact_email || b.item_id, sender: b.sender })
              }
            />
          )}
        </>
      )}

      {openItem && (
        <ConversationDrawer
          itemId={openItem.itemId}
          title={openItem.title}
          sender={senderLabel(openItem.sender)}
          onClose={() => setOpenItem(null)}
        />
      )}
    </>
  );
}

function DashboardSkeleton() {
  return (
    <>
      <div className="stat-grid">
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
      </div>
      <div className="chart-grid">
        <ChartCardSkeleton />
        <ChartCardSkeleton />
      </div>
    </>
  );
}

// --- Overview ---------------------------------------------------------------

const TREND_LINES: Array<{ key: keyof TrendPoint; name: string; color: string }> = [
  { key: "sent", name: "Sent", color: METRIC_COLORS.sent },
  { key: "replies", name: "Replies", color: METRIC_COLORS.replies },
  { key: "positive_replies", name: "Positive replies", color: METRIC_COLORS.positive_replies },
  { key: "bookings", name: "Bookings", color: METRIC_COLORS.bookings },
  { key: "follow_ups", name: "Follow-ups", color: METRIC_COLORS.follow_ups },
];

function OverviewTab({ data }: { data: AnalyticsSummary }) {
  const o = data.overall;
  if (!o.reply_rate) {
    return <div className="card"><EmptyState icon="chart" message="Overall figures are unavailable right now." /></div>;
  }
  if (!o.sent && !o.failed) {
    return (
      <div className="card">
        <EmptyState icon="chart" message="No emails were sent for these filters and dates." />
      </div>
    );
  }
  const rates = [
    { name: "Reply rate", pct: o.reply_rate.pct ?? 0, fill: METRIC_COLORS.replies },
    { name: "Positive-reply rate", pct: o.positive_reply_rate?.pct ?? 0, fill: METRIC_COLORS.positive_replies },
    { name: "Booking rate", pct: o.booking_rate?.pct ?? 0, fill: METRIC_COLORS.bookings },
  ];
  return (
    <>
      <div className="stat-grid">
        <StatCard icon="send" tone="accent" value={num(o.sent)} label="Emails sent" />
        <StatCard icon="mail" tone="mint" value={num(o.replies)} label="Replies" />
        <StatCard icon="sparkle" tone="mint" value={num(o.positive_replies)} label="Positive replies" />
        <StatCard icon="clock" tone="amber" value={num(o.follow_ups_sent)} label="Follow-ups sent" />
        <StatCard icon="calendar" tone="violet" value={num(o.bookings)} label="Bookings" />
        <StatCard icon="info" tone="amber" value={num(o.no_replies)} label="No reply" />
        <StatCard icon="close" tone="amber" value={num(o.failed)} label="Failed sends" />
        <StatCard icon="close" tone="amber" value={num(o.bounced)} label="Bounces detected" />
        <StatCard icon="chart" tone="mint" value={pct(o.reply_rate)} label={`Reply rate (${frac(o.reply_rate)})`} />
        <StatCard icon="chart" tone="mint" value={pct(o.positive_reply_rate)} label="Positive-reply rate" />
        <StatCard icon="chart" tone="violet" value={pct(o.booking_rate)} label="Booking rate" />
        <StatCard icon="chart" tone="amber" value={pct(o.failure_rate)} label="Failure rate" />
      </div>
      <p className="hint">
        Counts and rates cover emails first sent in the selected dates. Bounces are detected from bounce
        notices only; failed sends are emails the mail server rejected.
      </p>

      <div className="chart-grid">
        <div className="card chart-wide">
          <h3 className="card-title"><Icon name="analytics" size={16} /> Activity over time</h3>
          <p className="card-sub">Each event on the day it happened (a reply counts on the day it arrived).</p>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={data.trend} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} />
              {TREND_LINES.map((l) => (
                <Line key={l.key} type="monotone" dataKey={l.key} name={l.name} stroke={l.color} strokeWidth={2} dot={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title"><Icon name="target" size={16} /> Reply vs positive vs booking</h3>
          <p className="card-sub">Share of emails sent in the period.</p>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={rates} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} unit="%" />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => [`${v}%`, "Rate"]} />
              <Bar dataKey="pct" radius={[6, 6, 0, 0]} maxBarSize={56}>
                {rates.map((r) => (
                  <Cell key={r.name} fill={r.fill} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title"><Icon name="clock" size={16} /> Follow-up performance</h3>
          {data.followups.rows.length === 0 ? (
            <EmptyState icon="clock" message="No follow-ups have been sent in this period." />
          ) : (
            <>
              <p className="card-sub">{data.followups.note}</p>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart
                  data={data.followups.rows.map((r) => ({
                    name: `Follow-up #${r.followup_number}`,
                    sent: r.sent,
                    replied_after: r.replied_after,
                  }))}
                  margin={{ top: 4, right: 8, bottom: 4, left: -18 }}
                >
                  <CartesianGrid stroke={GRID} vertical={false} />
                  <XAxis dataKey="name" tickLine={false} axisLine={false} tick={AXIS} />
                  <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
                  <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
                  <Legend wrapperStyle={{ fontSize: 11.5 }} />
                  <Bar dataKey="sent" name="Sent" fill={METRIC_COLORS.follow_ups} radius={[6, 6, 0, 0]} maxBarSize={40} />
                  <Bar dataKey="replied_after" name="Replied afterwards" fill={METRIC_COLORS.replies} radius={[6, 6, 0, 0]} maxBarSize={40} />
                </BarChart>
              </ResponsiveContainer>
            </>
          )}
        </div>
      </div>
    </>
  );
}

// --- Email accounts -----------------------------------------------------------

function AccountsTab({
  data, colorOf, onDrill,
}: {
  data: AnalyticsSummary;
  colorOf: (s: string) => string;
  onDrill: (sender: string) => void;
}) {
  const accounts = data.accounts;
  if (accounts.length === 0) {
    return <div className="card"><EmptyState icon="mail" message="No sending accounts found." /></div>;
  }

  const volumes = accounts.map((a) => ({
    name: a.label, sent: a.sent, replies: a.replies, positive_replies: a.positive_replies,
    bookings: a.bookings, follow_ups: a.follow_ups_sent,
  }));
  const rates = accounts.map((a) => ({
    name: a.label,
    reply: a.reply_rate.pct ?? 0,
    positive: a.positive_reply_rate.pct ?? 0,
    booking: a.booking_rate.pct ?? 0,
  }));

  // One row per day with a column per account for the sent-per-day lines.
  // Columns are keyed by position, not address: recharts reads a dataKey
  // containing "." as a nested path, which an email address always does.
  const days = new Map<string, Record<string, number | string>>();
  accounts.forEach((a, idx) => {
    for (const t of a.trend) {
      const row = days.get(t.date) ?? { date: t.date };
      row[`a${idx}`] = t.sent;
      days.set(t.date, row);
    }
  });
  const trend = [...days.values()].sort((x, y) => String(x.date).localeCompare(String(y.date)));

  return (
    <>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3 className="card-title"><Icon name="mail" size={16} /> Email account performance</h3>
        <p className="card-sub">
          Every email is attributed to the mailbox that actually sent it. Click an account to see its campaigns.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Account</th><th>Sent</th><th>Replies</th><th>Positive</th><th>Follow-ups</th>
                <th>Bookings</th><th>Failed</th><th>Reply rate</th><th>Positive rate</th><th>Booking rate</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a: AccountRow) => (
                <tr key={a.sender} className="row-click" onClick={() => onDrill(a.sender)}>
                  <td>
                    <span className="sender-dot" style={{ background: colorOf(a.sender) }} />
                    {a.label}
                    {!a.configured && a.sender !== "unknown" && <span className="meta-chip">not configured</span>}
                  </td>
                  <td>{num(a.sent)}</td><td>{num(a.replies)}</td><td>{num(a.positive_replies)}</td>
                  <td>{num(a.follow_ups_sent)}</td><td>{num(a.bookings)}</td><td>{num(a.failed)}</td>
                  <td><RateCell rate={a.reply_rate} /></td>
                  <td><RateCell rate={a.positive_reply_rate} /></td>
                  <td><RateCell rate={a.booking_rate} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="chart-grid">
        <div className="card">
          <h3 className="card-title"><Icon name="chart" size={16} /> Volume by account</h3>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={volumes} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} />
              <Bar dataKey="sent" name="Sent" fill={METRIC_COLORS.sent} radius={[4, 4, 0, 0]} />
              <Bar dataKey="replies" name="Replies" fill={METRIC_COLORS.replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="positive_replies" name="Positive" fill={METRIC_COLORS.positive_replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="bookings" name="Bookings" fill={METRIC_COLORS.bookings} radius={[4, 4, 0, 0]} />
              <Bar dataKey="follow_ups" name="Follow-ups" fill={METRIC_COLORS.follow_ups} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title"><Icon name="target" size={16} /> Rates by account</h3>
          <p className="card-sub">Share of each account's emails sent in the period.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={rates} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} unit="%" />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => `${v}%`} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} />
              <Bar dataKey="reply" name="Reply" fill={METRIC_COLORS.replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="positive" name="Positive reply" fill={METRIC_COLORS.positive_replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="booking" name="Booking" fill={METRIC_COLORS.bookings} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card chart-wide">
          <h3 className="card-title"><Icon name="analytics" size={16} /> Emails sent per day, by account</h3>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={trend} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} />
              {accounts.map((a, idx) => (
                <Line
                  key={a.sender}
                  type="monotone"
                  dataKey={`a${idx}`}
                  name={a.label}
                  stroke={colorOf(a.sender)}
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </>
  );
}

// --- Campaigns + prospect drill-down --------------------------------------------

function CampaignsTab({
  data, filters, campaignId, senderLabel, onPickCampaign, onOpenItem,
}: {
  data: AnalyticsSummary;
  filters: AnalyticsFilters;
  campaignId: string | "all";
  senderLabel: (s: string | null) => string;
  onPickCampaign: (id: string) => void;
  onOpenItem: (p: ProspectRow) => void;
}) {
  const rows = data.campaigns;
  const chart = rows.slice(0, 10).map((c) => ({
    name: c.name, sent: c.sent, replies: c.replies, positive_replies: c.positive_replies, bookings: c.bookings,
  }));

  return (
    <>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3 className="card-title"><Icon name="send" size={16} /> Campaign performance</h3>
        <p className="card-sub">Click a campaign to list its prospects, then a prospect to read the conversation.</p>
        {rows.length === 0 ? (
          <EmptyState icon="send" message="No campaigns sent emails for these filters and dates." />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Campaign</th><th>Sending email</th><th>Prospects</th><th>Sent</th><th>Replies</th>
                  <th>Positive</th><th>Follow-ups</th><th>Bookings</th>
                  <th>Reply rate</th><th>Positive rate</th><th>Booking rate</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c: CampaignRow) => (
                  <tr
                    key={c.campaign_id}
                    className={`row-click${c.campaign_id === campaignId ? " row-selected" : ""}`}
                    onClick={() => onPickCampaign(c.campaign_id)}
                  >
                    <td>{c.name}</td>
                    <td>{c.senders.length ? c.senders.map((s) => senderLabel(s)).join(", ") : "—"}</td>
                    <td>{num(c.prospects)}</td><td>{num(c.sent)}</td><td>{num(c.replies)}</td>
                    <td>{num(c.positive_replies)}</td><td>{num(c.follow_ups_sent)}</td><td>{num(c.bookings)}</td>
                    <td><RateCell rate={c.reply_rate} /></td>
                    <td><RateCell rate={c.positive_reply_rate} /></td>
                    <td><RateCell rate={c.booking_rate} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {rows.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3 className="card-title"><Icon name="chart" size={16} /> Campaign comparison</h3>
          <p className="card-sub">Top {chart.length} campaigns by emails sent.</p>
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={chart} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} />
              <Bar dataKey="sent" name="Sent" fill={METRIC_COLORS.sent} radius={[4, 4, 0, 0]} />
              <Bar dataKey="replies" name="Replies" fill={METRIC_COLORS.replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="positive_replies" name="Positive" fill={METRIC_COLORS.positive_replies} radius={[4, 4, 0, 0]} />
              <Bar dataKey="bookings" name="Bookings" fill={METRIC_COLORS.bookings} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {campaignId !== "all" && <ProspectsPanel filters={filters} senderLabel={senderLabel} onOpenItem={onOpenItem} />}
    </>
  );
}

function ProspectsPanel({
  filters, senderLabel, onOpenItem,
}: {
  filters: AnalyticsFilters;
  senderLabel: (s: string | null) => string;
  onOpenItem: (p: ProspectRow) => void;
}) {
  const key = `outreach-analytics2-prospects:${filters.sender ?? ""}:${filters.campaignId ?? ""}:${filters.dateFrom ?? ""}:${filters.dateTo ?? ""}`;
  const { data, error } = useCachedFetch(key, () => analyticsApi.prospects(filters));

  return (
    <div className="card">
      <h3 className="card-title"><Icon name="users" size={16} /> Prospects</h3>
      {error && <p className="error-text">{String(error)}</p>}
      {!error && !data && <p className="hint">Loading prospects…</p>}
      {data && data.rows.length === 0 && <EmptyState icon="users" message="No prospects for these filters and dates." />}
      {data && data.rows.length > 0 && (
        <>
          <p className="card-sub">
            Showing {data.rows.length} of {data.total}. Click a row to open the conversation.
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Prospect</th><th>Sent from</th><th>Sent</th><th>Follow-ups</th><th>Status</th><th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((p) => (
                  <tr key={p.item_id} className="row-click" onClick={() => onOpenItem(p)}>
                    <td>
                      {p.company_name || p.item_id}
                      <div className="cell-company-domain">{p.contact_name || p.contact_email || ""}</div>
                    </td>
                    <td>{senderLabel(p.sender)}</td>
                    <td>{formatDate(p.sent_at)}</td>
                    <td>{p.follow_ups}</td>
                    <td>{p.stage.replace("_", " ")}</td>
                    <td>
                      {p.booked ? "Booked" : p.positive ? "Positive reply" : p.replied ? "Replied"
                        : p.failed ? `Failed${p.error ? ` — ${p.error}` : ""}`
                        : p.bounced ? "Bounce detected" : "No reply"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// --- Bookings -------------------------------------------------------------------

function BookingsTab({
  data, senderLabel, onOpenItem,
}: {
  data: AnalyticsSummary;
  senderLabel: (s: string | null) => string;
  onOpenItem: (b: BookingRow) => void;
}) {
  const b = data.bookings;
  return (
    <>
      <div className="stat-grid">
        <StatCard icon="calendar" tone="violet" value={num(b.active)} label="Active bookings" />
        <StatCard icon="close" tone="amber" value={num(b.canceled)} label="Canceled" />
        <StatCard icon="info" tone="amber" value={num(b.unmatched)} label="Not matched to a prospect" />
      </div>
      <p className="hint">
        Dated by when the booking was recorded. A booking whose invitee used a different address than the
        one emailed cannot be matched to a campaign or sending account, so it appears only when no account
        or campaign filter is set.
      </p>

      {b.rows.length === 0 ? (
        <div className="card"><EmptyState icon="calendar" message="No bookings for these filters and dates." /></div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <h3 className="card-title"><Icon name="analytics" size={16} /> Booking trend</h3>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={b.trend} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
                <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
                <Legend wrapperStyle={{ fontSize: 11.5 }} />
                <Bar dataKey="active" name="Active" stackId="b" fill={METRIC_COLORS.bookings} />
                <Bar dataKey="canceled" name="Canceled" stackId="b" fill={METRIC_COLORS.failed} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>

          <div className="card">
            <h3 className="card-title"><Icon name="calendar" size={16} /> Bookings</h3>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Prospect</th><th>Sent from</th><th>Campaign</th><th>Status</th><th>Booked</th><th>Meeting</th>
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((r, i) => (
                    <tr
                      key={`${r.item_id ?? "x"}-${i}`}
                      className={r.item_id ? "row-click" : undefined}
                      onClick={() => r.item_id && onOpenItem(r)}
                    >
                      <td>
                        {r.company_name || r.contact_name || r.contact_email || "Unknown invitee"}
                        <div className="cell-company-domain">{r.contact_email || ""}</div>
                      </td>
                      <td>{r.matched ? senderLabel(r.sender) : "—"}</td>
                      <td>{r.campaign_name || (r.matched ? "—" : "Not matched")}</td>
                      <td>{r.status}{r.no_show ? " · no-show" : ""}</td>
                      <td>{formatDate(r.booked_at)}</td>
                      <td>{formatDate(r.meeting_starts_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}

// --- Conversation drawer ----------------------------------------------------------

function ConversationDrawer({
  itemId, title, sender, onClose,
}: {
  itemId: string;
  title: string;
  sender: string;
  onClose: () => void;
}) {
  const { data, error } = useCachedFetch(`analytics-conversation:${itemId}`, () => outreachApi.itemConversation(itemId));
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} type="button" aria-label="Close">
          <Icon name="close" size={16} />
        </button>
        <div className="drawer-head">
          <div>
            <h2>{title}</h2>
            <div className="cell-company-domain">Sent from {sender}</div>
          </div>
        </div>

        {error && <p className="error-text">{String(error)}</p>}
        {!error && !data && <p className="hint">Loading conversation…</p>}
        {data && (
          <>
            <div className="drawer-meta">
              <span className="meta-chip">Stage: {data.stage.replace("_", " ")}</span>
              <span className="meta-chip">
                Follow-ups: {data.followup.followups_sent} of {data.followup.followups_total}
              </span>
              {data.followup.next_action && <span className="meta-chip">Next: {data.followup.next_action}</span>}
            </div>
            <h3 className="drawer-section-title">Conversation</h3>
            {data.messages.length === 0 ? (
              <p className="hint">
                No replies or follow-ups yet. The first email is shown on the campaign board.
              </p>
            ) : (
              data.messages.map((m) => (
                <div key={m.message_id} className="drawer-panel" style={{ marginBottom: 10 }}>
                  <div className="drawer-fact-label">
                    {m.direction === "inbound" ? "Reply" : m.draft_pending ? "Draft (not sent)" : "Sent"}
                    {m.from_email ? ` · ${m.from_email}` : ""}
                    {m.intent ? ` · ${m.intent.replace("_", " ")}` : ""}
                    {` · ${formatDate(m.received_at || m.sent_at || m.created_at)}`}
                  </div>
                  {m.subject && <div className="drawer-subject-line">{m.subject}</div>}
                  <div style={{ whiteSpace: "pre-wrap" }}>{m.clean_body}</div>
                </div>
              ))
            )}
          </>
        )}
      </div>
    </div>
  );
}
