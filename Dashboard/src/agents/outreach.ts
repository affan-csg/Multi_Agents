/** Typed client for the Outreach Agent's API (Outreach_Agent/src/outreach/api.py).
 *  Same conventions as research.ts: one module per agent, talking only to
 *  that agent's own HTTP API. */

import { agentBaseUrl } from "./registry";

const BASE = agentBaseUrl("outreach");

export type Stage =
  | "needs_review" | "pending" | "sent" | "waiting" | "replied" | "booked" | "not_interested" | "failed";

// needs_review sits before pending: an item lands there instead of pending
// when it was drafted from the generic template_only fallback (no
// meaningful research to personalize from) -- see Outreach_Agent's
// ingest.create_campaign_from_prospects and api.approve_item.
//
// 'not_interested' (labeled "Failed" in this UI) is the Conversation Agent's
// terminal rejection outcome, placed after 'booked'. It is a DIFFERENT
// concept from 'failed' below, which is the Outreach Agent's send-error
// stage (SMTP/delivery failure) -- see models.py's Stage comment on the
// backend for the full rationale. 'failed' (send errors) is intentionally
// left out of this UI-facing order -- see AGENT_OWNED_STAGES below.
export const STAGE_ORDER: Stage[] = ["needs_review", "pending", "sent", "waiting", "replied", "booked", "not_interested"];

// Stages the Outreach Agent sets on its own (pending -> sent, or -> failed on
// a send error; needs_review -> pending happens on a successful re-draft too).
export const AGENT_OWNED_STAGES: Stage[] = ["needs_review", "pending", "sent", "failed"];

// Stages after Sent. Owned by the Conversation Agent when it is switched on
// for the campaign, and by a person otherwise -- drag-and-drop stays enabled
// either way, and a person's move always overrides the agent's.
//
// not_interested is included here (unlike the Outreach Agent's own 'failed')
// so a person can manually mark a rejection, or re-open a misclassified one
// back to Waiting/Replied/Booked -- see api.set_item_stage, which is not
// bound by CONVERSATION_TRANSITIONS and always allows a manual override.
export const MANUAL_STAGES: Array<"waiting" | "replied" | "booked" | "not_interested"> =
  ["waiting", "replied", "booked", "not_interested"];
export const CONVERSATION_STAGES: Stage[] = ["sent", "waiting", "replied", "booked", "not_interested"];

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
  /** Every research run that has ever imported this domain, not just the
   *  most recent one (`research_run_id` above) -- this is what
   *  GET /prospects?run_id=... filters membership on server-side, so a
   *  prospect stays visible under an older run's filtered view even after a
   *  later rerun (which always gets a brand-new run_id -- the Research
   *  Agent has no "resume this run" path) refreshed its data under a new
   *  run. The frontend doesn't need to filter on this directly today (the
   *  backend already scopes the response to `run_id`), it's just part of
   *  the wire shape now. */
  research_run_ids: string[];
  name: string | null;
  tier: string | null;
  score: number | null;
  industry: string | null;
  region: string | null;
  description: string | null;
  pain_summary: string | null;
  recommended_angle: string | null;
  open_reqs: Array<Record<string, unknown>>;
  /** 'confirmed' | 'unknown' | null -- whether `open_reqs` being empty means
   *  Research actually read this company's job board/careers page and found
   *  nothing (confirmed), or every discovery method failed and the real
   *  count is simply unknown (see Research_Agent's models.HiringSignal).
   *  Never read an empty `open_reqs` as "not hiring" when this is
   *  'unknown'. */
  hiring_signal: "confirmed" | "unknown" | null;
  /** True when the Research Agent's last qualification attempt for this
   *  domain failed and nothing has succeeded since (see Research_Agent's
   *  qualification_attempts table). Distinguishes "qualification failed,
   *  retry in Research" from "never attempted" -- both used to render
   *  identically as pain_summary === null. Refreshed only on re-import;
   *  Outreach never calls Research's qualify endpoints itself. */
  qualification_failed: boolean;
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
  /** Share of sent items that ended in a clear rejection (the terminal
   *  not_interested stage, "Failed" in this UI). A negative campaign-outcome
   *  metric alongside booking_rate -- reply_rate still counts a rejection as
   *  "replied", same convention the Learning/Optimization agents use. */
  failure_rate: number;
  stage_funnel: Array<{ stage: Stage; count: number }>;
  /** Outreach Agent send-error count (SMTP/delivery failure) -- distinct
   *  from not_interested_count below; see the Stage comment in outreach.ts. */
  failed_count: number;
  not_interested_count: number;
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
  /** 'system' (Outreach Agent) | 'user' (a person) | 'conversation_agent'
   *  (Agent 3). Every stage change records which of the three moved it. */
  actor: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

