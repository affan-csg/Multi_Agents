import { CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { researchApi, type Analytics as ResearchAnalytics, type RunSummary, type ScoredCompany } from "../agents/research";
import { outreachApi, type Campaign, type ImportedProspect, type OutreachAnalytics } from "../agents/outreach";
import { EmptyState, Icon, StatCard } from "../components/ui";
import type { Route } from "../router";
import { useCachedFetch } from "../hooks/useCachedFetch";

interface Props {
  onNavigate: (r: Route) => void;
}

const AXIS = { stroke: "#8e96a8", fontSize: 12 };
const GRID = "#e8eaee";
const tooltipStyle = {
  background: "#fff",
  border: "1px solid #e8eaee",
  borderRadius: 10,
  boxShadow: "0 4px 14px rgba(20,23,41,.1)",
  fontSize: 13,
};

const DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Builds the last 7 UTC calendar days (oldest first) as { key: "YYYY-MM-DD", label: "Mon" }.
 * Server timestamps (scored_at etc.) are UTC ISO strings, so buckets are
 * computed in UTC too -- bucketing by local midnight would shift "today"
 * into the wrong bucket (or drop it entirely) for any non-UTC timezone. */
function last7Days(): { key: string; label: string }[] {
  const days = [];
  const now = new Date();
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    days.push({ key: d.toISOString().slice(0, 10), label: DAY_LABELS[d.getUTCDay()] });
  }
  return days;
}

