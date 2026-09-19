import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { researchApi, type ProviderUsage, type RunSummary, type UsageSummary } from "../../agents/research";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import { ChartCardSkeleton, EmptyState, Icon, StatCard, formatDate } from "../../components/ui";

// Same fixed categorical order as the rest of the app's charts (Analytics.tsx's
// TIER_COLORS, Settings.tsx's WEIGHT_COLORS) -- one color per provider, never
// reassigned by filtering, so "Gemini" always reads as indigo everywhere.
// #2d9cdb (Hunter) is the same blue already used for the Workable ATS badge
// elsewhere in the app (components/ui.tsx) -- reused here rather than a new
// hue, and the 4-color set passes the CVD/contrast validator together.
const PROVIDER_COLOR: Record<string, string> = {
  gemini: "#5145e8",
  groq: "#23c08b",
  tavily: "#8b5cf6",
  hunter: "#2d9cdb",
};

const PROVIDER_LABEL: Record<string, string> = {
  gemini: "Gemini",
  groq: "Groq",
  tavily: "Tavily",
  hunter: "Hunter.io",
};

const PROVIDER_ROLE: Record<string, string> = {
  gemini: "Firmographic research + discovery search (grounded search, token-metered)",
  groq: "Fallback JSON parsing when Gemini is exhausted (token-metered)",
  tavily: "Fallback web search when Gemini is exhausted (search-metered, no tokens)",
  hunter: "Contact discovery — Domain Search API (search-metered, hard 25/month on free tier)",
};

const PROVIDER_TONE: Record<string, "accent" | "mint" | "violet" | "amber"> = {
  gemini: "accent",
  groq: "mint",
  tavily: "violet",
  hunter: "amber",
};

const PROVIDERS = ["gemini", "groq", "tavily", "hunter"] as const;
// Gemini/Groq are token-metered; Tavily/Hunter are pure search/lookup APIs
// with no token concept -- hide the "Total tokens" fact for those instead of
// always showing a confusing "0".
const TOKEN_METERED = new Set(["gemini", "groq"]);

const AXIS = { stroke: "#8e96a8", fontSize: 12 };
const GRID = "#e8eaee";
const tooltipStyle = {
  background: "#fff",
  border: "1px solid #e8eaee",
  borderRadius: 10,
  boxShadow: "0 4px 14px rgba(20,23,41,.1)",
  fontSize: 13,
};

function successRate(p: ProviderUsage): number | null {
  return p.requests > 0 ? (p.successes / p.requests) * 100 : null;
}

type UsageWindow = "latest_run" | "7d" | "30d" | "all";

const WINDOW_OPTIONS: { key: UsageWindow; label: string }[] = [
  { key: "latest_run", label: "Latest Research Run" },
  { key: "7d", label: "Last 7 Days" },
  { key: "30d", label: "Last 30 Days" },
  { key: "all", label: "All Time" },
];

