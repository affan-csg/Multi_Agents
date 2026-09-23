import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCachedFetch } from "../../hooks/useCachedFetch";
import {
  learningApi,
  type BeforeAfterComparison,
  type CampaignPerformance,
  type Confidence,
  type DraftVersion,
  type LearningInsight,
} from "../../agents/learning";
import {
  optimizationApi,
  type OptimizationInsight,
} from "../../agents/optimization";
import { ChartCardSkeleton, EmptyState, Icon, StatCard, StatCardSkeleton } from "../../components/ui";
import type { IconName } from "../../components/ui";

const AXIS = { stroke: "#8e96a8", fontSize: 12 };
const GRID = "#e8eaee";

const tooltipStyle = {
  background: "#fff",
  border: "1px solid #e8eaee",
  borderRadius: 10,
  boxShadow: "0 4px 14px rgba(20,23,41,.1)",
  fontSize: 13,
};

const CONFIDENCE_COLOR: Record<Confidence, string> = {
  low: "#c9cdd8",
  medium: "#e8a33d",
  high: "#23c08b",
};

type Section =
  | "overview" | "insights" | "human-edits" | "campaigns"
  | "optimization-insights" | "adoptions";

const SECTIONS: Array<{ id: Section; label: string; icon: IconName }> = [
  { id: "overview", label: "Overview", icon: "chart" },
  { id: "insights", label: "What The Agent Has Learned", icon: "brain" },
  { id: "human-edits", label: "Human Edits", icon: "edit" },
  { id: "campaigns", label: "Campaign Comparison", icon: "target" },
  { id: "optimization-insights", label: "Strategy Insights", icon: "lightbulb" },
  { id: "adoptions", label: "Adoption Tracking", icon: "target" },
];

export function Learning() {
  const [section, setSection] = useState<Section>("overview");
  const [selectedInsight, setSelectedInsight] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [selectedOptimizationInsight, setSelectedOptimizationInsight] = useState<string | null>(null);

  return (
    <>
      <Head />

      <div className="tabs" role="tablist" style={{ marginBottom: 20 }}>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={section === s.id}
            className={`tab${section === s.id ? " tab--active" : ""}`}
            onClick={() => setSection(s.id)}
          >
            <Icon name={s.icon} size={15} />
            {s.label}
          </button>
        ))}
      </div>

      {section === "overview" && <OverviewSection onViewMethodology={() => setSection("insights")} />}
      {section === "insights" && (
        <InsightsSection selectedInsight={selectedInsight} onSelectInsight={setSelectedInsight} />
      )}
      {section === "human-edits" && (
        <HumanEditsSection selectedVersion={selectedVersion} onSelectVersion={setSelectedVersion} />
      )}
      {section === "campaigns" && <CampaignComparisonSection />}
      {section === "optimization-insights" && (
        <OptimizationInsightsSection
          selectedInsight={selectedOptimizationInsight}
          onSelectInsight={setSelectedOptimizationInsight}
        />
      )}
      {section === "adoptions" && <AdoptionsSection />}
    </>
  );
}

function Head() {
  return (
    <div className="page-head">
      <div className="page-head-text">
        <p className="breadcrumb">Outreach Agent / <span>Learning & Optimization</span></p>
        <h1>Learning & Optimization</h1>
        <p style={{ maxWidth: 900 }}>
          Patterns observed in AI drafts, human edits, campaigns, and outreach outcomes — evidence-based,
          confidence-gated, and never applied automatically. Feedback Learning (per-edit patterns) and Campaign-Level
          Optimization (campaign/strategy-level patterns) share the same evidence pipeline and are shown together
          below.
        </p>
      </div>
    </div>
  );
}

function MethodologyBanner({ onViewMethodology }: { onViewMethodology: () => void }) {
  return (
    <div className="methodology-banner">
      <span className="methodology-banner-icon">
        <Icon name="shieldCheck" size={17} />
      </span>
      <p>
        <strong>Learning is confidence-gated and suggestions are never applied automatically.</strong>
      </p>
      <button type="button" className="methodology-banner-link" onClick={onViewMethodology}>
        View methodology <Icon name="arrowRight" size={13} />
      </button>
    </div>
  );
}

// --- Overview ------------------------------------------------------------------

const MIN_EVIDENCE_FOR_ANY_INSIGHT = 20;

const OVERVIEW_METRIC_META: Array<{ metric: string; label: string; icon: IconName; tone: "accent" | "mint" | "violet" | "amber" }> = [
  { metric: "reply_rate", label: "Reply rate", icon: "mail", tone: "accent" },
  { metric: "positive_reply_rate", label: "Positive reply rate", icon: "star", tone: "mint" },
  { metric: "booking_rate", label: "Booking rate", icon: "calendar", tone: "violet" },
  { metric: "no_reply_rate", label: "No-reply rate", icon: "ban", tone: "amber" },
  { metric: "failure_rate", label: "Failure rate", icon: "close", tone: "amber" },
];