export function Home({ onNavigate }: Props) {
  const { data: runs } = useCachedFetch<RunSummary[]>("runs", () => researchApi.listRuns());
  const { data: campaigns } = useCachedFetch<Campaign[]>("campaigns", () => outreachApi.listCampaigns());
  // Aggregate endpoints -- cheap, server-computed totals -- instead of
  // pulling every company/item row just to count them client-side.
  const { data: researchAnalytics } = useCachedFetch<ResearchAnalytics>(
    "research-analytics:all",
    () => researchApi.analytics("all"),
  );
  const { data: prospects } = useCachedFetch<ImportedProspect[]>("prospects:all", () => outreachApi.listProspects());
  // Only the 7-day trend chart actually needs per-row scored_at timestamps --
  // scoped separately so it never blocks the stat tiles/flow steps above.
  const { data: allCompanies } = useCachedFetch<ScoredCompany[]>("companies:all", () => researchApi.runCompanies("all"));

  const latestRun = runs?.[0];
  const latestCampaign = campaigns?.[0];
  const latestCampaignId = latestCampaign?.campaign_id ?? null;
  // Stat tiles/pie below are scoped to the latest campaign, same aggregate
  // endpoint the campaign's own Analytics page uses -- no need to pull every item row.
  const { data: latestCampaignAnalytics } = useCachedFetch<OutreachAnalytics>(
    latestCampaignId ? `outreach-analytics:${latestCampaignId}` : null,
    () => outreachApi.analytics(latestCampaignId!),
  );
  const { data: latestItemCount } = useCachedFetch<number>(
    latestCampaignId ? `campaign-item-count:${latestCampaignId}` : null,
    () => outreachApi.campaignItems(latestCampaignId!).then((items) => items.length),
  );

  const companies = allCompanies ?? [];
  const days = last7Days();
  const researchActivity = days.map((d) => {
    const found = companies.filter((c) => c.scored_at?.slice(0, 10) === d.key).length;
    const researched = companies.filter((c) => c.scored_at?.slice(0, 10) === d.key && c.dimensions && Object.keys(c.dimensions).length > 0).length;
    return { day: d.label, found, researched };
  });
  const researchedTotal = researchAnalytics?.total ?? companies.length;
  const tierACount = researchAnalytics?.tiers.find((t) => t.tier === "A")?.count ?? 0;

  const qualifiedCount = (prospects ?? []).length;
  const personalizedCount = latestCampaignAnalytics?.personalized_from
    .filter((s) => s.source !== "not_drafted")
    .reduce((sum, s) => sum + s.count, 0) ?? 0;
  const pendingCount = latestCampaignAnalytics?.stage_funnel.find((s) => s.stage === "pending")?.count ?? 0;
  const sentCount = latestCampaignAnalytics?.sent_total ?? 0;

  const outreachPie = [
    { key: "qualified", name: "Qualified", value: qualifiedCount, color: "#5145e8" },
    { key: "personalized", name: "Personalized", value: personalizedCount, color: "#b3aef7" },
    { key: "pending", name: "Pending", value: pendingCount, color: "#e8a33d" },
    { key: "sent", name: "Sent", value: sentCount, color: "#23c08b" },
  ].filter((d) => d.value > 0);

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <h1>Overview</h1>
          <p>One dashboard across every agent — each keeps its own database; this page only reads their APIs.</p>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard icon="play" tone="accent" value={runs === null ? "…" : runs.length.toLocaleString()} label="Research runs" />
        <StatCard icon="send" tone="mint" value={campaigns === null ? "…" : campaigns.length.toLocaleString()} label="Outreach campaigns" />
        <StatCard icon="clock" tone="amber" value={pendingCount.toLocaleString()} label="Pending sends" />
        <StatCard icon="check" tone="violet" value={sentCount.toLocaleString()} label="Sent (latest campaign)" />
      </div>

      <div className="chart-grid">
        <div className="card">
          <h3 className="card-title">
            <Icon name="flask" size={16} />
            Research Agent
          </h3>
          <p className="card-sub">Finds and ranks companies as staffing prospects.</p>

          {runs === null ? (
            <p className="hint">Checking…</p>
          ) : runs.length === 0 ? (
            <EmptyState icon="play" message="No research runs yet." />
          ) : (
            <div className="summary-filters">
              <div className="summary-filter-row">
                <div>
                  <div className="summary-filter-label">Latest run</div>
                  <div className="summary-filter-value">{latestRun?.label || latestRun?.run_id}</div>
                </div>
              </div>
            </div>
          )}

          <div className="action-row" style={{ marginTop: 16 }}>
            <button type="button" className="primary" onClick={() => onNavigate({ name: "research-companies" })}>
              View companies
              <Icon name="arrowRight" size={14} />
            </button>
            <button type="button" onClick={() => onNavigate({ name: "research-runs" })}>
              Runs
            </button>
          </div>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="send" size={16} />
            Outreach Agent
          </h3>
          <p className="card-sub">Personalizes and sends email to qualified prospects; job stops at Sent.</p>

          {campaigns === null ? (
            <p className="hint">Checking…</p>
          ) : campaigns.length === 0 ? (
            <EmptyState icon="send" message="No campaigns yet." />
          ) : (
            <div className="summary-filters">
              <div className="summary-filter-row">
                <div>
                  <div className="summary-filter-label">Latest campaign</div>
                  <div className="summary-filter-value">{latestCampaign?.name || latestCampaign?.campaign_id}</div>
                </div>
                <span className="summary-count">{latestItemCount ?? 0}</span>
              </div>
            </div>
          )}

          <div className="action-row" style={{ marginTop: 16 }}>
            <button
              type="button"
              className="mint"
              onClick={() => (latestCampaign ? onNavigate({ name: "outreach-campaign", campaignId: latestCampaign.campaign_id, campaignName: latestCampaign.name }) : onNavigate({ name: "outreach-campaigns" }))}
            >
              {latestCampaign ? "Open pipeline board" : "Create a campaign"}
              <Icon name="arrowRight" size={14} />
            </button>
            <button type="button" onClick={() => onNavigate({ name: "outreach-prospects" })}>
              Prospects
            </button>
          </div>
        </div>

        <div className="card">
          <h3 className="card-title" style={{ justifyContent: "space-between", display: "flex" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
              <Icon name="analytics" size={16} />
              Research activity
            </span>
            <span className="summary-count">{researchedTotal} researched</span>
          </h3>
          <p className="card-sub">Companies found and researched over the last 7 days.</p>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={researchActivity} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid stroke={GRID} vertical={false} />
              <XAxis dataKey="day" tickLine={false} axisLine={false} tick={AXIS} />
              <YAxis tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Line type="monotone" dataKey="found" name="Companies found" stroke="#0f9d6d" strokeWidth={2} dot={{ r: 3 }} />
              <Line type="monotone" dataKey="researched" name="Researched" stroke="#7fe3c4" strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3 className="card-title" style={{ justifyContent: "space-between", display: "flex" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
              <Icon name="target" size={16} />
              Outreach activity
            </span>
            <span className="summary-count">{pendingCount} pending</span>
          </h3>
          <p className="card-sub">Qualified prospects moving through personalization and send.</p>
          {outreachPie.length === 0 ? (
            <EmptyState icon="target" message="No prospects yet." />
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={outreachPie} dataKey="value" nameKey="name" innerRadius={54} outerRadius={82} paddingAngle={2}>
                  {outreachPie.map((d) => (
                    <Cell key={d.key} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 11.5 }} verticalAlign="bottom" />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="flask" size={16} />
            Research flow
          </h3>
          <p className="card-sub">Discover, research, and rank.</p>
          <div className="flow-row">
            <div className="flow-step flow-step--mint">
              <span className="flow-step-icon flow-step-icon--mint">
                <Icon name="pin" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Region input</div>
                <div className="flow-step-value">{runs?.[0]?.label?.split(/[\s,-]/)[0] ?? "—"}</div>
              </div>
            </div>
            <div className="flow-step flow-step--mint">
              <span className="flow-step-icon flow-step-icon--mint">
                <Icon name="building" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Companies found</div>
                <div className="flow-step-value">{researchedTotal} companies</div>
              </div>
            </div>
            <div className="flow-step flow-step--mint">
              <span className="flow-step-icon flow-step-icon--mint">
                <Icon name="users" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Ranked prospects</div>
                <div className="flow-step-value">Tier A {tierACount}</div>
              </div>
            </div>
          </div>
        </div>

        <div className="card">
          <h3 className="card-title">
            <Icon name="send" size={16} />
            Outreach flow
          </h3>
          <p className="card-sub">Personalize and send to qualified prospects.</p>
          <div className="flow-row">
            <div className="flow-step flow-step--violet">
              <span className="flow-step-icon flow-step-icon--violet">
                <Icon name="users" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Qualified prospects</div>
                <div className="flow-step-value">{qualifiedCount} prospects</div>
              </div>
            </div>
            <div className="flow-step flow-step--violet">
              <span className="flow-step-icon flow-step-icon--violet">
                <Icon name="mail" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Personalized emails</div>
                <div className="flow-step-value">{pendingCount} pending</div>
              </div>
            </div>
            <div className="flow-step flow-step--violet">
              <span className="flow-step-icon flow-step-icon--violet">
                <Icon name="check" size={17} />
              </span>
              <div className="flow-step-text">
                <div className="flow-step-label">Sent</div>
                <div className="flow-step-value">{sentCount} sent</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
