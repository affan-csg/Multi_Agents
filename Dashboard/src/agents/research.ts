/** Typed client for the Research Agent's API. Ported from
 *  Research_Agent/frontend/src/api.ts -- same request/response shapes, since
 *  it talks to the exact same FastAPI backend (unmodified). Kept as its own
 *  module (not shared code) so each agent's client can evolve independently
 *  as that agent's API changes. */

import { agentBaseUrl } from "./registry";
import { withToken } from "../auth";

const BASE = agentBaseUrl("research");

export interface ProvenanceValue<T> {
  value: T;
  source?: string;
  url?: string | null;
  fetched_at?: string | null;
  method?: "reported" | "estimated" | "unknown";
  confidence?: "high" | "medium" | "low";
}

export interface RunResult {
  run_id: string;
  companies: number;
  suppressed: number;
  no_ats: number;
  ats_found: number;
  no_reqs: number;
  reqs_fetched: number;
  disqualified: number;
  scored: number;
  tiers: Record<string, number>;
  dq_reasons: Record<string, number>;
  detected_by: Record<string, number>;
  providers: Record<string, number>;
  /** Counts by which job-discovery method actually produced each company's
   *  signal ('ats' | 'careers_page' | 'web_search' | 'unknown') -- see
   *  Research_Agent's jobs_discovery.py. Answers "was any job signal found"
   *  more completely than no_ats/ats_found, which only ever meant "was an
   *  ATS board found". */
  job_discovery_methods?: Record<string, number>;
  /** Companies where every discovery method was exhausted -- job signal is
   *  UNKNOWN, not a confirmed zero. Distinct from no_reqs (a source WAS
   *  read, it just had zero postings). */
  hiring_signal_unknown?: number;
  region?: string;
  discovered?: number;
  enriched?: number;
  enrich_skipped?: number;
  enrich_failures?: Record<string, number>;
  exports?: Record<string, string>;
  qualify?: { qualified: number; skipped: number; errors: number; error_details: Record<string, string> } | { error: string } | null;
  contacts?: { found: number; skipped: number; errors: number; contacts_saved: number; error_details: Record<string, string> } | { error: string } | null;
  auto_qualify_top_n?: number;
  auto_qualify_skipped_tier?: number;
}

export interface Region {
  key: string;
  label: string;
  states: string[];
  hubs: string[];
}

export interface RegionOptions {
  active: string;
  regions: Region[];
  icp: {
    profile: string | null;
    size_band: [number, number];
    revenue_min: number | null;
    practice_areas: string[];
  };
}

export interface DiscoverRequest {
  region?: string | null;
  limit?: number;
  use_cache?: boolean;
  label?: string | null;
  auto_qualify_top?: number | null;
}

export interface JobSnapshot {
  job_id: string;
  kind: "discover" | "qualify" | "contacts";
  run_id: string | null;
  status: "pending" | "running" | "done" | "error";
  done: number;
  total: number;
  current_domain: string | null;
  result: RunResult | Record<string, unknown> | null;
  error: string | null;
}

export interface RunSummary {
  run_id: string;
  label: string | null;
  started_at: string;
  finished_at: string | null;
  profile: string | null;
  seed_count: number | null;
  notes: string | null;
  /** Companies this run scored with no ATS board and no firmographics --
   *  the signature of every provider being exhausted for them. Resume is
   *  only meaningful (and only shown in the UI) when this is > 0. */
  stalled_count: number;
}