function OverviewSection({ onViewMethodology }: { onViewMethodology: () => void }) {
  const { data: overview, error: overviewError } = useCachedFetch(
    "learning-overview",
    () => learningApi.overview(),
  );
  const { data: recommendations } = useCachedFetch(
    "learning-recommendations",
    () => learningApi.recommendations(),
  );
  const { data: campaigns } = useCachedFetch(
    "learning-campaign-comparison",
    () => learningApi.campaignComparison(),
  );
  const { data: history } = useCachedFetch("learning-history", () => learningApi.history(50));

  // Rules-of-Hooks requires calling useCachedFetch a fixed number of times,
  // not in a .map() over a runtime array -- so each of these 4 fixed
  // metrics gets its own explicit call rather than being derived from a
  // shared METRICS list.
  const replyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:reply_rate", () => learningApi.beforeAfter("reply_rate", 30));
  const positiveReplyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:positive_reply_rate", () => learningApi.beforeAfter("positive_reply_rate", 30));
  const bookingRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:booking_rate", () => learningApi.beforeAfter("booking_rate", 30));
  const noReplyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:no_reply_rate", () => learningApi.beforeAfter("no_reply_rate", 30));
  const failureRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:failure_rate", () => learningApi.beforeAfter("failure_rate", 30));
  const beforeAfterResults = [
    { metric: "reply_rate", result: replyRate },
    { metric: "positive_reply_rate", result: positiveReplyRate },
    { metric: "booking_rate", result: bookingRate },
    { metric: "no_reply_rate", result: noReplyRate },
    { metric: "failure_rate", result: failureRate },
  ];

  if (overviewError) {
    return <p className="error-text">{String(overviewError)}</p>;
  }

  if (!overview) {
    return (
      <div className="stat-grid">
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
        <StatCardSkeleton />
      </div>
    );
  }

  const readinessPct = Math.min(100, Math.round((overview.total_draft_versions / MIN_EVIDENCE_FOR_ANY_INSIGHT) * 100));

  return (
    <>
      <MethodologyBanner onViewMethodology={onViewMethodology} />

      <div className="stat-grid">
        <StatCard icon="eye" tone="accent" value={overview.total_draft_versions.toLocaleString()} label="Learning observations" />
        <StatCard icon="sparkle" tone="violet" value={overview.active_insight_count.toLocaleString()} label="Active learned patterns" />
        <StatCard icon="edit" tone="mint" value={overview.drafts_analyzed.toLocaleString()} label="Drafts analyzed" />
        <StatCard icon="contact" tone="amber" value={overview.human_edits_analyzed.toLocaleString()} label="Human edits analyzed" />
      </div>

      <div className="overview-split">
        <div className="card">
          <h3 className="card-title">
            <Icon name="analytics" size={16} />
            Performance Improvement
          </h3>
          <p className="card-sub">Previous 30 sent emails vs. the latest 30 — an observed comparison, not a claim of causation.</p>
          <div className="progress-row-list">
            {beforeAfterResults.map(({ metric, result }) => (
              <PerformanceProgressRow
                key={metric}
                meta={OVERVIEW_METRIC_META.find((m) => m.metric === metric)!}
                data={result.data}
              />
            ))}
          </div>
        </div>

        <div className="card readiness-card">
          <h3 className="card-title">
            <Icon name="chart" size={16} />
            Data readiness
          </h3>
          <ReadinessGauge pct={readinessPct} />
          <p className="card-sub readiness-note">
            {readinessPct >= 100
              ? "Enough sent-email evidence exists for the engine to evaluate patterns."
              : "Collect more campaign outcomes to unlock comparisons."}
          </p>
        </div>
      </div>

      <SectionHeading icon="target" title="Campaign overview" subtitle="Reply, positive-reply, booking, and no-reply rate for every campaign with sent outreach." />
      {!campaigns ? (
        <div className="chart-grid">
          <ChartCardSkeleton />
          <ChartCardSkeleton />
          <ChartCardSkeleton />
        </div>
      ) : campaigns.length === 0 ? (
        <div className="card">
          <EmptyState icon="target" message="Not enough data yet." />
        </div>
      ) : (
        <div className="chart-grid">
          {campaigns.map((c) => (
            <CampaignOverviewCard key={c.campaign_id} campaign={c} />
          ))}
        </div>
      )}

      <SectionHeading icon="lightbulb" title="Recommendations" subtitle="Suggestions derived from active insights. Nothing here is applied automatically." />
      {!recommendations ? (
        <ChartCardSkeleton />
      ) : recommendations.length === 0 ? (
        <div className="card">
          <EmptyState icon="lightbulb" message="Not enough data yet." />
        </div>
      ) : (
        <div className="chart-grid">
          {recommendations.map((r) => (
            <div className="card recommendation-card" key={r.pattern_key}>
              <p className="recommendation-text">{r.text}</p>
              <div className="recommendation-meta">
                <span className={`confidence-pill confidence-pill--${r.confidence}`}>{r.confidence} confidence</span>
                <span className="card-sub" style={{ margin: 0 }}>Evidence: {r.evidence_count} emails</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <SectionHeading icon="activity" title="Recent learning activity" subtitle="The latest insight events across both Feedback Learning and Campaign Optimization." />
      <div className="card">
        {!history ? (
          <ChartCardSkeleton />
        ) : history.length === 0 ? (
          <EmptyState icon="sparkle" message="No learning events yet." />
        ) : (
          <ul className="activity-feed">
            {history.slice(0, 6).map((event) => (
              <li key={event.event_id} className="activity-feed-row">
                <span className="activity-feed-icon">
                  <Icon name="sparkle" size={14} />
                </span>
                <div>
                  <div className="activity-feed-title">{formatEventType(event.event_type)}</div>
                  <div className="card-sub" style={{ margin: 0 }}>
                    {event.pattern_key} · {event.created_at ? new Date(event.created_at).toLocaleDateString() : ""}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function ReadinessGauge({ pct }: { pct: number }) {
  return (
    <div className="readiness-gauge" style={{ background: `conic-gradient(var(--accent) ${pct * 3.6}deg, var(--surface-hover) 0deg)` }}>
      <div className="readiness-gauge-inner">
        <span className="readiness-gauge-value">{pct}%</span>
      </div>
    </div>
  );
}

function PerformanceProgressRow({
  meta, data,
}: {
  meta: { metric: string; label: string; icon: IconName; tone: "accent" | "mint" | "violet" | "amber" };
  data: BeforeAfterComparison | null | undefined;
}) {
  const hasData = data && !data.insufficient_data && data.before && data.after;
  const pct = hasData ? Math.min(100, data!.after!.value) : 0;
  return (
    <div className="progress-row">
      <span className={`progress-row-icon stat-icon--${meta.tone}`}>
        <Icon name={meta.icon} size={15} />
      </span>
      <div className="progress-row-body">
        <div className="progress-row-head">
          <span className="progress-row-label">{meta.label}</span>
          <span className="progress-row-value">
            {hasData ? `${data!.after!.value}%` : "Not enough data yet."}
          </span>
        </div>
        <div className="progress-track">
          {hasData && (
            <div className={`progress-fill progress-fill--${meta.tone}`} style={{ width: `${pct}%` }} />
          )}
        </div>
      </div>
    </div>
  );
}

function SectionHeading({ icon, title, subtitle }: { icon: IconName; title: string; subtitle: string }) {
  return (
    <div className="section-heading">
      <h3 className="card-title">
        <Icon name={icon} size={16} />
        {title}
      </h3>
      <p className="card-sub">{subtitle}</p>
    </div>
  );
}

const CAMPAIGN_METRICS: Array<{ key: keyof CampaignPerformance; label: string; tone: "accent" | "mint" | "violet" | "amber" }> = [
  { key: "reply_rate", label: "Reply", tone: "accent" },
  { key: "positive_reply_rate", label: "Positive", tone: "mint" },
  { key: "booking_rate", label: "Booking", tone: "violet" },
  { key: "no_reply_rate", label: "No reply", tone: "amber" },
  { key: "failure_rate", label: "Failed", tone: "amber" },
];

function CampaignOverviewCard({ campaign }: { campaign: CampaignPerformance }) {
  return (
    <div className="card campaign-overview-card">
      <div className="card-title-row">
        <h3 className="card-title">
          <Icon name="send" size={16} />
          {campaign.name || campaign.campaign_id}
        </h3>
        <span className="card-sub" style={{ margin: 0 }}>{campaign.sent.toLocaleString()} sent</span>
      </div>
      <div className="campaign-metric-grid">
        {CAMPAIGN_METRICS.map((m) => (
          <div className={`campaign-metric campaign-metric--${m.tone}`} key={m.key}>
            <span className="campaign-metric-value">{campaign[m.key]}%</span>
            <span className="campaign-metric-label">{m.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- What The Agent Has Learned / Learning History ------------------------------

function InsightsSection({
  selectedInsight, onSelectInsight,
}: {
  selectedInsight: string | null;
  onSelectInsight: (key: string | null) => void;
}) {
  const { data: insights, error } = useCachedFetch(
    "learning-insights:active",
    () => learningApi.insights({ status: "active" }),
  );
  const { data: history } = useCachedFetch("learning-history", () => learningApi.history(50));
  const { data: detail } = useCachedFetch(
    selectedInsight ? `learning-insight-detail:${selectedInsight}` : null,
    () => learningApi.insightDetail(selectedInsight as string),
  );

  if (error) return <p className="error-text">{String(error)}</p>;

  const confidenceCounts = insights ? countByConfidence(insights) : null;

  return (
    <>
      {!insights ? (
        <div className="stat-grid">
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
        </div>
      ) : (
        <div className="stat-grid">
          <StatCard icon="sparkle" tone="accent" value={insights.length.toLocaleString()} label="Active learned patterns" />
          <StatCard icon="shieldCheck" tone="mint" value={(confidenceCounts?.high ?? 0).toLocaleString()} label="High confidence" />
          <StatCard icon="clock" tone="violet" value={(confidenceCounts?.medium ?? 0).toLocaleString()} label="Medium confidence" />
          <StatCard icon="eye" tone="amber" value={(confidenceCounts?.low ?? 0).toLocaleString()} label="Low confidence" />
        </div>
      )}

      {insights && insights.length > 0 && (
        <div className="card chart-wide" style={{ marginBottom: 24 }}>
          <h3 className="card-title">
            <Icon name="analytics" size={16} />
            Confidence distribution
          </h3>
          <p className="card-sub">How many active patterns fall into each confidence tier — more evidence moves a pattern up.</p>
          <ResponsiveContainer width="100%" height={140}>
            <BarChart data={confidenceChartData(confidenceCounts!)} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <YAxis type="category" dataKey="label" tickLine={false} axisLine={false} tick={AXIS} width={90} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => [v, "Patterns"]} />
              <Bar dataKey="count" radius={[0, 6, 6, 0]} maxBarSize={26}>
                {confidenceChartData(confidenceCounts!).map((d) => (
                  <Cell key={d.label} fill={CONFIDENCE_COLOR[d.key]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <SectionHeading icon="brain" title="Learned patterns" subtitle="Evidence-backed patterns detected from human edits and outcomes." />
      <div className="chart-grid">
        {!insights ? (
          <>
            <ChartCardSkeleton />
            <ChartCardSkeleton />
            <ChartCardSkeleton />
          </>
        ) : insights.length === 0 ? (
          <div className="card">
            <EmptyState icon="brain" message="Not enough data yet." />
          </div>
        ) : (
          insights.map((insight) => (
            <InsightCard key={insight.pattern_key} insight={insight} onClick={() => onSelectInsight(insight.pattern_key)} />
          ))
        )}
      </div>

      <SectionHeading icon="clock" title="Learning History" subtitle="Every insight created, updated, or retired, in order." />
      <div className="card">
        {!history ? (
          <ChartCardSkeleton />
        ) : history.length === 0 ? (
          <EmptyState icon="clock" message="Not enough data yet." />
        ) : (
          <ul className="activity-feed">
            {history.map((event) => (
              <li key={event.event_id} className="activity-feed-row">
                <span className="activity-feed-icon">
                  <Icon name="sparkle" size={14} />
                </span>
                <div>
                  <div className="activity-feed-title">{formatEventType(event.event_type)}</div>
                  <div className="card-sub" style={{ margin: 0 }}>
                    {event.pattern_key} · {event.created_at ? new Date(event.created_at).toLocaleDateString() : ""}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {selectedInsight && (
        <InsightDrawer patternKey={selectedInsight} detail={detail ?? null} onClose={() => onSelectInsight(null)} />
      )}
    </>
  );
}

function countByConfidence(insights: Array<{ confidence: Confidence }>): Record<Confidence, number> {
  const counts: Record<Confidence, number> = { low: 0, medium: 0, high: 0 };
  for (const i of insights) counts[i.confidence]++;
  return counts;
}

function confidenceChartData(counts: Record<Confidence, number>): Array<{ key: Confidence; label: string; count: number }> {
  return [
    { key: "high", label: "High", count: counts.high },
    { key: "medium", label: "Medium", count: counts.medium },
    { key: "low", label: "Low", count: counts.low },
  ];
}

function formatEventType(eventType: string): string {
  return eventType.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function InsightCard({ insight, onClick }: { insight: LearningInsight; onClick: () => void }) {
  const groupA = insight.metric_comparison.group_a as { label?: string; reply_rate?: number; n?: number } | undefined;
  const groupB = insight.metric_comparison.group_b as { label?: string; reply_rate?: number; n?: number } | undefined;
  return (
    <button type="button" className="card" onClick={onClick} style={{ textAlign: "left", cursor: "pointer" }}>
      <h3 className="card-title">
        <Icon name="sparkle" size={16} />
        {insight.pattern_label}
      </h3>
      <p className="card-sub">{insight.description}</p>
      <p style={{ margin: "8px 0 0", fontSize: 13 }}>
        Confidence: <span style={{ color: CONFIDENCE_COLOR[insight.confidence], fontWeight: 600 }}>{insight.confidence}</span>
        {" · "}Evidence: {insight.evidence_count} emails
      </p>
      {groupA && groupB && groupA.reply_rate != null && groupB.reply_rate != null && (
        <p className="card-sub" style={{ margin: "4px 0 0" }}>
          Observed: {groupA.label} {groupA.reply_rate}% (n={groupA.n}) vs. {groupB.label} {groupB.reply_rate}% (n={groupB.n})
        </p>
      )}
    </button>
  );
}

function InsightDrawer({
  patternKey, detail, onClose,
}: {
  patternKey: string;
  detail: (LearningInsight & { evidence_sample: DraftVersion[] }) | null;
  onClose: () => void;
}) {
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="drawer-close" onClick={onClose} aria-label="Close">
          <Icon name="close" size={16} />
        </button>
        {!detail ? (
          <ChartCardSkeleton />
        ) : (
          <>
            <h2>{detail.pattern_label}</h2>
            <p className="card-sub">{detail.description}</p>
            <p>
              Confidence: <strong>{detail.confidence}</strong> · Evidence: {detail.evidence_count} emails · Status:{" "}
              {detail.status}
            </p>
            <p className="card-sub">
              First observed: {detail.first_observed_at ? new Date(detail.first_observed_at).toLocaleDateString() : "—"}
              {" · "}
              Last updated: {detail.last_updated_at ? new Date(detail.last_updated_at).toLocaleDateString() : "—"}
            </p>
            <h3 className="card-title" style={{ marginTop: 20 }}>Evidence sample</h3>
            {detail.evidence_sample.length === 0 ? (
              <p className="card-sub">No sample drafts available.</p>
            ) : (
              detail.evidence_sample.slice(0, 5).map((v) => (
                <div key={v.version_id} className="card" style={{ marginBottom: 8 }}>
                  <p className="card-sub" style={{ margin: 0 }}>Item {v.item_id} · {v.source}</p>
                </div>
              ))
            )}
            <p className="card-sub" style={{ marginTop: 16 }}>Pattern key: {patternKey}</p>
          </>
        )}
      </div>
    </div>
  );
}

// --- Human Edits -----------------------------------------------------------------

function HumanEditsSection({
  selectedVersion, onSelectVersion,
}: {
  selectedVersion: string | null;
  onSelectVersion: (id: string | null) => void;
}) {
  const { data: edits, error } = useCachedFetch(
    "learning-human-edits",
    () => learningApi.humanEdits({ limit: 50 }),
  );
  const { data: detail } = useCachedFetch(
    selectedVersion ? `learning-human-edit-detail:${selectedVersion}` : null,
    () => learningApi.humanEditDetail(selectedVersion as string),
  );

  if (error) return <p className="error-text">{String(error)}</p>;
  if (!edits) return <ChartCardSkeleton tall />;
  if (edits.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="edit" message="Not enough data yet." />
      </div>
    );
  }

  const editTypeCounts = countEditTypes(edits);
  const bookedCount = edits.filter((v) => v.outcome_booked).length;
  const positiveCount = edits.filter((v) => v.outcome_positive_reply).length;
  const repliedCount = edits.filter((v) => v.outcome_replied).length;

  return (
    <>
      <div className="stat-grid">
        <StatCard icon="edit" tone="accent" value={edits.length.toLocaleString()} label="Human-edited drafts" />
        <StatCard icon="mail" tone="mint" value={repliedCount.toLocaleString()} label="Went on to get a reply" />
        <StatCard icon="star" tone="violet" value={positiveCount.toLocaleString()} label="Positive replies" />
        <StatCard icon="calendar" tone="amber" value={bookedCount.toLocaleString()} label="Booked" />
      </div>

      <div className="card chart-wide" style={{ marginBottom: 24 }}>
        <h3 className="card-title">
          <Icon name="analytics" size={16} />
          What humans change most
        </h3>
        <p className="card-sub">Frequency of each detected edit type across all human-edited drafts.</p>
        <ResponsiveContainer width="100%" height={Math.max(180, editTypeCounts.length * 34)}>
          <BarChart data={editTypeCounts} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
            <CartesianGrid stroke={GRID} horizontal={false} />
            <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
            <YAxis type="category" dataKey="label" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} width={150} />
            <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => [v, "Drafts"]} />
            <Bar dataKey="count" radius={[0, 6, 6, 0]} maxBarSize={22} fill="#5145e8" />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <SectionHeading icon="edit" title="Edited drafts" subtitle="Every AI draft a human changed before sending, with detected changes and later outcome." />
      <div className="card">
        <table className="data-table">
          <thead>
            <tr>
              <th>Item</th>
              <th>Source</th>
              <th>Detected changes</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {edits.map((v) => (
              <tr key={v.version_id} onClick={() => onSelectVersion(v.version_id)} style={{ cursor: "pointer" }}>
                <td>{v.item_id}</td>
                <td>{v.source}</td>
                <td>{describeChanges(v)}</td>
                <td>{describeOutcome(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedVersion && detail && (
        <div className="drawer-overlay" onClick={() => onSelectVersion(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <button type="button" className="drawer-close" onClick={() => onSelectVersion(null)} aria-label="Close">
              <Icon name="close" size={16} />
            </button>
            <h2>Draft comparison</h2>
            <h3 className="card-title">AI draft</h3>
            <p className="card-sub" style={{ whiteSpace: "pre-wrap" }}>{detail.before_subject}</p>
            <p style={{ whiteSpace: "pre-wrap" }}>{detail.before_body}</p>
            <h3 className="card-title">Human edited version</h3>
            <p className="card-sub" style={{ whiteSpace: "pre-wrap" }}>{detail.after_subject}</p>
            <p style={{ whiteSpace: "pre-wrap" }}>{detail.after_body}</p>
            <h3 className="card-title">Detected changes</h3>
            <p className="card-sub">{describeChanges(detail)}</p>
            <h3 className="card-title">Later outcome</h3>
            <p className="card-sub">{describeOutcome(detail)}</p>
          </div>
        </div>
      )}
    </>
  );
}

function countEditTypes(edits: DraftVersion[]): Array<{ label: string; count: number }> {
  const counters: Array<{ label: string; test: (v: DraftVersion) => boolean }> = [
    { label: "Subject changed", test: (v) => v.subject_changed },
    { label: "CTA shortened", test: (v) => v.cta_shortened },
    { label: "CTA changed", test: (v) => v.cta_changed && !v.cta_shortened },
    { label: "Opening changed", test: (v) => v.opening_changed },
    { label: "Body shortened", test: (v) => v.shortened },
    { label: "Body lengthened", test: (v) => v.lengthened },
    { label: "Personalization added", test: (v) => v.personalization_delta > 0 },
    { label: "Personalization removed", test: (v) => v.personalization_delta < 0 },
    { label: "Formatting changed", test: (v) => v.formatting_changed },
  ];
  return counters
    .map((c) => ({ label: c.label, count: edits.filter(c.test).length }))
    .filter((c) => c.count > 0)
    .sort((a, b) => b.count - a.count);
}

function describeChanges(v: DraftVersion): string {
  const flags: string[] = [];
  if (v.subject_changed) flags.push("Subject changed");
  if (v.cta_shortened) flags.push("CTA shortened");
  else if (v.cta_changed) flags.push("CTA changed");
  if (v.opening_changed) flags.push("Opening changed");
  if (v.shortened) flags.push("Body shortened");
  if (v.lengthened) flags.push("Body lengthened");
  if (v.personalization_delta > 0) flags.push("Personalization added");
  else if (v.personalization_delta < 0) flags.push("Personalization removed");
  if (v.formatting_changed) flags.push("Formatting changed");
  return flags.length ? flags.join(", ") : "No material changes detected";
}

function describeOutcome(v: DraftVersion): string {
  if (v.outcome_booked) return "Booked";
  // Checked ahead of outcome_replied: a rejection is technically a reply
  // (outcome_replied is true for it too, same convention analytics uses),
  // but reporting it as plain "Replied" here would hide the negative
  // outcome from this table entirely.
  if (v.outcome_stage === "not_interested") return "Failed";
  if (v.outcome_positive_reply) return "Positive reply";
  if (v.outcome_replied) return "Replied";
  if (v.outcome_stage) return "No reply yet";
  return "Outcome pending";
}

// --- Campaign Comparison -----------------------------------------------------------

function CampaignComparisonSection() {
  const { data, error } = useCachedFetch("learning-campaign-comparison", () => learningApi.campaignComparison());

  if (error) return <p className="error-text">{String(error)}</p>;
  if (!data) return <ChartCardSkeleton tall />;
  if (data.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="target" message="Not enough data yet." />
      </div>
    );
  }

  const totalSent = data.reduce((sum, c) => sum + c.sent, 0);
  const avg = (key: keyof CampaignPerformance) =>
    Math.round((data.reduce((sum, c) => sum + (c[key] as number), 0) / data.length) * 10) / 10;
  const best = [...data].sort((a, b) => b.reply_rate - a.reply_rate)[0];

  return (
    <>
      <div className="stat-grid">
        <StatCard icon="send" tone="accent" value={data.length.toLocaleString()} label="Campaigns compared" />
        <StatCard icon="mail" tone="mint" value={`${avg("reply_rate")}%`} label="Average reply rate" />
        <StatCard icon="calendar" tone="violet" value={`${avg("booking_rate")}%`} label="Average booking rate" />
        <StatCard icon="target" tone="amber" value={totalSent.toLocaleString()} label="Total emails sent" />
      </div>

      <div className="card chart-wide" style={{ marginBottom: 24 }}>
        <h3 className="card-title">
          <Icon name="target" size={16} />
          Reply rate by campaign
        </h3>
        {best && (
          <p className="card-sub">Best performer so far: <strong>{best.name || best.campaign_id}</strong> at {best.reply_rate}% reply rate.</p>
        )}
        <ResponsiveContainer width="100%" height={Math.max(260, data.length * 40)}>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
            <CartesianGrid stroke={GRID} horizontal={false} />
            <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
            <YAxis
              type="category"
              dataKey="name"
              tickLine={false}
              axisLine={false}
              tick={{ ...AXIS, fontSize: 11 }}
              width={140}
            />
            <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => [`${v}%`, "Reply rate"]} />
            <Bar dataKey="reply_rate" radius={[0, 6, 6, 0]} maxBarSize={28}>
              {data.map((c) => (
                <Cell key={c.campaign_id} fill="#5145e8" />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="card chart-wide" style={{ marginBottom: 24 }}>
        <h3 className="card-title">
          <Icon name="analytics" size={16} />
          All metrics by campaign
        </h3>
        <p className="card-sub">Reply, positive-reply, booking, no-reply, and failure rate side by side for every campaign.</p>
        <ResponsiveContainer width="100%" height={Math.max(260, data.length * 48)}>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
            <CartesianGrid stroke={GRID} horizontal={false} />
            <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
            <YAxis type="category" dataKey="name" tickLine={false} axisLine={false} tick={{ ...AXIS, fontSize: 11 }} width={140} />
            <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v, key) => [`${v}%`, OPTIMIZATION_METRIC_LABEL[key as string] || key]} />
            <Bar dataKey="reply_rate" name="Reply rate" fill="#5145e8" radius={[0, 4, 4, 0]} maxBarSize={12} />
            <Bar dataKey="positive_reply_rate" name="Positive reply" fill="#23c08b" radius={[0, 4, 4, 0]} maxBarSize={12} />
            <Bar dataKey="booking_rate" name="Booking rate" fill="#6f63f2" radius={[0, 4, 4, 0]} maxBarSize={12} />
            <Bar dataKey="no_reply_rate" name="No-reply rate" fill="#e8a33d" radius={[0, 4, 4, 0]} maxBarSize={12} />
            <Bar dataKey="failure_rate" name="Failure rate" fill="#e8734a" radius={[0, 4, 4, 0]} maxBarSize={12} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <SectionHeading icon="send" title="Campaign details" subtitle="Per-campaign breakdown of reply, positive-reply, booking, no-reply, and failure rate." />
      <div className="chart-grid">
        {data.map((c) => (
          <CampaignOverviewCard key={c.campaign_id} campaign={c} />
        ))}
      </div>
    </>
  );
}

// --- Strategy Insights (Campaign-Level Optimization) --------------------------------
// Same evidence-gating pipeline as the Feedback Learning sections above, extended
// from per-edit patterns to campaign/ICP/strategy-level comparisons -- see
// Outreach_Agent/src/outreach/optimization/. A separate, sibling backend package;
// shown here as more tabs on the same page rather than a separate page, since both
// read the same underlying outcome data and serve the same "what should I do
// differently" question.

const OPTIMIZATION_METRIC_LABEL: Record<string, string> = {
  reply_rate: "Reply rate",
  positive_reply_rate: "Positive reply rate",
  booking_rate: "Booking rate",
  no_reply_rate: "No-reply rate",
  failure_rate: "Failure rate",
};

function OptimizationInsightsSection({
  selectedInsight, onSelectInsight,
}: {
  selectedInsight: string | null;
  onSelectInsight: (key: string | null) => void;
}) {
  const { data: insights, error } = useCachedFetch(
    "optimization-insights:active",
    () => optimizationApi.insights({ status: "active" }),
  );
  const { data: recommendations } = useCachedFetch("optimization-recommendations", () => optimizationApi.recommendations());
  const { data: detail } = useCachedFetch(
    selectedInsight ? `optimization-insight-detail:${selectedInsight}` : null,
    () => optimizationApi.insightDetail(selectedInsight as string),
  );

  if (error) return <p className="error-text">{String(error)}</p>;

  const confidenceCounts = insights ? countByConfidence(insights) : null;
  const segmentCount = insights ? new Set(insights.map((i) => i.segment || "all")).size : 0;

  return (
    <>
      {!insights ? (
        <div className="stat-grid">
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
        </div>
      ) : (
        <div className="stat-grid">
          <StatCard icon="lightbulb" tone="accent" value={insights.length.toLocaleString()} label="Active strategy patterns" />
          <StatCard icon="target" tone="mint" value={segmentCount.toLocaleString()} label="Segments covered" />
          <StatCard icon="sparkle" tone="violet" value={(recommendations?.length ?? 0).toLocaleString()} label="Live recommendations" />
          <StatCard icon="shieldCheck" tone="amber" value={(confidenceCounts?.high ?? 0).toLocaleString()} label="High confidence" />
        </div>
      )}

      {insights && insights.length > 0 && (
        <div className="card chart-wide" style={{ marginBottom: 24 }}>
          <h3 className="card-title">
            <Icon name="analytics" size={16} />
            Confidence distribution
          </h3>
          <p className="card-sub">How many active strategy patterns fall into each confidence tier.</p>
          <ResponsiveContainer width="100%" height={140}>
            <BarChart data={confidenceChartData(confidenceCounts!)} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" tickLine={false} axisLine={false} tick={AXIS} allowDecimals={false} />
              <YAxis type="category" dataKey="label" tickLine={false} axisLine={false} tick={AXIS} width={90} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(81,69,232,.06)" }} formatter={(v) => [v, "Patterns"]} />
              <Bar dataKey="count" radius={[0, 6, 6, 0]} maxBarSize={26}>
                {confidenceChartData(confidenceCounts!).map((d) => (
                  <Cell key={d.label} fill={CONFIDENCE_COLOR[d.key]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <SectionHeading
        icon="lightbulb"
        title="Recommendations"
        subtitle="Suggestions derived from active, confidence-gated campaign/strategy insights. Nothing here is applied automatically — use Adoption Tracking to mark a campaign as following one, then check back for its observed outcome."
      />
      {!recommendations ? (
        <ChartCardSkeleton />
      ) : recommendations.length === 0 ? (
        <div className="card">
          <EmptyState icon="lightbulb" message="Not enough data yet." />
        </div>
      ) : (
        <div className="chart-grid" style={{ marginBottom: 28 }}>
          {recommendations.map((r) => (
            <div className="card recommendation-card" key={r.pattern_key}>
              <p className="recommendation-text">{r.text}</p>
              <div className="recommendation-meta">
                <span className={`confidence-pill confidence-pill--${r.confidence}`}>{r.confidence} confidence</span>
                <span className="card-sub" style={{ margin: 0 }}>Evidence: {r.evidence_count} emails</span>
                <span className="card-sub" style={{ margin: 0 }}>{r.segment ? `Segment: ${r.segment}` : "All campaigns"}</span>
              </div>
              <p className="card-sub recommendation-causality">{r.causality_note}</p>
            </div>
          ))}
        </div>
      )}

      <SectionHeading icon="sparkle" title="Strategy Patterns" subtitle="Dimension and segment comparisons behind the recommendations above." />
      <div className="chart-grid">
        {!insights ? (
          <>
            <ChartCardSkeleton />
            <ChartCardSkeleton />
            <ChartCardSkeleton />
          </>
        ) : insights.length === 0 ? (
          <div className="card">
            <EmptyState icon="lightbulb" message="Not enough data yet." />
          </div>
        ) : (
          insights.map((insight) => (
            <OptimizationInsightCard
              key={insight.pattern_key}
              insight={insight}
              onClick={() => onSelectInsight(insight.pattern_key)}
            />
          ))
        )}
      </div>

      {selectedInsight && (
        <OptimizationInsightDrawer patternKey={selectedInsight} detail={detail ?? null} onClose={() => onSelectInsight(null)} />
      )}
    </>
  );
}

function OptimizationInsightCard({ insight, onClick }: { insight: OptimizationInsight; onClick: () => void }) {
  const { group_a: groupA, group_b: groupB, primary_metric: primaryMetric } = insight.metric_comparison;
  return (
    <button type="button" className="card" onClick={onClick} style={{ textAlign: "left", cursor: "pointer" }}>
      <h3 className="card-title">
        <Icon name="lightbulb" size={16} />
        {insight.pattern_label}
      </h3>
      <p className="card-sub">{insight.description}</p>
      <p style={{ margin: "8px 0 0", fontSize: 13 }}>
        Confidence: <span style={{ color: CONFIDENCE_COLOR[insight.confidence], fontWeight: 600 }}>{insight.confidence}</span>
        {" · "}Evidence: {insight.evidence_count} emails
        {" · "}{insight.segment ? `Segment: ${insight.segment}` : "All campaigns"}
      </p>
      {groupA && groupB && (
        <p className="card-sub" style={{ margin: "4px 0 0" }}>
          {OPTIMIZATION_METRIC_LABEL[primaryMetric] || primaryMetric}: {groupA.label} {groupA[primaryMetric]}% (n={groupA.n}) vs.{" "}
          {groupB.label} {groupB[primaryMetric]}% (n={groupB.n})
        </p>
      )}
    </button>
  );
}

function OptimizationInsightDrawer({
  patternKey, detail, onClose,
}: {
  patternKey: string;
  detail: OptimizationInsight | null;
  onClose: () => void;
}) {
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="drawer-close" onClick={onClose} aria-label="Close">
          <Icon name="close" size={16} />
        </button>
        {!detail ? (
          <ChartCardSkeleton />
        ) : (
          <>
            <h2>{detail.pattern_label}</h2>
            <p className="card-sub">{detail.description}</p>
            <p>
              Confidence: <strong>{detail.confidence}</strong> · Evidence: {detail.evidence_count} emails · Status:{" "}
              {detail.status}
            </p>
            <p className="card-sub">
              Segment: {detail.segment || "All campaigns"} · Dimension: {detail.dimension}
            </p>
            <p className="card-sub">
              First observed: {detail.first_observed_at ? new Date(detail.first_observed_at).toLocaleDateString() : "—"}
              {" · "}
              Last updated: {detail.last_updated_at ? new Date(detail.last_updated_at).toLocaleDateString() : "—"}
            </p>

            <h3 className="card-title" style={{ marginTop: 20 }}>Observed rates</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Group</th>
                  <th>n</th>
                  <th>Reply</th>
                  <th>Positive reply</th>
                  <th>Booking</th>
                  <th>No reply</th>
                  <th>Failed</th>
                </tr>
              </thead>
              <tbody>
                {[detail.metric_comparison.group_a, detail.metric_comparison.group_b].map((g) => (
                  <tr key={g.label}>
                    <td>{g.label}</td>
                    <td>{g.n}</td>
                    <td>{g.reply_rate}%</td>
                    <td>{g.positive_reply_rate}%</td>
                    <td>{g.booking_rate}%</td>
                    <td>{g.no_reply_rate}%</td>
                    <td>{g.failure_rate}%</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <p className="card-sub" style={{ marginTop: 12, fontStyle: "italic" }}>
              This is an observed correlation across past campaigns, not a proven cause of better performance.
            </p>

            <h3 className="card-title" style={{ marginTop: 20 }}>Evidence campaigns</h3>
            <p className="card-sub">
              {detail.evidence_campaign_ids.length === 0 ? "—" : detail.evidence_campaign_ids.join(", ")}
            </p>

            <p className="card-sub" style={{ marginTop: 16 }}>Pattern key: {patternKey}</p>
          </>
        )}
      </div>
    </div>
  );
}

// --- Adoption Tracking -------------------------------------------------------------

function AdoptionsSection() {
  const { data: adoptions, error } = useCachedFetch("optimization-adoptions", () => optimizationApi.adoptions());

  if (error) return <p className="error-text">{String(error)}</p>;
  if (!adoptions) return <ChartCardSkeleton tall />;

  const patternKeys = Array.from(new Set(adoptions.map((a) => a.pattern_key)));
  const campaignCount = new Set(adoptions.map((a) => a.campaign_id)).size;

  return (
    <>
      <MethodologyBannerStatic text="Adoptions are recorded from a campaign when a person marks it as built following a recommendation. This never happens automatically." />

      <div className="stat-grid">
        <StatCard icon="check" tone="accent" value={adoptions.length.toLocaleString()} label="Recorded adoptions" />
        <StatCard icon="send" tone="mint" value={campaignCount.toLocaleString()} label="Campaigns following a pattern" />
        <StatCard icon="lightbulb" tone="violet" value={patternKeys.length.toLocaleString()} label="Patterns being tracked" />
      </div>

      <SectionHeading icon="clock" title="Adoption log" subtitle="Every campaign that recorded following a recommendation, and when." />
      {adoptions.length === 0 ? (
        <div className="card">
          <EmptyState icon="target" message="No campaigns have recorded adopting a recommendation yet." />
        </div>
      ) : (
        <div className="card" style={{ marginBottom: 28 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Campaign</th>
                <th>Pattern</th>
                <th>Note</th>
                <th>Recorded</th>
              </tr>
            </thead>
            <tbody>
              {adoptions.map((a) => (
                <tr key={a.adoption_id}>
                  <td>{a.campaign_id}</td>
                  <td>{a.pattern_key}</td>
                  <td>{a.note || "—"}</td>
                  <td>{a.created_at ? new Date(a.created_at).toLocaleDateString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {patternKeys.length > 0 && (
        <SectionHeading icon="analytics" title="Observed outcomes" subtitle="Adopting campaigns vs. all others, per pattern — a correlation, not a proven cause." />
      )}
      <div className="chart-grid">
        {patternKeys.map((key) => (
          <AdoptionOutcomeCard key={key} patternKey={key} />
        ))}
      </div>
    </>
  );
}

function MethodologyBannerStatic({ text }: { text: string }) {
  return (
    <div className="methodology-banner">
      <span className="methodology-banner-icon">
        <Icon name="shieldCheck" size={17} />
      </span>
      <p>
        <strong>{text}</strong>
      </p>
    </div>
  );
}

function AdoptionOutcomeCard({ patternKey }: { patternKey: string }) {
  const { data: outcome } = useCachedFetch(
    `optimization-adoption-outcome:${patternKey}`,
    () => optimizationApi.adoptionOutcome(patternKey),
  );

  if (!outcome) return <ChartCardSkeleton />;
  if (outcome.insufficient_data) {
    return (
      <div className="card">
        <h3 className="card-title">{patternKey}</h3>
        <EmptyState icon="clock" message="Not enough data yet." />
      </div>
    );
  }

  const rows: Array<{ metric: string; label: string; tone: "accent" | "mint" | "violet" | "amber" }> = [
    { metric: "reply_rate", label: "Reply rate", tone: "accent" },
    { metric: "positive_reply_rate", label: "Positive reply", tone: "mint" },
    { metric: "booking_rate", label: "Booking rate", tone: "violet" },
    { metric: "failure_rate", label: "Failure rate", tone: "amber" },
  ];

  return (
    <div className="card">
      <h3 className="card-title">
        <Icon name="target" size={16} />
        {patternKey}
      </h3>
      <p className="card-sub">
        Adopting: n={outcome.adopting?.n} · Comparison: n={outcome.comparison?.n}
      </p>
      <div className="progress-row-list">
        {rows.map((r) => {
          const adoptingVal = (outcome.adopting as Record<string, number> | undefined)?.[r.metric] ?? 0;
          const comparisonVal = (outcome.comparison as Record<string, number> | undefined)?.[r.metric] ?? 0;
          const max = Math.max(adoptingVal, comparisonVal, 1);
          return (
            <div className="progress-row" key={r.metric}>
              <div className="progress-row-body">
                <div className="progress-row-head">
                  <span className="progress-row-label">{r.label}</span>
                  <span className="progress-row-value">{adoptingVal}% vs {comparisonVal}%</span>
                </div>
                <div className="progress-track">
                  <div className={`progress-fill progress-fill--${r.tone}`} style={{ width: `${(adoptingVal / max) * 100}%` }} />
                </div>
                <div className="progress-track" style={{ marginTop: 4, opacity: 0.5 }}>
                  <div className="progress-fill" style={{ width: `${(comparisonVal / max) * 100}%`, background: "var(--text-faint)" }} />
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <p className="card-sub" style={{ marginTop: 12, fontStyle: "italic" }}>{outcome.causality_note}</p>
    </div>
  );
}
