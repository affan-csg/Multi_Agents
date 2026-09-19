/** Typed client for the Outreach Agent's API (Outreach_Agent/src/outreach/api.py).
 *  Same conventions as research.ts: one module per agent, talking only to
 *  that agent's own HTTP API. */

import { agentBaseUrl } from "./registry";

const BASE = agentBaseUrl("outreach");

export type Stage = "needs_review" | "pending" | "sent" | "waiting" | "replied" | "booked" | "failed";

// needs_review sits before pending: an item lands there instead of pending
// when it was drafted from the generic template_only fallback (no
// meaningful research to personalize from) -- see Outreach_Agent's
// ingest.create_campaign_from_prospects and api.approve_item.
export const STAGE_ORDER: Stage[] = ["needs_review", "pending", "sent", "waiting", "replied", "booked"];

// Stages this agent sets on its own (pending -> sent, or -> failed on a send
// error; needs_review -> pending happens on a successful re-draft too).
// waiting/replied/booked are advanced by a person or a future
// reply-tracking agent -- see Outreach_Agent/config/outreach.yaml stages.
export const AGENT_OWNED_STAGES: Stage[] = ["needs_review", "pending", "sent", "failed"];
export const MANUAL_STAGES: Array<"waiting" | "replied" | "booked"> = ["waiting", "replied", "booked"];

/** A Tier A/B company left out of an import or campaign because Hunter found
 *  no contact for it -- see Outreach_Agent's ingest.create_campaign_from_prospects
 *  and research_client.qualified_prospects. Score/tier are untouched by this;
 *  it's purely a "nobody to email yet" exclusion. */
export interface ExcludedProspect {
  domain: string;
  name: string;
  tier: string | null;
}

export interface ImportedProspect {
  research_domain: string;
  research_run_id: string | null;
  name: string | null;
  tier: string | null;
  score: number | null;
  industry: string | null;
  region: string | null;
  description: string | null;
  pain_summary: string | null;
  recommended_angle: string | null;
  open_reqs: Array<Record<string, unknown>>;
  contact_email: string | null;
  contact_name: string | null;
  contact_position: string | null;
  contact_linkedin_url: string | null;
  imported_at: string | null;
}

export interface Campaign {
  campaign_id: string;
  name: string | null;
  research_run_id: string | null;
  channel: string;
  selection: Record<string, unknown>;
  created_at: string | null;
  notes: string | null;
}

export interface OutreachItem {
  item_id: string;
  campaign_id: string;
  research_domain: string;
  stage: Stage;
  channel: string;
  subject: string | null;
  body: string | null;
  personalized_from: string | null;
  to_email: string | null;
  provider_message_id: string | null;
  error: string | null;
  scheduled_at: string | null;
  created_at: string | null;
  sent_at: string | null;
  stage_updated_at: string | null;
}

export interface MessagePreview {
  subject: string;
  body: string;
  personalized_from: string;
}

export interface OutreachAnalytics {
  campaign_id: string;
  campaign_count: number;
  total_items: number;
  sent_total: number;
  reply_rate: number;
  booking_rate: number;
  stage_funnel: Array<{ stage: Stage; count: number }>;
  failed_count: number;
  needs_review_count: number;
  tiers: Array<{ tier: string; count: number }>;
  industries: Array<{ industry: string; count: number }>;
  personalized_from: Array<{ source: string; count: number }>;
  sends_over_time: Array<{ date: string; count: number }>;
  channels: Array<{ channel: string; count: number }>;
}