export interface ScoredCompany {
  domain: string;
  run_id: string;
  score: number;
  tier: string;
  dimensions: Record<string, number>;
  explanation: string[];
  metrics: Record<string, unknown>;
  scored_at: string;
  /** True when this run found a company that already existed -- it is listed
   *  here but was not re-created, re-scored, or handed to Outreach. */
  existing_match?: boolean;
  matched_from_run?: string | null;
  name: string | null;
  employee_count: ProvenanceValue<number> | null;
  revenue: ProvenanceValue<string> | null;
  funding: ProvenanceValue<string> | null;
  industry: string | null;
  description: string | null;
  region: string | null;
  ats_provider: string | null;
  ats_token: string | null;
  /** How this company's job signal was found -- see Research_Agent's
   *  jobs_discovery.py: 'ats' | 'careers_page' | 'web_search' | 'unknown'. */
  job_discovery_method?: "ats" | "careers_page" | "web_search" | "unknown" | null;
  /** 'confirmed' | 'unknown' -- whether metrics.req_count == 0 means a
   *  board/careers page was actually read and found empty, or every
   *  discovery method was exhausted without reading anything at all.
   *  'unknown' must never be displayed or treated the same as a confirmed
   *  zero -- see Research_Agent's models.HiringSignal. */
  hiring_signal?: "confirmed" | "unknown" | null;
}

export interface CompanyDetail {
  company: {
    domain: string;
    name: string | null;
    employee_count: ProvenanceValue<number> | null;
    revenue: ProvenanceValue<string> | null;
    funding: ProvenanceValue<string> | null;
    industry: string | null;
    description: string | null;
    region: string | null;
    hq_state: string | null;
    ats_provider: string | null;
    ats_token: string | null;
    ats_detected_by: string | null;
    careers_url: string | null;
    status: string;
    dq_reason: string | null;
    /** How this company's job signal was found -- see Research_Agent's
     *  jobs_discovery.py: 'ats' | 'careers_page' | 'web_search' | 'unknown'.
     *  'unknown' means every method was exhausted, NOT a confirmed zero. */
    job_discovery_method: "ats" | "careers_page" | "web_search" | "unknown" | null;
    job_source: string | null;
    hiring_signal: "confirmed" | "unknown" | null;
    job_discovery_notes: string | null;
  };
  score: ScoredCompany | null;
  postings: Array<{
    domain: string;
    ats_provider: string | null;
    source: "ats" | "careers_page" | "web_search";
    external_id: string;
    title: string;
    location: string | null;
    department: string | null;
    employment_type: string | null;
    url: string | null;
    posted_at: string | null;
    description: string;
    delivery_fit: string | null;
    practice_area: string | null;
  }>;
  qualification: {
    pain_summary: string;
    recommended_angle: string;
    model: string;
    qualified_at: string;
  } | null;
  /** Latest qualification attempt for this company+run, success or failure --
   *  null only when qualification was never attempted at all. Lets the UI
   *  distinguish "not qualified yet" from "qualification failed, retry" (see
   *  Research_Agent's qualification_attempts table / migrations/004). */
  qualification_attempt: {
    status: "succeeded" | "failed";
    error_reason: string | null;
    error_detail: string | null;
    attempt_count: number;
    last_attempted_at: string;
  } | null;
  contacts: Array<{
    email: string;
    first_name: string | null;
    last_name: string | null;
    position: string | null;
    seniority: string | null;
    confidence: number | null;
    verification: string | null;
    linkedin_url: string | null;
  }>;
  outreach_ready?: boolean;
  has_linkedin_contact?: boolean;
}

export interface CompanyFilter {
  tier?: string;
  state?: string;
  industry?: string;
  min_score?: number;
  keyword?: string;
}

export interface SavedCompany {
  domain: string;
  saved_from_run: string | null;
  note: string | null;
  saved_at: string;
  company: CompanyDetail["company"] | null;
  score: ScoredCompany | null;
}

export interface Contact {
  domain: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  position: string | null;
  seniority: string | null;
  department: string | null;
  linkedin_url: string | null;
  confidence: number | null;
  verification: string | null;
  source: string;
  found_at: string;
  company_name: string | null;
  industry: string | null;
}

export interface Analytics {
  run_id: string;
  total: number;
  avg_score: number;
  tiers: Array<{ tier: string; count: number }>;
  industries: Array<{ industry: string; count: number; avg_score: number }>;
  providers: Array<{ provider: string; count: number }>;
  dimensions: Array<{ dimension: string; avg: number }>;
  score_distribution: Array<{ bucket: string; count: number }>;
  scatter: Array<{
    domain: string;
    reqs: number;
    score: number;
    tier: string;
    median_age: number | null;
    /** 'confirmed' | 'unknown' -- see ScoredCompany.hiring_signal. A point
     *  at reqs=0 with 'unknown' was never actually confirmed as having zero
     *  openings; job discovery was simply exhausted for that company. */
    hiring_signal?: "confirmed" | "unknown" | null;
  }>;
}

