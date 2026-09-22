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
  type Confidence,
  type DraftVersion,
  type LearningInsight,
} from "../../agents/learning";
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

const METRIC_LABEL: Record<string, string> = {
  reply_rate: "Reply rate",
  positive_reply_rate: "Positive reply rate",
  booking_rate: "Booking rate",
  no_reply_rate: "No-reply rate",
};

type Section = "overview" | "insights" | "human-edits" | "campaigns";

const SECTIONS: Array<{ id: Section; label: string; icon: IconName }> = [
  { id: "overview", label: "Overview", icon: "chart" },
  { id: "insights", label: "What The Agent Has Learned", icon: "brain" },
  { id: "human-edits", label: "Human Edits", icon: "edit" },
  { id: "campaigns", label: "Campaign Comparison", icon: "target" },
];

export function Learning() {
  const [section, setSection] = useState<Section>("overview");
  const [selectedInsight, setSelectedInsight] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);

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

      {section === "overview" && <OverviewSection />}
      {section === "insights" && (
        <InsightsSection selectedInsight={selectedInsight} onSelectInsight={setSelectedInsight} />
      )}
      {section === "human-edits" && (
        <HumanEditsSection selectedVersion={selectedVersion} onSelectVersion={setSelectedVersion} />
      )}
      {section === "campaigns" && <CampaignComparisonSection />}
    </>
  );
}

function Head() {
  return (
    <div className="page-head">
      <div className="page-head-text">
        <p className="breadcrumb">Outreach Agent / <span>Learning</span></p>
        <h1>Feedback Learning / Optimization</h1>
        <p>
          Patterns observed in AI drafts, human edits, and outreach outcomes — evidence-based, confidence-gated,
          and never applied automatically.
        </p>
      </div>
    </div>
  );
}

// --- Overview ------------------------------------------------------------------

function OverviewSection() {
  const { data: overview, error: overviewError } = useCachedFetch(
    "learning-overview",
    () => learningApi.overview(),
  );
  const { data: recommendations } = useCachedFetch(
    "learning-recommendations",
    () => learningApi.recommendations(),
  );

  // Rules-of-Hooks requires calling useCachedFetch a fixed number of times,
  // not in a .map() over a runtime array -- so each of these 4 fixed
  // metrics gets its own explicit call rather than being derived from a
  // shared METRICS list.
  const replyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:reply_rate", () => learningApi.beforeAfter("reply_rate", 30));
  const positiveReplyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:positive_reply_rate", () => learningApi.beforeAfter("positive_reply_rate", 30));
  const bookingRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:booking_rate", () => learningApi.beforeAfter("booking_rate", 30));
  const noReplyRate = useCachedFetch<BeforeAfterComparison>("learning-before-after:no_reply_rate", () => learningApi.beforeAfter("no_reply_rate", 30));
  const beforeAfterResults = [
    { metric: "reply_rate", result: replyRate },
    { metric: "positive_reply_rate", result: positiveReplyRate },
    { metric: "booking_rate", result: bookingRate },
    { metric: "no_reply_rate", result: noReplyRate },
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

  return (
    <>
      <div className="stat-grid">
        <StatCard icon="brain" tone="accent" value={overview.total_draft_versions.toLocaleString()} label="Learning observations" />
        <StatCard icon="sparkle" tone="violet" value={overview.active_insight_count.toLocaleString()} label="Active learned patterns" />
        <StatCard icon="edit" tone="mint" value={overview.drafts_analyzed.toLocaleString()} label="Drafts analyzed" />
        <StatCard icon="check" tone="amber" value={overview.human_edits_analyzed.toLocaleString()} label="Human edits analyzed" />
      </div>

      <h3 className="card-title" style={{ marginTop: 28 }}>
        <Icon name="analytics" size={16} />
        Performance Improvement
      </h3>
      <p className="card-sub" style={{ marginBottom: 12 }}>
        Previous 30 sent emails vs. the latest 30 — an observed comparison, not a claim of causation.
      </p>
      <div className="stat-grid">
        {beforeAfterResults.map(({ metric, result }) => (
          <BeforeAfterCard key={metric} metric={metric} data={result.data} loading={!result.data && !result.error} />
        ))}
      </div>

      <h3 className="card-title" style={{ marginTop: 28 }}>
        <Icon name="lightbulb" size={16} />
        Recommendations
      </h3>
      <p className="card-sub" style={{ marginBottom: 12 }}>
        Suggestions derived from active insights. Nothing here is applied automatically.
      </p>
      {!recommendations ? (
        <ChartCardSkeleton />
      ) : recommendations.length === 0 ? (
        <div className="card">
          <EmptyState icon="lightbulb" message="Not enough data yet." />
        </div>
      ) : (
        <div className="chart-grid">
          {recommendations.map((r) => (
            <div className="card" key={r.pattern_key}>
              <p style={{ margin: "0 0 8px" }}>{r.text}</p>
              <p className="card-sub" style={{ margin: 0 }}>
                Evidence: {r.evidence_count} emails · Confidence:{" "}
                <span style={{ color: CONFIDENCE_COLOR[r.confidence] }}>{r.confidence}</span>
              </p>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function BeforeAfterCard({
  metric, data, loading,
}: {
  metric: string;
  data: BeforeAfterComparison | null;
  loading: boolean;
}) {
  if (loading) return <StatCardSkeleton />;
  if (!data || data.insufficient_data || !data.before || !data.after) {
    return (
      <div className="stat-card">
        <span className="stat-icon stat-icon--accent">
          <Icon name="clock" size={19} />
        </span>
        <div>
          <div className="stat-value">Not enough data yet.</div>
          <div className="stat-label">{METRIC_LABEL[metric] || metric}</div>
        </div>
      </div>
    );
  }
  const changeLabel = data.change_pct == null ? "—" : `${data.change_pct > 0 ? "+" : ""}${data.change_pct}%`;
  return (
    <div className="stat-card">
      <span className="stat-icon stat-icon--mint">
        <Icon name="analytics" size={19} />
      </span>
      <div>
        <div className="stat-value">{data.after.value}%</div>
        <div className="stat-label">
          {METRIC_LABEL[metric] || metric} · was {data.before.value}% · {changeLabel}
        </div>
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

  return (
    <>
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

      <h3 className="card-title" style={{ marginTop: 28 }}>
        <Icon name="clock" size={16} />
        Learning History
      </h3>
      <div className="card">
        {!history ? (
          <ChartCardSkeleton />
        ) : history.length === 0 ? (
          <EmptyState icon="clock" message="Not enough data yet." />
        ) : (
          <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
            {history.map((event) => (
              <li key={event.event_id} style={{ padding: "10px 0", borderBottom: "1px solid #e8eaee" }}>
                <strong>{formatEventType(event.event_type)}</strong>
                <span className="card-sub" style={{ marginLeft: 8 }}>
                  {event.pattern_key} · {event.created_at ? new Date(event.created_at).toLocaleDateString() : ""}
                </span>
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

  return (
    <>
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

  return (
    <div className="card chart-wide">
      <h3 className="card-title">
        <Icon name="target" size={16} />
        Reply rate by campaign
      </h3>
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
  );
}
