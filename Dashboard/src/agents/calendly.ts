/** Typed client for the Calendly integration's API
 *  (Outreach_Agent/src/outreach/calendly/routes.py). Same backend as
 *  outreach.ts (mounted inside the Outreach Agent's own process), so it
 *  reuses that module's base URL rather than re-deriving one from the
 *  registry. The webhook receiver itself is never called from the
 *  Dashboard -- Calendly calls it directly -- this client only covers the
 *  manual sync trigger and status check the Bookings page uses. */

import { outreachApi } from "./outreach";

const BASE = outreachApi.baseUrl;

export interface CalendlySyncResult {
  fetched: number;
  created: number;
  updated: number;
  matched: number;
  unmatched: number;
  advanced_to_booked: number;
  reverted_from_booked: number;
  errors: string[];
}

export interface CalendlyStatus {
  api_configured: boolean;
  webhook_configured: boolean;
}

export interface CalendlyCancelResult {
  status: string;
  event_uri: string;
  item_id: string | null;
}

export interface CalendlyNoShowResult {
  status: string;
  event_uri: string;
  item_id: string | null;
}

export interface CalendlyDeleteResult {
  status: string;
  event_uri: string;
}

// Phase 2: view-only event types/availability (Calendly's API has no write
// endpoint for either -- only its own web UI can activate/deactivate an
// event type or edit availability) plus one real mutation, scheduling links.
export interface EventType {
  uri: string;
  name: string;
  active: boolean;
  duration: number;
  color: string | null;
  scheduling_url: string | null;
}

export interface AvailabilitySchedule {
  uri: string;
  name: string;
  timezone: string;
  default: boolean;
  rules: Record<string, unknown>[];
}

export interface SchedulingLink {
  booking_url: string;
  owner: string;
  owner_type: string;
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

export const calendlyApi = {
  status: () => req<CalendlyStatus>("/calendly/status"),

  sync: (lookbackDays?: number) =>
    req<CalendlySyncResult>(`/calendly/sync${lookbackDays ? `?lookback_days=${lookbackDays}` : ""}`, {
      method: "POST",
    }),

  cancel: (eventUri: string, reason?: string) =>
    req<CalendlyCancelResult>("/calendly/events/cancel", {
      method: "POST",
      body: JSON.stringify({ event_uri: eventUri, reason }),
    }),

  markNoShow: (eventUri: string) =>
    req<CalendlyNoShowResult>("/calendly/events/no-show", {
      method: "POST",
      body: JSON.stringify({ event_uri: eventUri }),
    }),

  unmarkNoShow: (eventUri: string) =>
    req<CalendlyNoShowResult>("/calendly/events/no-show", {
      method: "DELETE",
      body: JSON.stringify({ event_uri: eventUri }),
    }),

  // Permanently deletes a booking, any status -- cancels it on Calendly
  // first if still active, then removes the local row so it stops
  // appearing anywhere in this dashboard (see routes.py's
  // calendly_delete_booking / sync.delete_booking).
  deleteBooking: (eventUri: string) =>
    req<CalendlyDeleteResult>("/calendly/events/delete", {
      method: "POST",
      body: JSON.stringify({ event_uri: eventUri }),
    }),

  listEventTypes: () => req<EventType[]>("/calendly/event-types"),

  listAvailability: () => req<AvailabilitySchedule[]>("/calendly/availability"),

  createSchedulingLink: (eventTypeUri: string) =>
    req<SchedulingLink>("/calendly/scheduling-links", {
      method: "POST",
      body: JSON.stringify({ event_type_uri: eventTypeUri }),
    }),
};