export interface SuppressedCompany {
  domain: string;
  reason: string | null;
  added_at: string;
  company_name: string | null;
}

export interface ApiKeyStatus {
  name: string;
  configured: boolean;
  masked: string | null;
}

export interface IcpBasics {
  active_region: string;
  available_regions: Array<{ key: string; label: string }>;
  // Editable from the Settings page's Built-in ICP card:
  sweet_min: number;
  sweet_max: number;
  revenue_sweet_min: number | null;
  // Displayed on the same card, read-only:
  hard_min: number;
  hard_max: number;
  funding_preferred: boolean;
  tier_a_threshold: number;
  tier_b_threshold: number;
  tier_c_threshold: number;
  target_industries: string[];
  practice_areas: string[];
  require_us_presence: boolean;
  min_relevant_reqs: number | null;
  competitor_recruiter_req_share: number | null;
}

export interface IcpBasicsUpdate {
  sweet_min?: number;
  sweet_max?: number;
  revenue_sweet_min?: number;
}

export interface Settings {
  profile: string | null;
  weights: Record<string, number>;
  tiers: Record<string, number>;
  icp_basics: IcpBasics;
  api_keys: ApiKeyStatus[];
}

export interface UsageDailyBucket {
  date: string;
  requests: number;
  failures: number;
  total_tokens: number;
}

export interface ProviderUsage {
  requests: number;
  successes: number;
  failures: number;
  retries: number;
  rate_limited_429: number;
  quota_exhausted: number;
  connection_errors: number;
  parse_errors: number;
  other_errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  avg_latency_ms: number | null;
  daily: UsageDailyBucket[];
}

