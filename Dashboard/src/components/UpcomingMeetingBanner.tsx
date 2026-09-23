import { useEffect, useState } from "react";
import { outreachApi, type BookingEntry } from "../agents/outreach";
import { useCachedFetch } from "../hooks/useCachedFetch";
import { Icon } from "./ui";

const REMINDER_WINDOW_MS = 24 * 60 * 60 * 1000;
const DISMISSED_KEY = "calendly_dismissed_reminders";

function readDismissed(): string[] {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function writeDismissed(uris: string[]): void {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(uris));
  } catch {
    // localStorage can throw (private browsing, blocked site data) --
    // dismissal just won't persist across reloads in that case.
  }
}

function formatRemaining(ms: number): string {
  const totalMinutes = Math.max(0, Math.round(ms / 60_000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes}m`;
  return `${hours}h ${minutes}m`;
}

/** A dismissible heads-up for a meeting starting within 24h -- shown at the
 *  app shell level (alongside ToastStack) rather than scoped to the Bookings
 *  page, since a time-sensitive reminder should be visible regardless of
 *  which page the user is currently on. Reuses the exact same unpaginated
 *  "bookings" cache key Bookings.tsx's listBookings() would use, so mounting
 *  this here never causes a duplicate network fetch. Shows only the single
 *  soonest undismissed qualifying meeting -- no stacking UI for multiple
 *  simultaneous reminders, which this codebase has no precedent for. */
export function UpcomingMeetingBanner({ onOpen }: { onOpen: () => void }) {
  const { data } = useCachedFetch<BookingEntry[]>("bookings", () => outreachApi.listBookings());
  const [dismissed, setDismissed] = useState<string[]>(() => readDismissed());
  // Re-render periodically so "in 3h 59m" counts down and a dismissed
  // meeting's own window (and the localStorage prune below) stays current
  // without needing a page reload.
  const [, forceTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  const bookings = data ?? [];
  const now = Date.now();

  const upcoming = bookings
    .filter((b) => b.event_uri && b.meeting_status === "active" && b.meeting_starts_at)
    .map((b) => ({ booking: b, startsAt: new Date(b.meeting_starts_at as string).getTime() }))
    .filter(({ startsAt }) => startsAt > now && startsAt - now <= REMINDER_WINDOW_MS)
    .sort((a, b) => a.startsAt - b.startsAt);

  // Prune dismissed entries whose meeting no longer qualifies (passed,
  // canceled, or fell out of the window) so the localStorage blob doesn't
  // grow unboundedly forever.
  useEffect(() => {
    const liveUris = new Set(upcoming.map(({ booking }) => booking.event_uri as string));
    const pruned = dismissed.filter((uri) => liveUris.has(uri));
    if (pruned.length !== dismissed.length) {
      setDismissed(pruned);
      writeDismissed(pruned);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookings.length]);

  const next = upcoming.find(({ booking }) => !dismissed.includes(booking.event_uri as string));
  if (!next) return null;

  const { booking, startsAt } = next;
  const who = booking.contact_name || booking.contact_email || "a prospect";

  const dismiss = () => {
    const uri = booking.event_uri as string;
    const updated = [...dismissed, uri];
    setDismissed(updated);
    writeDismissed(updated);
  };

  return (
    <div className="reminder-banner">
      <Icon name="calendar" size={16} />
      <span>
        Meeting with {who} in {formatRemaining(startsAt - now)} —{" "}
        {new Date(startsAt).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
      </span>
      <button type="button" className="ghost" onClick={onOpen} style={{ marginLeft: "auto" }}>
        View booking
      </button>
      <button type="button" className="reminder-banner-close" onClick={dismiss} aria-label="Dismiss reminder">
        <Icon name="close" size={13} />
      </button>
    </div>
  );
}
