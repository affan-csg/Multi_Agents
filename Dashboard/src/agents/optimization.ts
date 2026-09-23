/** Typed client for the Campaign-Level Optimization layer's API
 *  (Outreach_Agent/src/outreach/optimization/routes.py). Same backend as
 *  outreach.ts and learning.ts (this layer is mounted inside the Outreach
 *  Agent's own process), so it reuses that module's base URL rather than
 *  re-deriving one from the registry. Independent of learning.ts -- this
 *  layer extends the Feedback Learning Agent's evidence-gating approach up
 *  to campaign/strategy-level comparisons, it does not replace it. */

import { outreachApi } from "./outreach";

const BASE = outreachApi.baseUrl;

export type Confidence = "low" | "medium" | "high";
export type InsightStatus = "active" | "insufficient_data" | "retired";
export type OptimizationDimension =
  | "email_style" | "subject_style" | "opening_style" | "cta_style"
  | "length_bucket" | "personalization_level" | "followup_count" | "followup_timing";

export interface OptimizationOverview {
  total_insight_count: number;
  active_insight_count: number;
  segmented_insight_count: number;
  confidence_counts: Record<Confidence, number>;
}

export interface MetricGroup {
  label: string;
  n: number;
  reply_rate: number;
  positive_reply_rate: number;
  booking_rate: number;
  no_reply_rate: number;
  /** Share of this group that ended in a clear rejection (the terminal
   *  not_interested stage, "Failed" in the UI) -- a negative campaign
   *  outcome, reported alongside the other rates but never used to pick
   *  `primary_metric` (see Outreach_Agent's optimization/engine.py). */
  failure_rate: number;
}

export interface MetricComparison {
  group_a: MetricGroup;
  group_b: MetricGroup;
  primary_metric: "booking_rate" | "positive_reply_rate" | "reply_rate";
  effect_pct_points: number;
}

export interface OptimizationInsight {
  pattern_key: string;
  pattern_label: string;
  dimension: OptimizationDimension;
  segment: string;
  description: string;
  evidence_count: number;
  metric_comparison: MetricComparison;
  confidence: Confidence;
  status: InsightStatus;
  evidence_campaign_ids: string[];
  evidence_item_ids: string[];
  first_observed_at: string | null;
  last_updated_at: string | null;
}

export interface Recommendation {
  pattern_key: string;
  text: string;
  dimension: OptimizationDimension;
  segment: string;
  evidence_count: number;
  confidence: Confidence;
  metric_comparison: MetricComparison;
  evidence_campaign_ids: string[];
  causality_note: string;
}

export interface RecommendationAdoption {
  adoption_id: string;
  pattern_key: string;
  campaign_id: string;
  note: string | null;
  created_at: string | null;
}

export interface AdoptionOutcome {
  pattern_key: string;
  insufficient_data: boolean;
  reason?: string;
  adopting_campaign_count?: number;
  adopting_campaign_ids?: string[];
  adopting?: { n: number; reply_rate: number; positive_reply_rate: number; booking_rate: number; no_reply_rate: number; failure_rate: number };
  comparison?: { n: number; reply_rate: number; positive_reply_rate: number; booking_rate: number; no_reply_rate: number; failure_rate: number };
  causality_note?: string;
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

export const optimizationApi = {
  overview: () => req<OptimizationOverview>("/optimization/overview"),

  insights: (params?: { status?: string; dimension?: string; segment?: string }) =>
    req<OptimizationInsight[]>(`/optimization/insights${qs(params)}`),

  insightDetail: (patternKey: string) =>
    req<OptimizationInsight>(`/optimization/insights/${encodeURIComponent(patternKey)}`),

  recommendations: (segment?: string) =>
    req<Recommendation[]>(`/optimization/recommendations${qs({ segment })}`),

  recordAdoption: (body: { pattern_key: string; campaign_id: string; note?: string }) =>
    req<RecommendationAdoption>("/optimization/adoptions", { method: "POST", body: JSON.stringify(body) }),

  adoptions: (params?: { pattern_key?: string; campaign_id?: string }) =>
    req<RecommendationAdoption[]>(`/optimization/adoptions${qs(params)}`),

  adoptionOutcome: (patternKey: string) =>
    req<AdoptionOutcome>(`/optimization/adoptions/${encodeURIComponent(patternKey)}/outcome`),
};
