import { useCachedFetch } from "../../hooks/useCachedFetch";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { researchApi, type Analytics as AnalyticsData, type RunSummary } from "../../agents/research";
import { ChartCardSkeleton, EmptyState, Icon, StatCard, StatCardSkeleton } from "../../components/ui";

const TIER_COLORS: Record<string, string> = {
  A: "#5145e8",
  B: "#23c08b",
  C: "#8b83f0",
  D: "#c9cdd8",
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
  runId: string | "all" | null;
  runs: RunSummary[];
}

export function Analytics({ runId, runs }: Props) {
  const { data, error: fetchError } = useCachedFetch<AnalyticsData>(
    runId ? `analytics:${runId}` : null,
    () => researchApi.analytics(runId!),
  );
  const error = fetchError ? String(fetchError) : null;

  const runLabel = runId === "all" ? "All research" : runs.find((r) => r.run_id === runId)?.label || runId;

  if (!runId) {
    return (
      <>
        <Head />
        <div className="card">
          <EmptyState icon="chart" message="No completed run yet — start a research run to see analytics." />
        </div>
      </>
    );
  }

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
        <Head label={runLabel} />
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
          <ChartCardSkeleton tall />
          <ChartCardSkeleton />
        </div>
      </>
    );
  }

  if (data.total === 0) {
    return (
      <>
        <Head />
        <div className="card">
          <EmptyState icon="chart" message={runId === "all" ? "No runs have scored any companies yet." : "This run scored no companies."} />
        </div>
      </>
    );
  }

  const topTier = data.tiers.find((t) => t.tier === "A")?.count ?? 0;
  const topIndustry = data.industries[0];

  return (
    <>
      <Head label={runLabel} />

      <div className="stat-grid">
        <StatCard icon="building" tone="accent" value={data.total.toLocaleString()} label="Companies scored" />
        <StatCard icon="target" tone="mint" value={data.avg_score.toFixed(1)} label="Average ICP score" />
        <StatCard icon="star" tone="violet" value={topTier.toLocaleString()} label="Tier A leads" />
        <StatCard
          icon="tag"
          tone="amber"
          value={topIndustry ? topIndustry.count : 0}
          label={topIndustry ? `Top industry: ${topIndustry.industry}` : "Top industry"}
        />
      </div>

      <div className="chart-grid">
        <div className="card">
          <h3 className="card-title">
            <Icon name="chart" size={16} />
            Tier distribution
          </h3>
          <p className="card-sub">How many companies landed in each ICP tier.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data.tiers} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="tier" tickLine={false} axisLine={false} tick={AXIS} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Bar dataKey="count" name="Companies" radius={[6, 6, 0, 0]} maxBarSize={64}>
                {data.tiers.map((t) => (
                  <Cell key={t.tier} fill={TIER_COLORS[t.tier] || "#8e96a8"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="analytics" size={16} />
            Score distribution
          </h3>
          <p className="card-sub">Spread of ICP scores across the run.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data.score_distribution} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="bucket" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} interval={0} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Bar dataKey="count" name="Companies" fill="#5145e8" radius={[5, 5, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="briefcase" size={16} />
            Top industries
          </h3>
          <p className="card-sub">Most represented industries and their average score.</p>
          <ResponsiveContainer width="100%" height={300}>
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
                width={130}
              />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="count" name="Companies" fill="#5145e8" radius={[0, 5, 5, 0]} />
              <Bar dataKey="avg_score" name="Avg score" fill="#23c08b" radius={[0, 5, 5, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="target" size={16} />
            Scoring dimensions
          </h3>
          <p className="card-sub">Average points contributed by each scoring dimension.</p>
          <ResponsiveContainer width="100%" height={300}>
            <RadarChart data={data.dimensions} outerRadius="72%">
              <PolarGrid stroke={GRID} />
              <PolarAngleAxis dataKey="dimension" tick={{ ...AXIS, fontSize: 11 }} />
              <PolarRadiusAxis tick={{ ...AXIS, fontSize: 10 }} axisLine={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Radar
                name="Avg points"
                dataKey="avg"
                stroke="#5145e8"
                fill="#5145e8"
                fillOpacity={0.28}
                strokeWidth={2}
              />
            </RadarChart>
          </ResponsiveContainer>
        </div>

        <div className="card chart-wide">
          <h3 className="card-title">
            <Icon name="search" size={16} />
            Open roles vs. score
          </h3>
          <p className="card-sub">
            Each dot is a company — hiring volume against its ICP score, colored by tier.
          </p>
          <ResponsiveContainer width="100%" height={340}>
            <ScatterChart margin={{ top: 8, right: 16, bottom: 28, left: -12 }}>
              <CartesianGrid stroke={GRID} />
              <XAxis
                type="number"
                dataKey="reqs"
                name="Open roles"
                tickLine={false}
                axisLine={false}
                tick={AXIS}
                label={{ value: "Open roles", position: "bottom", offset: 0, fill: "#8e96a8", fontSize: 12 }}
              />
              <YAxis
                type="number"
                dataKey="score"
                name="ICP score"
                tickLine={false}
                axisLine={false}
                tick={AXIS}
                domain={[0, 100]}
              />
              <ZAxis range={[70, 70]} />
              <Tooltip
                cursor={{ strokeDasharray: "3 3" }}
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null;
                  const p = payload[0].payload as AnalyticsData["scatter"][number];
                  const reqsLabel =
                    p.reqs === 0 && p.hiring_signal === "unknown" ? "open roles unknown" : `${p.reqs} open roles`;
                  return (
                    <div style={{ ...tooltipStyle, padding: "9px 12px" }}>
                      <div style={{ fontWeight: 650, marginBottom: 3 }}>{p.domain}</div>
                      <div style={{ color: "#636b7e" }}>
                        Score {p.score.toFixed(0)} · Tier {p.tier} · {reqsLabel}
                      </div>
                    </div>
                  );
                }}
              />
              {["A", "B", "C", "D"].map((tier) => (
                <Scatter
                  key={tier}
                  name={`Tier ${tier}`}
                  data={data.scatter.filter((d) => d.tier === tier)}
                  fill={TIER_COLORS[tier]}
                  fillOpacity={0.75}
                />
              ))}
              <Legend verticalAlign="top" align="right" wrapperStyle={{ fontSize: 12, paddingBottom: 6 }} />
            </ScatterChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="database" size={16} />
            ATS providers
          </h3>
          <p className="card-sub">Which applicant tracking systems these companies run.</p>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data.providers} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="provider" tickLine={false} axisLine={false} tick={AXIS} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} />
              <Bar dataKey="count" name="Companies" fill="#8b5cf6" radius={[6, 6, 0, 0]} maxBarSize={64} />
            </BarChart>
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
        <p className="breadcrumb">Research Agent / <span>Analytics</span></p>
        <h1>Analytics</h1>
        <p>Trends across your research — tier mix, score spread, and hiring signals.</p>
      </div>
      {label && (
        <div className="page-head-actions">
          <span className="run-badge">{label}</span>
        </div>
      )}
    </div>
  );
}
