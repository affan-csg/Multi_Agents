/** Typed client for the Feedback Learning / Optimization Agent's API
 *  (Outreach_Agent/src/outreach/learning/routes.py). Same backend as
 *  outreach.ts (this agent is mounted inside the Outreach Agent's own
 *  process), so it reuses that module's base URL rather than re-deriving
 *  one from the registry. */

import { outreachApi } from "./outreach";

const BASE = outreachApi.baseUrl;

export type Confidence = "low" | "medium" | "high";
export type InsightStatus = "active" | "insufficient_data" | "retired";
export type InsightCategory =
  | "cta" | "length" | "personalization" | "opening" | "subject" | "followup" | "human_edit_pattern";

export interface LearningOverview {
  total_draft_versions: number;
  active_insight_count: number;
  drafts_analyzed: number;
  human_edits_analyzed: number;
  confidence_counts: Record<Confidence, number>;
}

export interface MetricGroup {
  label: string;
  reply_rate?: number;
  n: number;
}

export interface LearningInsight {
  pattern_key: string;
  pattern_label: string;
  category: InsightCategory;
  description: string;
  evidence_count: number;
  metric_comparison: Record<string, unknown>;
  confidence: Confidence;
  status: InsightStatus;
  evidence_campaign_ids: string[];
  evidence_item_ids: string[];
  first_observed_at: string | null;
  last_updated_at: string | null;
}

export interface LearningInsightDetail extends LearningInsight {
  evidence_sample: DraftVersion[];
}

export interface LearningEvent {
  event_id: string;
  pattern_key: string;
  event_type: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

export interface DraftVersion {
  version_id: string;
  item_id: string;
  campaign_id: string;
  source: "revise" | "redraft" | "redraft_generic" | "conversation_revise" | "conversation_redraft";
  message_id: string | null;
  before_subject: string | null;
  before_body: string | null;
  after_subject: string | null;
  after_body: string | null;
  instruction: string | null;
  subject_changed: boolean;
  opening_changed: boolean;
  cta_changed: boolean;
  cta_shortened: boolean;
  length_delta_chars: number | null;
  length_delta_words: number | null;
  shortened: boolean;
  lengthened: boolean;
  sentences_removed_count: number;
  personalization_delta: number;
  formatting_changed: boolean;
  outcome_stage: string | null;
  outcome_replied: boolean | null;
  outcome_positive_reply: boolean | null;
  outcome_booked: boolean | null;
  created_at: string | null;
}

export interface BeforeAfterComparison {
  metric: string;
  before: { value: number; n: number } | null;
  after: { value: number; n: number } | null;
  change_pct: number | null;
  insufficient_data: boolean;
}

export interface CampaignPerformance {
  campaign_id: string;
  name: string | null;
  sent: number;
  reply_rate: number;
  positive_reply_rate: number;
  booking_rate: number;
  no_reply_rate: number;
}

export interface FollowupBucket {
  followup_count: string;
  n: number;
  reply_rate: number;
}

export interface Recommendation {
  pattern_key: string;
  text: string;
  evidence_count: number;
  confidence: Confidence;
}

function qs(params?: Record<string, string | number | undefined | null>): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (!entries.length) return "";
  return `?${entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")}`;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

export const learningApi = {
  overview: () => req<LearningOverview>("/learning/overview"),

  insights: (params?: { status?: string; category?: string }) =>
    req<LearningInsight[]>(`/learning/insights${qs(params)}`),

  insightDetail: (patternKey: string) =>
    req<LearningInsightDetail>(`/learning/insights/${encodeURIComponent(patternKey)}`),

  history: (limit = 50) => req<LearningEvent[]>(`/learning/history${qs({ limit })}`),

  humanEdits: (params?: { campaign_id?: string; limit?: number; offset?: number }) =>
    req<DraftVersion[]>(`/learning/human-edits${qs(params)}`),

  humanEditDetail: (versionId: string) =>
    req<DraftVersion>(`/learning/human-edits/${encodeURIComponent(versionId)}`),

  beforeAfter: (metric: string, window = 30) =>
    req<BeforeAfterComparison>(`/learning/performance/before-after${qs({ metric, window })}`),

  campaignComparison: () => req<CampaignPerformance[]>("/learning/performance/campaigns"),

  followupPerformance: () =>
    req<{ buckets: FollowupBucket[]; insufficient_data: boolean }>("/learning/performance/followups"),

  recommendations: () => req<Recommendation[]>("/learning/recommendations"),
};