// --- Conversation Agent (Agent 3) -------------------------------------------
// Owns sent -> waiting -> replied -> booked, per campaign, and only while
// switched on. See Outreach_Agent/src/outreach/conversation/.

/** What an inbound reply was judged to mean. `out_of_office` and `bounce`
 *  are deliberately not replies -- mail arrived but no human answered, so
 *  they never advance a card. */
export type ReplyIntent =
  | "interested" | "booked" | "not_interested"
  | "out_of_office" | "bounce" | "unclear";

export interface ConversationSettings {
  campaign_id: string;
  /** Master switch. Off means the agent does nothing at all for this
   *  campaign -- no reply detection, no card moves, no sends. */
  enabled: boolean;
  /** Independent of `enabled`. With the agent on and this off, replies are
   *  detected, classified, cards move and drafts are written -- but nothing
   *  is emailed without a person approving it. */
  auto_reply: boolean;
  followup_after_days: number;
  max_followups: number;
  updated_at: string | null;
}

export interface ConversationMessage {
  message_id: string;
  item_id: string;
  direction: "inbound" | "outbound";
  provider_message_id: string | null;
  in_reply_to: string | null;
  subject: string | null;
  body: string | null;
  from_email: string | null;
  to_email: string | null;
  intent: ReplyIntent | null;
  /** True on a reply the agent composed but did not send (auto_reply off) --
   *  it is waiting for a person to approve or discard it. */
  draft_pending: boolean;
  /** `body` with the quoted original stripped, computed server-side (the
   *  same stripping the classifier itself judges from) -- what the drawer
   *  should show by default. `body` is untouched and always the full
   *  original, one click away behind "Show full quote". Equal to `body` on
   *  an outbound message, which never carries a quoted original. */
  clean_body: string;
  sent_at: string | null;
  received_at: string | null;
  created_at: string | null;
}

export interface ItemConversation {
  item_id: string;
  stage: Stage;
  followup_count: number;
  last_inbound_at: string | null;
  enabled: boolean;
  auto_reply: boolean;
  messages: ConversationMessage[];
  pending_drafts: ConversationMessage[];
}

/** What one "Check replies" run did. Every counter is something that
 *  actually happened, so an all-zero result means a genuine no-op. */
export interface TickResult {
  campaign_id: string;
  enabled: boolean;
  fetched: number;
  matched: number;
  new_inbound: number;
  duplicates: number;
  unmatched: number;
  classified: Record<string, number>;
  advanced: number;
  replies_sent: number;
  replies_drafted: number;
  followups_sent: number;
  followups_drafted: number;
  moved_to_waiting: number;
  skipped_locked: number;
  errors: Record<string, string>;
  message: string | null;
}

