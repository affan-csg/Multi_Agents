/** Typed client for the Outreach Analytics dashboard
 *  (Outreach_Agent/src/outreach/analytics/routes.py). Same backend process as
 *  outreach.ts, so it reuses that module's base URL. */

import { outreachApi } from "./outreach";

const BASE = outreachApi.baseUrl;

/** A rate with its numerator/denominator so the UI can show "3 of 12".
 *  `pct` is null when the denominator is 0; `low_sample` flags a denominator
 *  too small to read much into. */
export interface Rate {
  n: number;
  d: number;
  pct: number | null;
  low_sample: boolean;
}

export interface Totals {
  sent: number;
  replies: number;
  positive_replies: number;
  follow_ups_sent: number;
  bookings: number;
  no_replies: number;
  failed: number;
  bounced: number;
  reply_rate: Rate;
  positive_reply_rate: Rate;
  booking_rate: Rate;
  failure_rate: Rate;
}

export interface TrendPoint {
  date: string;
  sent: number;
  replies: number;
  positive_replies: number;
  bookings: number;
  follow_ups: number;
}

export interface AccountRow extends Totals {
  /** Lower-cased address, or "unknown" for items sent before sender tracking. */
  sender: string;
  label: string;
  configured: boolean;
  trend: TrendPoint[];
}

export interface CampaignRow extends Totals {
  campaign_id: string;
  name: string;
  created_at: string | null;
  senders: string[];
  prospects: number;
}

export interface FollowupRow {
  followup_number: number;
  sent: number;
  replied_after: number;
  reply_rate: Rate;
}

export interface BookingRow {
  item_id: string | null;
  campaign_id: string | null;
  campaign_name: string | null;
  sender: string | null;
  company_name: string | null;
  contact_name: string | null;
  contact_email: string | null;
  status: string;
  booked_at: string | null;
  meeting_starts_at: string | null;
  no_show: boolean;
  source: string;
  matched: boolean;
}

export interface BookingsBlock {
  rows: BookingRow[];
  total: number;
  active: number;
  canceled: number;
  unmatched: number;
  trend: Array<{ date: string; active: number; canceled: number }>;
}

export interface AnalyticsSummary {
  filters: { sender: string | null; campaign_id: string | null; date_from: string | null; date_to: string | null };
  senders: Array<{ sender: string; label: string; configured: boolean }>;
  campaigns_available: Array<{ campaign_id: string; name: string }>;
  /** Empty object when that block failed (see `errors`). */
  overall: Partial<Totals>;
  trend: TrendPoint[];
  accounts: AccountRow[];
  campaigns: CampaignRow[];
  followups: { rows: FollowupRow[]; note: string };
  bookings: BookingsBlock;
  /** Blocks that could not be computed; the rest of the page still renders. */
  errors: Record<string, string>;
}

export interface ProspectRow {
  item_id: string;
  campaign_id: string;
  campaign_name: string | null;
  sender: string;
  company_name: string | null;
  contact_name: string | null;
  contact_email: string | null;
  stage: string;
  sent_at: string | null;
  follow_ups: number;
  replied: boolean;
  positive: boolean;
  booked: boolean;
  failed: boolean;
  bounced: boolean;
  error: string | null;
}

export interface ProspectsPage {
  total: number;
  limit: number;
  offset: number;
  rows: ProspectRow[];
  errors: Record<string, string>;
}

export interface AnalyticsFilters {
  /** A sending address, or "unknown"; omitted = every account. */
  sender?: string;
  campaignId?: string;
  dateFrom?: string;
  dateTo?: string;
}

function qs(f: AnalyticsFilters, extra: Record<string, string | number> = {}): string {
  const p = new URLSearchParams();
  if (f.sender) p.set("sender", f.sender);
  if (f.campaignId) p.set("campaign_id", f.campaignId);
  if (f.dateFrom) p.set("date_from", f.dateFrom);
  if (f.dateTo) p.set("date_to", f.dateTo);
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json();
}

export const analyticsApi = {
  summary: (f: AnalyticsFilters) => get<AnalyticsSummary>(`/analytics/v2/summary${qs(f)}`),
  prospects: (f: AnalyticsFilters, limit = 200) =>
    get<ProspectsPage>(`/analytics/v2/prospects${qs(f, { limit })}`),
};