export interface UsageSummary {
  /** Set only when the window was requested as an hours-back count (e.g.
   *  "Last 7 Days"); null for "All Time" and for an explicit `since`
   *  (e.g. "Latest Research Run"). */
  window_hours: number | null;
  /** Echoes back whatever cutoff the backend actually used, if any -- null
   *  means "All Time" (no lower bound). */
  since: string | null;
  providers: {
    gemini: ProviderUsage;
    groq: ProviderUsage;
    tavily: ProviderUsage;
    hunter: ProviderUsage;
  };
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

export const researchApi = {
  baseUrl: BASE,

  regions: () => req<RegionOptions>("/regions"),

  startDiscover: (body: DiscoverRequest = {}) =>
    req<{ job_id: string; region: string }>("/discover", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listRuns: () => req<RunSummary[]>("/runs"),

  renameRun: (runId: string, label: string) =>
    req<{ status: string }>(`/runs/${runId}`, {
      method: "PATCH",
      body: JSON.stringify({ label }),
    }),

  deleteRun: (runId: string) => req<{ status: string }>(`/runs/${runId}`, { method: "DELETE" }),

  resumeRun: (runId: string) =>
    req<{ job_id: string; run_id: string; stalled_count: number }>(`/runs/${runId}/resume`, { method: "POST" }),

  runCompanies: (runId: string, filter: CompanyFilter = {}) => {
    const params = new URLSearchParams();
    if (filter.tier) params.set("tier", filter.tier);
    if (filter.state) params.set("state", filter.state);
    if (filter.industry) params.set("industry", filter.industry);
    if (filter.min_score !== undefined) params.set("min_score", String(filter.min_score));
    if (filter.keyword) params.set("keyword", filter.keyword);
    const qs = params.toString();
    return req<ScoredCompany[]>(`/runs/${runId}/companies${qs ? `?${qs}` : ""}`);
  },

  companyDetail: (runId: string, domain: string) =>
    req<CompanyDetail>(`/runs/${runId}/companies/${domain}`),

  suppress: (runId: string, domain: string, reason: string) =>
    req<{ status: string }>(`/runs/${runId}/suppression`, {
      method: "POST",
      body: JSON.stringify({ domain, reason }),
    }),

  startQualify: (runId: string, tiers: string[], force = false, domains?: string[]) =>
    req<{ job_id: string | null; message?: string; provider?: string; companies?: number }>(
      `/runs/${runId}/qualify`,
      { method: "POST", body: JSON.stringify({ tiers, force, domains: domains ?? null }) },
    ),

  listFailedQualifications: (runId: string) =>
    req<ScoredCompany[]>(`/runs/${runId}/qualify/failed`),

  retryFailedQualifications: (runId: string) =>
    req<{ job_id: string | null; message?: string; provider?: string; companies?: number }>(
      `/runs/${runId}/qualify/retry-failed`,
      { method: "POST" },
    ),

  startContacts: (runId: string, tiers: string[], force = false) =>
    req<{ job_id: string | null; message?: string; companies?: number }>(`/runs/${runId}/contacts`, {
      method: "POST",
      body: JSON.stringify({ tiers, force }),
    }),

  analytics: (runId: string | "all") => req<Analytics>(`/runs/${runId}/analytics`),

  listContacts: (runId: string | "all" = "all") => req<Contact[]>(`/runs/${runId}/contacts`),

  listSaved: (runId: string | "all" = "all") => req<SavedCompany[]>(`/runs/${runId}/saved`),

  savedDomains: (runId: string | "all" = "all") => req<string[]>(`/runs/${runId}/saved/domains`),

  save: (domain: string, runId: string | null, note?: string) =>
    req<{ status: string }>(`/runs/${runId || "all"}/saved`, {
      method: "POST",
      body: JSON.stringify({ domain, note: note ?? null }),
    }),

  unsave: (domain: string, runId: string | null = null) =>
    req<{ status: string }>(`/runs/${runId || "all"}/saved/${encodeURIComponent(domain)}`, { method: "DELETE" }),

  listSuppression: (runId: string | "all" = "all") => req<SuppressedCompany[]>(`/runs/${runId}/suppression`),

  addSuppression: (domain: string, reason: string, runId: string | "all" = "all") =>
    req<{ status: string }>(`/runs/${runId}/suppression`, {
      method: "POST",
      body: JSON.stringify({ domain, reason }),
    }),

  removeSuppression: (domain: string, runId: string | "all" = "all") =>
    req<{ status: string }>(`/runs/${runId}/suppression/${encodeURIComponent(domain)}`, { method: "DELETE" }),

  getSettings: () => req<Settings>("/settings"),

  /** Pass exactly one of `hours` (e.g. 24*7 for "Last 7 Days") or `since`
   *  (an ISO timestamp, e.g. a run's started_at, for "Latest Research
   *  Run") -- omit both for "All Time". */
  getUsage: (opts: { hours?: number; since?: string } = {}) => {
    const params = new URLSearchParams();
    if (opts.since) params.set("since", opts.since);
    else if (opts.hours !== undefined) params.set("hours", String(opts.hours));
    const qs = params.toString();
    return req<UsageSummary>(`/settings/usage${qs ? `?${qs}` : ""}`);
  },

  updateIcpBasics: (fields: IcpBasicsUpdate) =>
    req<{ status: string; icp_basics: IcpBasics }>("/settings/icp", {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),

  /** URLs for the two on-demand, read-only exports (see Research_Agent's
   *  GET /runs/{run_id}/export/excel|pdf) -- plain download links, not
   *  fetch() calls, so the browser's native file-download flow handles the
   *  streamed response (same pattern as any `<a href>` file download). */
  exportExcelUrl: (runId: string) => withToken(`${BASE}/runs/${runId}/export/excel`),
  exportPdfUrl: (runId: string) => withToken(`${BASE}/runs/${runId}/export/pdf`),

  jobStatus: (jobId: string) => req<JobSnapshot>(`/jobs/${jobId}`),

  jobSocket: (jobId: string, onMessage: (s: JobSnapshot) => void): WebSocket => {
    const wsBase = BASE.replace(/^http/, "ws");
    const ws = new WebSocket(withToken(`${wsBase}/jobs/${jobId}/ws`));
    ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
    return ws;
  },
};
