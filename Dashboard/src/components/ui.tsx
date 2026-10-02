import type { CSSProperties, ReactNode } from "react";

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className={className}>
      {PATHS[name]}
    </svg>
  );
}

export type IconName = keyof typeof PATHS;

const PATHS = {
  home: <><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V20h14V9.5" /></>,
  play: <><path d="M6 4.5 19 12 6 19.5z" /></>,
  building: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M9 8h2M13 8h2M9 12h2M13 12h2M9 16h6" /></>,
  users: <><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5" /><path d="M17 8.2a3 3 0 0 1 0 5.6M18.5 20c0-2.3-1-4-2.5-4.9" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 15a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.1-2.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 9 4.6V4a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.8 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.8H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  filter: <><path d="M3 5h18l-7 8v6l-4 2v-8z" /></>,
  export: <><path d="M12 15V3" /><path d="m8 7 4-4 4 4" /><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" /></>,
  star: <><path d="m12 3.5 2.6 5.5 6 .8-4.3 4.3 1 6-5.3-2.9L6.7 20l1-6-4.3-4.3 6-.8z" /></>,
  tag: <><path d="M3 12V5a2 2 0 0 1 2-2h7l9 9-9 9z" /><circle cx="7.5" cy="7.5" r="1.3" /></>,
  chevronLeft: <><path d="m14 6-6 6 6 6" /></>,
  chevronRight: <><path d="m10 6 6 6-6 6" /></>,
  chevronDown: <><path d="m6 9 6 6 6-6" /></>,
  close: <><path d="M6 6 18 18M18 6 6 18" /></>,
  mail: <><rect x="2.5" y="5" width="19" height="14" rx="2" /><path d="m3 6.5 9 6.5 9-6.5" /></>,
  link: <><path d="M10 13.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.7 1.7" /><path d="M14 10.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.7-1.7" /></>,
  linkedin: <><rect x="3" y="3" width="18" height="18" rx="3" /><circle cx="8" cy="8.3" r="0.6" fill="currentColor" /><path d="M8 11v6M12.5 17v-3.5a2 2 0 0 1 4 0V17M12.5 17v-6M16.5 17v-3.5" /></>,
  pin: <><path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11z" /><circle cx="12" cy="10" r="2.6" /></>,
  briefcase: <><rect x="2.5" y="7" width="19" height="13" rx="2" /><path d="M8.5 7V5.5a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2V7" /></>,
  lightbulb: <><path d="M9.5 18h5" /><path d="M10 21h4" /><path d="M12 3a6 6 0 0 1 3.5 10.9c-.6.5-.9 1.2-1 1.9l-.1.7h-4.8l-.1-.7c-.1-.7-.4-1.4-1-1.9A6 6 0 0 1 12 3z" /></>,
  target: <><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="4.5" /><circle cx="12" cy="12" r="1" /></>,
  sparkle: <><path d="M12 3.5 13.6 9l5.4 1.6-5.4 1.6L12 17.6l-1.6-5.4L5 10.6 10.4 9z" /><path d="M18.5 3.5v3M20 5h-3" /></>,
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  bookmark: <><path d="M6 3.5h12v17l-6-4-6 4z" /></>,
  database: <><ellipse cx="12" cy="6" rx="8" ry="3.2" /><path d="M4 6v12c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2V6" /><path d="M4 12c0 1.8 3.6 3.2 8 3.2s8-1.4 8-3.2" /></>,
  refresh: <><path d="M20 12a8 8 0 1 1-2.3-5.6" /><path d="M20 3v5h-5" /></>,
  brain: <><path d="M9.5 3.5A3 3 0 0 0 6.6 6a3 3 0 0 0-2 4.6A3 3 0 0 0 6 15.4a3 3 0 0 0 3.5 4.1V3.5z" /><path d="M14.5 3.5A3 3 0 0 1 17.4 6a3 3 0 0 1 2 4.6A3 3 0 0 1 18 15.4a3 3 0 0 1-3.5 4.1V3.5z" /></>,
  send: <><path d="M21 3 10.5 13.5" /><path d="M21 3 14.5 21l-4-7.5L3 9.5z" /></>,
  list: <><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" /></>,
  flask: <><path d="M9 3v6.5L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L15 9.5V3" /><path d="M7.5 3h9M7 15h10" /></>,
  arrowRight: <><path d="M4 12h16" /><path d="m14 6 6 6-6 6" /></>,
  plus: <><path d="M12 5v14M5 12h14" /></>,
  check: <><path d="m5 12.5 4.5 4.5L19 7" /></>,
  crown: <><path d="m3 7 4 3.5L12 4l5 6.5L21 7l-1.6 12H4.6z" /></>,
  info: <><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5M12 8h.01" /></>,
  calendar: <><rect x="3.5" y="5" width="17" height="16" rx="2" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>,
  contact: <><circle cx="12" cy="8.5" r="3.3" /><path d="M5.5 20c0-3.4 2.9-5.8 6.5-5.8s6.5 2.4 6.5 5.8" /><path d="M15.5 5.5 17 4M15.5 11l1.5 1.5" /></>,
  analytics: <><path d="M4 20V12M9.3 20V7M14.6 20v-5.5M20 20V4" /><circle cx="20" cy="4" r="1.4" fill="currentColor" stroke="none" /></>,
  trash: <><path d="M4 7h16" /><path d="M9 7V4.5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1V7" /><path d="M6.5 7 7.3 19a2 2 0 0 0 2 1.9h5.4a2 2 0 0 0 2-1.9L17.5 7" /><path d="M10 11v6M14 11v6" /></>,
  more: <><circle cx="12" cy="5" r="1.4" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" /><circle cx="12" cy="19" r="1.4" fill="currentColor" stroke="none" /></>,
  edit: <><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" /></>,
  grip: <>
    <circle cx="9" cy="6" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="15" cy="6" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="9" cy="12" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="15" cy="12" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="9" cy="18" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="15" cy="18" r="1.2" fill="currentColor" stroke="none" />
  </>,
  eye: <><path d="M2 12s3.8-7 10-7 10 7 10 7-3.8 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" /></>,
  shieldCheck: <><path d="M12 3.5 5 6v6c0 4.6 3 7.6 7 8.5 4-.9 7-3.9 7-8.5V6z" /><path d="m9 12 2 2 4-4.5" /></>,
  ban: <><circle cx="12" cy="12" r="8.5" /><path d="m6 6 12 12" /></>,
  activity: <><path d="M3 12h4l2.5-7L14 19l2.5-7H21" /></>,
} as const;

