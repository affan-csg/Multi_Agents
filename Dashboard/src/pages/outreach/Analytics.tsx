import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import { outreachApi, type Campaign, type OutreachAnalytics, type Stage } from "../../agents/outreach";
import { ChartCardSkeleton, EmptyState, Icon, StatCard, StatCardSkeleton } from "../../components/ui";

const TIER_COLORS: Record<string, string> = {
  A: "#5145e8",
  B: "#23c08b",
  C: "#8b83f0",
  D: "#c9cdd8",
};

const STAGE_COLORS: Record<Stage, string> = {
  needs_review: "#c9cdd8",
  pending: "#4f7df3",
  sent: "#23c08b",
  waiting: "#e8a33d",
  replied: "#23c08b",
  booked: "#6f63f2",
  failed: "#e8734a",
};

const STAGE_LABEL: Record<Stage, string> = {
  needs_review: "Needs Review",
  pending: "Pending",
  sent: "Sent",
  waiting: "Waiting",
  replied: "Replied",
  booked: "Booked",
  failed: "Failed",
};

const SOURCE_LABEL: Record<string, string> = {
  pain_summary: "Pain summary",
  recommended_angle: "Recommended angle",
  research_data: "Research data (LLM)",
  web_search: "Web search (LLM)",
  template_only: "Generic template",
  revision: "Manually edited",
  not_drafted: "Not drafted yet",
};

const SOURCE_COLORS: Record<string, string> = {
  pain_summary: "#5145e8",
  recommended_angle: "#23c08b",
  research_data: "#4f7df3",
  web_search: "#0ea5a0",
  template_only: "#c9cdd8",
  revision: "#8b5cf6",
  not_drafted: "#e8a33d",
};

const AXIS = { stroke: "#8e96a8", fontSize: 12 };
const GRID = "#e8eaee";

const tooltipStyle = {
  background: "#fff",
  border: "1px solid #e8eaee",
  borderRadius: 10,
  boxShadow: "0 4px 14px rgba(20,23,41,.1)",
  fontSize: 13,
};

interface Props {
  campaignId: string | "all";
  campaigns: Campaign[];
}