export interface OutreachEvent {
  event_id: string;
  item_id: string;
  event_type: string;
  from_stage: string | null;
  to_stage: string | null;
  actor: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

export interface JobSnapshot {
  job_id: string;
  kind: "import" | "send";
  campaign_id: string | null;
  status: "pending" | "running" | "done" | "error";
  done: number;
  total: number;
  current_domain: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

export interface ImportRequest {
  run_id: string;
  tiers?: string[];
  min_score?: number | null;
  limit?: number | null;
}

export interface CampaignCreateRequest {
  name?: string | null;
  research_run_id?: string | null;
  limit?: number | null;
  tiers?: string[] | null;
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

export const outreachApi = {
  baseUrl: BASE,

  researchRuns: () => req<Array<{ run_id: string; label: string | null }>>("/research/runs"),

  startImport: (body: ImportRequest) =>
    req<{ job_id: string }>("/import", { method: "POST", body: JSON.stringify(body) }),

  listProspects: (runId?: string) =>
    req<ImportedProspect[]>(`/prospects${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`),

  // Same cascade the Research Agent triggers on its own when a run is
  // deleted (see Research_Agent's api.delete_run / WORKFLOW.md "Deleting a
  // run") -- exposed here too so a person can clear out a run's imported
  // prospects (and any campaign/items built from them) from this side,
  // without having to go delete the research run itself to trigger it.
  deleteProspectsByRun: (runId: string) =>
    req<{ status: string; campaigns_removed: number; items_removed: number; prospects_removed: number }>(
      `/research/runs/${encodeURIComponent(runId)}`,
      { method: "DELETE" },
    ),

  listCampaigns: () => req<Campaign[]>("/campaigns"),

  analytics: (campaignId: string) =>
    req<OutreachAnalytics>(`/analytics?campaign_id=${encodeURIComponent(campaignId)}`),

  createCampaign: (body: CampaignCreateRequest) =>
    req<{ campaign: Campaign; item_count: number; excluded_no_contact: ExcludedProspect[] }>("/campaigns", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Seeds a fixed, safe-to-repeat Test Campaign with two synthetic prospects
  // (never real Research Agent data) for QA'ing the pipeline UI -- see
  // Outreach_Agent's ingest.create_test_campaign for the exact fixture.
  createTestCampaign: () =>
    req<{ campaign: Campaign; item_count: number }>("/campaigns/test", { method: "POST" }),

  campaignItems: (campaignId: string) => req<OutreachItem[]>(`/campaigns/${campaignId}/items`),

  deleteCampaign: (campaignId: string) =>
    req<{ status: string }>(`/campaigns/${campaignId}`, { method: "DELETE" }),

  renameCampaign: (campaignId: string, name: string) =>
    req<{ campaign_id: string; name: string }>(`/campaigns/${campaignId}/rename`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  startSend: (campaignId: string) =>
    req<{ job_id: string | null; message?: string; items?: number }>(`/campaigns/${campaignId}/send`, {
      method: "POST",
    }),

  scheduleCampaign: (campaignId: string, scheduledAt: string | null) =>
    req<{ status: string; items_updated: number }>(`/campaigns/${campaignId}/schedule`, {
      method: "POST",
      body: JSON.stringify({ scheduled_at: scheduledAt }),
    }),

  itemDetail: (itemId: string) =>
    req<{ item: OutreachItem; events: OutreachEvent[] }>(`/items/${itemId}`),

  itemPreview: (itemId: string) => req<MessagePreview>(`/items/${itemId}/preview`),

  reviseItem: (itemId: string, instruction: string) =>
    req<MessagePreview>(`/items/${itemId}/revise`, {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),

  redraftItem: (itemId: string) => req<MessagePreview & { stage: Stage }>(`/items/${itemId}/redraft`, { method: "POST" }),

  approveItem: (itemId: string) => req<{ status: string }>(`/items/${itemId}/approve`, { method: "POST" }),

  redraftGenericItems: (campaignId: string) =>
    req<{ checked: number; upgraded: number; still_generic: number }>(
      `/campaigns/${campaignId}/redraft-generic`,
      { method: "POST" },
    ),

  scheduleItem: (itemId: string, scheduledAt: string | null) =>
    req<{ status: string }>(`/items/${itemId}/schedule`, {
      method: "POST",
      body: JSON.stringify({ scheduled_at: scheduledAt }),
    }),

  deleteItem: (itemId: string) =>
    req<{ status: string }>(`/items/${itemId}`, { method: "DELETE" }),

  setItemStage: (itemId: string, stage: "waiting" | "replied" | "booked") =>
    req<{ status: string }>(`/items/${itemId}/stage`, {
      method: "POST",
      body: JSON.stringify({ stage }),
    }),

  jobStatus: (jobId: string) => req<JobSnapshot>(`/jobs/${jobId}`),

  jobSocket: (jobId: string, onMessage: (s: JobSnapshot) => void): WebSocket => {
    const wsBase = BASE.replace(/^http/, "ws");
    const ws = new WebSocket(`${wsBase}/jobs/${jobId}/ws`);
    ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
    return ws;
  },
};