const AVATAR_COLORS = [
  "linear-gradient(145deg,#5145e8,#3b2fd0)",
  "linear-gradient(145deg,#23c08b,#12a071)",
  "linear-gradient(145deg,#4f7df3,#2f5cd8)",
  "linear-gradient(145deg,#8b5cf6,#6d3ff0)",
  "linear-gradient(145deg,#e8734a,#d1552c)",
  "linear-gradient(145deg,#0ea5e9,#0284c7)",
  "linear-gradient(145deg,#ec4899,#db2777)",
];

export function Avatar({ name, size = "" }: { name: string; size?: "" | "sm" | "lg" }) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  const initial = (name.trim()[0] || "?").toUpperCase();
  return (
    <span
      className={`avatar${size ? ` avatar--${size}` : ""}`}
      style={{ background: AVATAR_COLORS[hash % AVATAR_COLORS.length] }}
      aria-hidden="true"
    >
      {initial}
    </span>
  );
}

const ATS_COLORS: Record<string, string> = {
  greenhouse: "#23c08b",
  lever: "#5145e8",
  ashby: "#8b5cf6",
  workday: "#f0ac3f",
  smartrecruiters: "#e0563f",
  workable: "#2d9cdb",
  bamboohr: "#7dc242",
  rippling: "#c94f7c",
};

export function AtsBadge({ provider }: { provider: string | null }) {
  if (!provider) return <span className="cell-muted">—</span>;
  return (
    <span className="ats-badge">
      <span className="ats-dot" style={{ background: ATS_COLORS[provider.toLowerCase()] || "#8e96a8" }} />
      {provider}
    </span>
  );
}

export function ScorePill({ score }: { score: number }) {
  const level = score >= 75 ? "high" : score >= 55 ? "mid" : "low";
  return <span className={`score-pill score-pill--${level}`}>{score.toFixed(0)}</span>;
}

export function StatCard({
  icon,
  tone,
  value,
  label,
  compact,
}: {
  icon: IconName;
  tone: "accent" | "mint" | "violet" | "amber";
  value: ReactNode;
  label: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`stat-card${compact ? " stat-card--compact" : ""}`}>
      <span className={`stat-icon stat-icon--${tone}`}>
        <Icon name={icon} size={compact ? 15 : 19} />
      </span>
      <div>
        <div className="stat-value">{value}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}

export function EmptyState({ icon, message }: { icon: IconName; message: string }) {
  return (
    <div className="empty-state">
      <Icon name={icon} size={34} />
      <p>{message}</p>
    </div>
  );
}

/** A single shimmering placeholder bar/block. Width/height accept any CSS
 * length; `circle` makes it round (for avatar placeholders). */
export function Skeleton({
  width = "100%",
  height = 14,
  circle = false,
  style,
}: {
  width?: number | string;
  height?: number | string;
  circle?: boolean;
  style?: CSSProperties;
}) {
  return (
    <span
      className="skeleton"
      style={{ width, height, borderRadius: circle ? "50%" : undefined, ...style }}
    />
  );
}

export function StatCardSkeleton() {
  return (
    <div className="stat-card">
      <Skeleton width={40} height={40} style={{ borderRadius: 12, flexShrink: 0 }} />
      <div style={{ flex: 1 }}>
        <Skeleton width="55%" height={22} style={{ marginBottom: 8 }} />
        <Skeleton width="75%" height={12} />
      </div>
    </div>
  );
}

/** Mimics a chart card's title + a chart-shaped placeholder (bars of
 * varying random-ish height, so it reads as "a chart is coming," not just a
 * gray box) -- this is the skeleton language from the reference design. */