export function Analytics({ campaignId, campaigns }: Props) {
  const { data, error: fetchError } = useCachedFetch<OutreachAnalytics>(
    `outreach-analytics:${campaignId}`,
    () => outreachApi.analytics(campaignId),
  );
  const error = fetchError ? String(fetchError) : null;

  const campaignLabel =
    campaignId === "all" ? "All campaigns" : campaigns.find((c) => c.campaign_id === campaignId)?.name || campaignId;

  if (error) {
    return (
      <>
        <Head />
        <p className="error-text">{error}</p>
      </>
    );
  }

  if (!data) {
    return (
      <>
        <Head label={campaignLabel} />
        <div className="stat-grid">
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
        </div>
        <div className="chart-grid">
          <ChartCardSkeleton />
          <ChartCardSkeleton />
          <ChartCardSkeleton tall />
          <ChartCardSkeleton tall />
          <ChartCardSkeleton />
          <ChartCardSkeleton />
        </div>
      </>
    );
  }

  if (data.total_items === 0) {
    return (
      <>
        <Head />
        <div className="card">
          <EmptyState
            icon="chart"
            message={campaignId === "all" ? "No campaigns have any prospects yet." : "This campaign has no prospects yet."}
          />
        </div>
      </>
    );
  }

  const topIndustry = data.industries[0];
  const donutData = data.personalized_from.map((s) => ({
    name: SOURCE_LABEL[s.source] || s.source,
    key: s.source,
    value: s.count,
  }));

  return (
    <>
      <Head label={campaignLabel} />

      <div className="stat-grid">
        <StatCard icon="send" tone="accent" value={data.total_items.toLocaleString()} label="Prospects in scope" />
        <StatCard icon="info" tone="amber" value={data.needs_review_count.toLocaleString()} label="Needs review" />
        <StatCard icon="check" tone="mint" value={data.sent_total.toLocaleString()} label="Messages sent" />
        <StatCard icon="mail" tone="violet" value={`${data.reply_rate}%`} label="Reply rate" />
        <StatCard icon="calendar" tone="amber" value={`${data.booking_rate}%`} label="Booking rate" />
      </div>

      <div className="chart-grid">
        <div className="card chart-wide">
          <h3 className="card-title">
            <Icon name="chart" size={16} />
            Pipeline funnel
          </h3>
          <p className="card-sub">Where every prospect currently sits in the 5-stage pipeline.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart
              data={data.stage_funnel}
              layout="vertical"
              margin={{ top: 4, right: 24, bottom: 4, left: 8 }}
            >
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <YAxis
                type="category"
                dataKey="stage"
                tickFormatter={(s) => STAGE_LABEL[s as Stage] ?? s}
                tickLine={false}
                axisLine={false}
                tick={{ ...AXIS, fontSize: 11 }}
                width={80}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                cursor={{ fill: "rgba(81,69,232,.06)" }}
                formatter={(v) => [v, "Prospects"]}
                labelFormatter={(s) => STAGE_LABEL[s as Stage] ?? s}
              />
              <Bar dataKey="count" radius={[0, 6, 6, 0]} maxBarSize={28}>
                {data.stage_funnel.map((s) => (
                  <Cell key={s.stage} fill={STAGE_COLORS[s.stage]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="sparkle" size={16} />
            Personalization source
          </h3>
          <p className="card-sub">How each drafted email was written.</p>
          <ResponsiveContainer width="100%" height={260}>
            <PieChart>
              <Pie
                data={donutData}
                dataKey="value"
                nameKey="name"
                innerRadius={58}
                outerRadius={90}
                paddingAngle={2}
              >
                {donutData.map((d) => (
                  <Cell key={d.key} fill={SOURCE_COLORS[d.key] || "#8e96a8"} />
                ))}
              </Pie>
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 11.5 }} verticalAlign="bottom" />
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="target" size={16} />
            Prospect tier mix
          </h3>
          <p className="card-sub">ICP tier of the prospects being messaged.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data.tiers} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="tier" tickLine={false} axisLine={false} tick={AXIS} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Bar dataKey="count" name="Prospects" radius={[6, 6, 0, 0]} maxBarSize={64}>
                {data.tiers.map((t) => (
                  <Cell key={t.tier} fill={TIER_COLORS[t.tier] || "#8e96a8"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card chart-wide">
          <h3 className="card-title">
            <Icon name="analytics" size={16} />
            Sends over time
          </h3>
          <p className="card-sub">Messages actually sent, by day.</p>
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={data.sends_over_time} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <defs>
                <linearGradient id="sendsFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#5145e8" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="#5145e8" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Area type="monotone" dataKey="count" name="Sent" stroke="#5145e8" strokeWidth={2} fill="url(#sendsFill)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="briefcase" size={16} />
            Top industries
          </h3>
          <p className="card-sub">Most represented industries in this scope.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart
              data={data.industries}
              layout="vertical"
              margin={{ top: 4, right: 16, bottom: 4, left: 8 }}
            >
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <YAxis
                type="category"
                dataKey="industry"
                tickLine={false}
                axisLine={false}
                tick={{ ...AXIS, fontSize: 11 }}
                width={120}
              />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Bar dataKey="count" name="Prospects" fill="#5145e8" radius={[0, 5, 5, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="close" size={16} />
            Delivery health
          </h3>
          <p className="card-sub">
            {data.failed_count} of {data.sent_total + data.failed_count} send attempt
            {data.sent_total + data.failed_count === 1 ? "" : "s"} failed
            {topIndustry ? ` · top industry: ${topIndustry.industry}` : ""}.
          </p>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart
              data={[
                { name: "Sent", value: data.sent_total },
                { name: "Failed", value: data.failed_count },
              ]}
              margin={{ top: 8, right: 16, bottom: 4, left: -18 }}
            >
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="name" tickLine={false} axisLine={false} tick={AXIS} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Line type="monotone" dataKey="value" stroke="#e8734a" strokeWidth={2} dot={{ r: 4 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </>
  );
}

function Head({ label }: { label?: string | null }) {
  return (
    <div className="page-head">
      <div className="page-head-text">
        <p className="breadcrumb">Outreach Agent / <span>Analytics</span></p>
        <h1>Analytics</h1>
        <p>Pipeline health across your outreach — funnel progression, personalization, and reply performance.</p>
      </div>
      {label && (
        <div className="page-head-actions">
          <span className="run-badge">{label}</span>
        </div>
      )}
    </div>
  );
}