// One prospect currently in the Booked stage -- Bookings page. See the
// Outreach Agent's GET /bookings.
export interface BookingEntry {
  item_id: string;
  campaign_id: string;
  campaign_name: string | null;
  research_domain: string;
  company_name: string | null;
  contact_name: string | null;
  contact_email: string | null;
  contact_position: string | null;
  industry: string | null;
  sent_at: string | null;
  /** When the card actually moved to Booked, from outreach_events -- null
   *  only for a booking made before this endpoint existed. */
  booked_at: string | null;
  booked_detail: Record<string, unknown>;
  /** 'conversation_agent' | 'user' | null -- who moved the card. */
  booked_by: string | null;
  /** The account's Calendly scheduling page, from config/CALENDLY_URL --
   *  same link on every row (no per-booking event URL is captured), null
   *  if never configured. */
  calendly_url: string | null;
}

export interface ConversationSettingsUpdate {
  enabled?: boolean;
  auto_reply?: boolean;
  followup_after_days?: number;
  max_followups?: number;
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

  setItemStage: (itemId: string, stage: "waiting" | "replied" | "booked" | "not_interested") =>
    req<{ status: string }>(`/items/${itemId}/stage`, {
      method: "POST",
      body: JSON.stringify({ stage }),
    }),

  // --- Conversation Agent -------------------------------------------------

  conversationSettings: (campaignId: string) =>
    req<ConversationSettings>(`/campaigns/${campaignId}/conversation/settings`),

  setConversationSettings: (campaignId: string, body: ConversationSettingsUpdate) =>
    req<ConversationSettings>(`/campaigns/${campaignId}/conversation/settings`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // One conversation cycle, run now -- the "Check replies" button. Returns a
  // no-op result rather than an error when the agent is off, so the UI can
  // explain why nothing happened.
  conversationTick: (campaignId: string) =>
    req<TickResult>(`/campaigns/${campaignId}/conversation/tick`, { method: "POST" }),

  itemConversation: (itemId: string) => req<ItemConversation>(`/items/${itemId}/conversation`),

  // Compose a reply to the item's latest inbound message on demand -- the
  // drawer's "Draft a reply" button. A retry of the same drafting a tick
  // already attempts once per new reply; exists for when that first attempt
  // failed (an LLM call errored or timed out) or was never run because the
  // agent was off when the reply arrived.
  draftConversationReply: (itemId: string) =>
    req<{ status: string; message_id: string; draft_pending: boolean; subject: string; body: string }>(
      `/items/${itemId}/conversation/draft`,
      { method: "POST" },
    ),

  // Sends a draft a person approved. Deliberately works while the agent is
  // switched off -- a human clicking send is the authorization.
  sendConversationDraft: (messageId: string) =>
    req<{ status: string; message_id: string; provider_message_id: string | null }>(
      `/conversation/messages/${messageId}/send`,
      { method: "POST" },
    ),

  discardConversationDraft: (messageId: string) =>
    req<{ status: string }>(`/conversation/messages/${messageId}`, { method: "DELETE" }),

  // Edits an already-drafted reply per a free-text instruction, in place --
  // same contract as reviseItem for a cold-outreach draft.
  reviseConversationDraft: (messageId: string, instruction: string) =>
    req<{ status: string; message_id: string; subject: string; body: string }>(
      `/conversation/messages/${messageId}/revise`,
      { method: "POST", body: JSON.stringify({ instruction }) },
    ),

  // Fully regenerates a pending draft from scratch -- same contract as
  // redraftItem for a cold-outreach draft.
  redraftConversationDraft: (messageId: string) =>
    req<{ status: string; message_id: string; subject: string; body: string }>(
      `/conversation/messages/${messageId}/redraft`,
      { method: "POST" },
    ),

  // Every prospect currently in the Booked stage, across every campaign.
  listBookings: () => req<BookingEntry[]>("/bookings"),

  jobStatus: (jobId: string) => req<JobSnapshot>(`/jobs/${jobId}`),

  jobSocket: (jobId: string, onMessage: (s: JobSnapshot) => void): WebSocket => {
    const wsBase = BASE.replace(/^http/, "ws");
    const ws = new WebSocket(`${wsBase}/jobs/${jobId}/ws`);
    ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
    return ws;
  },
};