export function ChartCardSkeleton({ tall = false }: { tall?: boolean }) {
  const bars = [40, 70, 55, 90, 65, 45, 80];
  return (
    <div className="card">
      <Skeleton width="45%" height={16} style={{ marginBottom: 8 }} />
      <Skeleton width="65%" height={12} style={{ marginBottom: 20 }} />
      <div className="skeleton-chart" style={{ height: tall ? 300 : 260 }}>
        {bars.map((h, i) => (
          <span key={i} className="skeleton skeleton-chart-bar" style={{ height: `${h}%` }} />
        ))}
      </div>
    </div>
  );
}

export function SavedCardSkeleton() {
  return (
    <div className="saved-card saved-card--skeleton">
      <div className="saved-card-head">
        <Skeleton width={36} height={36} circle style={{ flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <Skeleton width="70%" height={14} style={{ marginBottom: 6 }} />
          <Skeleton width="45%" height={12} />
        </div>
      </div>
      <div className="saved-card-meta">
        <Skeleton width={64} height={22} style={{ borderRadius: 999 }} />
        <Skeleton width={90} height={22} style={{ borderRadius: 999 }} />
      </div>
      <div className="saved-card-foot">
        <Skeleton width={70} height={22} />
        <Skeleton width={80} height={12} />
      </div>
    </div>
  );
}

export function TableRowsSkeleton({ columns = 6, rows = 6 }: { columns?: number; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="skeleton-row">
          {Array.from({ length: columns }).map((_, c) => (
            <td key={c}>
              <Skeleton width={c === 0 ? "80%" : "60%"} height={13} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

/** Full company-detail page skeleton -- mirrors CompanyDetail's real layout
 * (header + action row + 3-card grid + requisitions table) so the page
 * doesn't flash to a blank/plain state while its detail fetch is in flight. */
export function CompanyDetailSkeleton() {
  return (
    <>
      <Skeleton width={120} height={14} style={{ marginBottom: 20 }} />

      <div className="detail-head">
        <Skeleton width={56} height={56} circle style={{ flexShrink: 0 }} />
        <div className="detail-head-text">
          <Skeleton width={220} height={24} style={{ marginBottom: 10 }} />
          <div style={{ display: "flex", gap: 8 }}>
            <Skeleton width={70} height={22} style={{ borderRadius: 999 }} />
            <Skeleton width={110} height={22} style={{ borderRadius: 999 }} />
            <Skeleton width={90} height={22} style={{ borderRadius: 999 }} />
          </div>
        </div>
      </div>

      <div className="action-row" style={{ marginBottom: 24 }}>
        <Skeleton width={140} height={36} style={{ borderRadius: 8 }} />
        <Skeleton width={130} height={36} style={{ borderRadius: 8 }} />
        <Skeleton width={90} height={36} style={{ borderRadius: 8 }} />
      </div>

      <div className="detail-grid">
        {Array.from({ length: 3 }).map((_, i) => (
          <div className="card" key={i}>
            <Skeleton width="55%" height={16} style={{ marginBottom: 8 }} />
            <Skeleton width="80%" height={12} style={{ marginBottom: 20 }} />
            <Skeleton width="100%" height={100} style={{ borderRadius: 10 }} />
          </div>
        ))}
      </div>

      <div className="card">
        <Skeleton width="35%" height={16} style={{ marginBottom: 20 }} />
        <TableSkeletonBlock columns={4} rows={5} />
      </div>
    </>
  );
}

/** Compact skeleton for the company side-drawer -- narrower than the full
 * CompanyDetailSkeleton, mirroring CompanyDrawer's head + score summary +
 * dimension bars layout. */
export function DrawerSkeleton() {
  return (
    <>
      <div className="drawer-head">
        <Skeleton width={40} height={40} circle style={{ flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <Skeleton width="60%" height={16} style={{ marginBottom: 8 }} />
          <Skeleton width="40%" height={12} />
        </div>
      </div>

      <div className="drawer-meta">
        <Skeleton width={64} height={22} style={{ borderRadius: 999 }} />
        <Skeleton width={100} height={22} style={{ borderRadius: 999 }} />
      </div>

      <div className="score-summary">
        <Skeleton width={48} height={48} circle style={{ flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <Skeleton width="50%" height={13} style={{ marginBottom: 6 }} />
          <Skeleton width="75%" height={12} />
        </div>
      </div>

      <div className="dim-list" style={{ marginTop: 16 }}>
        {Array.from({ length: 4 }).map((_, i) => (
          <div className="dim-item" key={i}>
            <div className="dim-item-head">
              <Skeleton width="40%" height={12} />
              <Skeleton width={24} height={12} />
            </div>
            <Skeleton width="100%" height={8} style={{ borderRadius: 999 }} />
          </div>
        ))}
      </div>
    </>
  );
}

function TableSkeletonBlock({ columns, rows }: { columns: number; rows: number }) {
  return (
    <div>
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} style={{ display: "flex", gap: 20, padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
          {Array.from({ length: columns }).map((_, c) => (
            <Skeleton key={c} width={c === 0 ? "30%" : "18%"} height={13} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "1 day ago";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return months === 1 ? "1 month ago" : `${months} months ago`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
