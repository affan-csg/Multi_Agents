/** Shared Recharts styling for the Analytics dashboard. The same constants are
 *  still declared locally in older pages (Learning.tsx, the pipeline section of
 *  Analytics.tsx); they are intentionally not refactored here. */

export const AXIS = { stroke: "#8e96a8", fontSize: 12 };
export const GRID = "#e8eaee";

export const tooltipStyle = {
  background: "#fff",
  border: "1px solid #e8eaee",
  borderRadius: 10,
  boxShadow: "0 4px 14px rgba(20,23,41,.1)",
  fontSize: 13,
};

/** Metric colours, fixed so a metric looks the same on every chart. */
export const METRIC_COLORS = {
  sent: "#5145e8",
  replies: "#23c08b",
  positive_replies: "#0ea5a0",
  bookings: "#6f63f2",
  follow_ups: "#e8a33d",
  failed: "#e8734a",
};

/** One colour per sending account, assigned by position so a third (or
 *  fourth) mailbox gets its own colour with no code change. */
const SENDER_PALETTE = ["#5145e8", "#23c08b", "#e8a33d", "#4f7df3", "#e8734a", "#8b5cf6"];
export const UNKNOWN_COLOR = "#c9cdd8";

export function senderColor(index: number): string {
  return SENDER_PALETTE[index % SENDER_PALETTE.length];
}