export function UsageTab() {
  const [windowFilter, setWindowFilter] = useState<UsageWindow>("all");

  // Same cache key Settings.tsx's own research-runs list uses for this exact
  // researchApi.listRuns() call -- reusing it means "Latest Research Run"
  // here never triggers a second network round trip for data the page next
  // to it already fetched.
  const { data: runsData } = useCachedFetch<RunSummary[]>("research-runs", () => researchApi.listRuns());

  // The most recently *started* run, not just the first array element --
  // listRuns()'s own ordering isn't a contract this page should depend on.
  const latestRun = useMemo(() => {
    const runs = runsData ?? [];
    if (runs.length === 0) return null;
    return runs.reduce((a, b) => (a.started_at > b.started_at ? a : b));
  }, [runsData]);

  const usageParams = useMemo(() => {
    if (windowFilter === "latest_run") return latestRun ? { since: latestRun.started_at } : {};
    if (windowFilter === "7d") return { hours: 24 * 7 };
    if (windowFilter === "30d") return { hours: 24 * 30 };
    return {}; // "all"
  }, [windowFilter, latestRun]);

  // Key includes the resolved params (not just windowFilter) so switching to
  // "Latest Research Run" before listRuns() has loaded, then having it load
  // a moment later, is treated as a genuinely different request rather than
  // silently reusing an "all time" response cached under the same key.
  const usageKey = `settings-usage:${windowFilter}:${usageParams.since ?? ""}:${usageParams.hours ?? ""}`;
  const { data, loading } = useCachedFetch<UsageSummary>(usageKey, () => researchApi.getUsage(usageParams));

  const filterBar = (
    <div className="filter-bar" style={{ marginBottom: 16 }}>
      <div className="filter-bar-row">
        <Icon name="filter" size={14} />
        <div className="chip-row">
          {WINDOW_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              type="button"
              className={`chip${windowFilter === opt.key ? " chip-active" : ""}`}
              onClick={() => setWindowFilter(opt.key)}
              disabled={opt.key === "latest_run" && !latestRun}
              title={opt.key === "latest_run" && !latestRun ? "No research runs yet" : undefined}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );

  if (loading && !data) {
    return (
      <>
        {filterBar}
        <div className="chart-grid">
          <ChartCardSkeleton />
          <ChartCardSkeleton />
          <ChartCardSkeleton />
          <ChartCardSkeleton tall />
        </div>
      </>
    );
  }

  const providers = data?.providers;
  const totalRequests = providers
    ? PROVIDERS.reduce((sum, key) => sum + providers[key].requests, 0)
    : 0;

  if (!providers || totalRequests === 0) {
    return (
      <>
        {filterBar}
        <div className="card">
          <EmptyState
            icon="database"
            message={
              windowFilter === "all"
                ? "No API usage recorded yet — run a research, qualify, or contacts job to start tracking Gemini, Groq, Tavily, and Hunter.io usage."
                : "No API usage recorded in this window — try a wider filter."
            }
          />
        </div>
      </>
    );
  }

  // Merge each provider's daily buckets into one series per date for the
  // stacked-area "requests over time" chart -- Recharts wants one row per
  // x-value with a key per series, not one array per provider.
  const byDate = new Map<string, Record<string, number>>();
  for (const key of PROVIDERS) {
    for (const bucket of providers[key].daily) {
      const row = byDate.get(bucket.date) ?? { date: bucket.date } as unknown as Record<string, number>;
      row[key] = bucket.requests;
      byDate.set(bucket.date, row);
    }
  }
  const timeSeries = Array.from(byDate.values()).sort((a, b) =>
    String(a.date).localeCompare(String(b.date)),
  );

  const windowDescription =
    windowFilter === "latest_run" && latestRun
      ? `since the latest research run started (${formatDate(latestRun.started_at)})`
      : windowFilter === "7d"
      ? "over the last 7 days"
      : windowFilter === "30d"
      ? "over the last 30 days"
      : "across all recorded history";

  return (
    <>
      {filterBar}
      <p className="card-sub" style={{ marginTop: -8, marginBottom: 20 }}>
        Requests, tokens, retries, and 429s for every Gemini, Groq, Tavily, and Hunter.io call made
        during research (discovery, enrichment, qualify) and contact finding, {windowDescription}.
        Recorded best-effort — a logging failure never affects an actual research run.
      </p>

      <div className="stat-grid" style={{ marginBottom: 20 }}>
        {PROVIDERS.map((key) => {
          const p = providers[key];
          const rate = successRate(p);
          return (
            <StatCard
              key={key}
              icon="database"
              tone={PROVIDER_TONE[key]}
              value={p.requests.toLocaleString()}
              label={
                <>
                  {PROVIDER_LABEL[key]} requests
                  {rate !== null && (
                    <span style={{ marginLeft: 6, color: rate >= 95 ? "#23c08b" : rate >= 80 ? "#f0ac3f" : "#e0563f" }}>
                      {rate.toFixed(0)}% ok
                    </span>
                  )}
                </>
              }
            />
          );
        })}
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <h3 className="card-title">
          <Icon name="chart" size={16} />
          Requests over time
        </h3>
        <p className="card-sub">Daily request volume per provider.</p>
        {timeSeries.length === 0 ? (
          <EmptyState icon="chart" message="Not enough history yet to chart." />
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={timeSeries} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend wrapperStyle={{ fontSize: 12 }} formatter={(v) => PROVIDER_LABEL[v as string] || v} />
              <Area type="monotone" dataKey="gemini" stackId="1" stroke={PROVIDER_COLOR.gemini} fill={PROVIDER_COLOR.gemini} fillOpacity={0.18} />
              <Area type="monotone" dataKey="groq" stackId="1" stroke={PROVIDER_COLOR.groq} fill={PROVIDER_COLOR.groq} fillOpacity={0.18} />
              <Area type="monotone" dataKey="tavily" stackId="1" stroke={PROVIDER_COLOR.tavily} fill={PROVIDER_COLOR.tavily} fillOpacity={0.18} />
              <Area type="monotone" dataKey="hunter" stackId="1" stroke={PROVIDER_COLOR.hunter} fill={PROVIDER_COLOR.hunter} fillOpacity={0.18} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="settings-grid">
        {PROVIDERS.map((key) => {
          const p = providers[key];
          return (
            <div className="card" key={key}>
              <h3 className="card-title">
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: PROVIDER_COLOR[key], display: "inline-block" }} />
                {PROVIDER_LABEL[key]}
              </h3>
              <p className="card-sub">{PROVIDER_ROLE[key]}</p>

              <div className="drawer-fact-grid" style={{ marginTop: 4 }}>
                <div>
                  <span className="drawer-fact-label">Requests</span>
                  <span className="drawer-fact-value">{p.requests.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Successes</span>
                  <span className="drawer-fact-value">{p.successes.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Failures</span>
                  <span className="drawer-fact-value">{p.failures.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Retries</span>
                  <span className="drawer-fact-value">{p.retries.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Rate-limited (429)</span>
                  <span className="drawer-fact-value">{p.rate_limited_429.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Quota exhausted</span>
                  <span className="drawer-fact-value">{p.quota_exhausted.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Connection errors</span>
                  <span className="drawer-fact-value">{p.connection_errors.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Parse errors</span>
                  <span className="drawer-fact-value">{p.parse_errors.toLocaleString()}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Avg latency</span>
                  <span className="drawer-fact-value">{p.avg_latency_ms != null ? `${p.avg_latency_ms.toLocaleString()} ms` : "—"}</span>
                </div>
                {TOKEN_METERED.has(key) && (
                  <div>
                    <span className="drawer-fact-label">Total tokens</span>
                    <span className="drawer-fact-value">{p.total_tokens.toLocaleString()}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
