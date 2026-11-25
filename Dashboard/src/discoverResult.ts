import type { RunResult } from "./agents/research";
import { formatCompanyOutcome } from "./discoverOutcome";

export interface DiscoverResultDescription {
  tone: "success" | "error";
  message: string;
}

const ENRICH_FAILURE_LABEL: Record<string, string> = {
  no_api_key: "no GEMINI_API_KEY configured",
  quota_exhausted: "a provider quota limit was reached",
  rate_limited: "rate limited (429); retries exhausted",
  connection_error: "connection/TLS error reaching the API",
  timeout: "request timed out",
  parse_error: "the provider response could not be parsed",
  request_error: "the provider request failed",
};

const ENRICHMENT_STATUS_LABEL: Record<string, string> = {
  complete: "fully researched",
  complete_with_gaps: "completed with unavailable fields",
  partial: "partially researched",
  needs_review: "need review",
  research_incomplete: "research incomplete",
  failed: "did not finish",
};

const COMPANY_STATUS_LABEL: Record<string, string> = {
  complete: "fully researched",
  complete_with_gaps: "completed with unavailable fields",
  partial: "partially researched",
  needs_review: "needs review",
  research_incomplete: "research incomplete",
  failed: "did not finish",
};

const OPERATIONAL_ENRICH_FAILURES = new Set([
  "no_api_key", "quota_exhausted", "rate_limited", "connection_error",
  "timeout", "parse_error", "request_error", "interrupted",
]);

const VALIDATION_MARKERS = [
  "supporting url", "conflicting claims", "grounding",
  "could not be verified", "not present in provider", "excerpt",
];

function summaryDetail(item: { missing_fields?: Record<string, string>; review_fields?: Record<string, string>; blocking_fields?: Record<string, string> }): string {
  return Object.values({
    ...(item.missing_fields || {}),
    ...(item.review_fields || {}),
    ...(item.blocking_fields || {}),
  }).join(" ").toLowerCase();
}

export function presentedEnrichmentStatus(status?: string, failureReason?: string, detail = ""): string {
  if (!status) return "";
  if (status === "failed" && failureReason && OPERATIONAL_ENRICH_FAILURES.has(failureReason)) return "failed";
  if (status !== "failed") return status;
  const text = detail.toLowerCase();
  if (VALIDATION_MARKERS.some((marker) => text.includes(marker))) return "needs_review";
  if (failureReason === "incomplete_result" || failureReason === "partial") return "partial";
  if (failureReason === "needs_review") return "needs_review";
  if (failureReason === "complete_with_gaps") return "complete_with_gaps";
  return "research_incomplete";
}

export function describeDiscoverResult(
  result: RunResult | Record<string, unknown> | null,
): DiscoverResultDescription {
  const run = (result || {}) as Partial<RunResult>;
  const parts: string[] = [];
  const problems: string[] = [];

  if (typeof run.discovered === "number") {
    parts.push(`${run.discovered} compan${run.discovered === 1 ? "y" : "ies"} discovered`);
  }
  if (typeof run.scored === "number") parts.push(`${run.scored} scored`);
  if (run.tiers) {
    const ab = (run.tiers.A ?? 0) + (run.tiers.B ?? 0);
    if (ab > 0) parts.push(`${ab} Tier A/B`);
  }

  const enrichmentSummary = run.enrichment_summary;
  const entries = enrichmentSummary && typeof enrichmentSummary === "object"
    ? Object.entries(enrichmentSummary)
    : [];
  if (entries.length > 0) {
    const counts = new Map<string, number>();
    const presented = entries.map(([domain, item]) => {
      const status = presentedEnrichmentStatus(item.status, item.failure_reason, summaryDetail(item));
      counts.set(status, (counts.get(status) || 0) + 1);
      return [domain, item, status] as const;
    });
    for (const status of ["complete", "partial", "needs_review", "complete_with_gaps", "research_incomplete"] as const) {
      const count = counts.get(status) || 0;
      if (count) parts.push(`${count} ${ENRICHMENT_STATUS_LABEL[status]}`);
    }
    const attention = presented.filter(([, , status]) => status !== "complete" && status !== "failed");
    const details = attention.slice(0, 8).map(([domain, , status]) => (
      `${domain}: ${COMPANY_STATUS_LABEL[status] || status}`
    ));
    if (attention.length > details.length) {
      details.push(`${attention.length - details.length} more company result(s) in run details`);
    }
    if (details.length) parts.push(details.join("; "));
    const failed = presented.filter(([, , status]) => status === "failed");
    if (failed.length > 0) {
      const reasons = failed.map(([domain, item]) => (
        `${domain} (${ENRICH_FAILURE_LABEL[item.failure_reason || ""] || "enrichment did not finish"})`
      ));
      problems.push(`Enrichment did not finish for ${failed.length} compan${failed.length === 1 ? "y" : "ies"}: ${reasons.join(", ")}`);
    }
  } else {
    if (run.enrich_partial) parts.push(`${run.enrich_partial} partially researched`);
    if (run.enrich_needs_review) parts.push(`${run.enrich_needs_review} need review`);
    if (run.enrich_complete_with_gaps) {
      parts.push(`${run.enrich_complete_with_gaps} completed with unavailable fields`);
    }
    if (run.enrich_research_incomplete) {
      parts.push(`${run.enrich_research_incomplete} research incomplete`);
    }
  }

  if (entries.length === 0 && run.enrich_failures && typeof run.enrich_failures === "object") {
    const operational: [string, number][] = [];
    let review = 0;
    let partial = 0;
    let incomplete = 0;
    for (const [reason, count] of Object.entries(run.enrich_failures)) {
      if (!count) continue;
      if (OPERATIONAL_ENRICH_FAILURES.has(reason)) operational.push([reason, count]);
      else if (reason === "incomplete_result" || reason === "partial") partial += count;
      else if (reason === "needs_review" || reason === "ungrounded_response") review += count;
      else incomplete += count;
    }
    if (partial) parts.push(`${partial} partially researched`);
    if (review) parts.push(`${review} need review`);
    if (incomplete) parts.push(`${incomplete} research incomplete`);
    if (operational.length > 0) {
      const total = operational.reduce((sum, [, count]) => sum + count, 0);
      const reasons = operational.map(([reason, count]) => `${ENRICH_FAILURE_LABEL[reason] || "enrichment did not finish"} (${count})`);
      problems.push(`Enrichment did not finish for ${total} compan${total === 1 ? "y" : "ies"}: ${reasons.join(", ")}`);
    }
  }

  const companyOutcomes = Array.isArray(run.company_outcomes) ? run.company_outcomes : [];
  if (companyOutcomes.length > 0) {
    for (const outcome of companyOutcomes.slice(0, 5)) {
      const formatted = formatCompanyOutcome(outcome);
      (formatted.tone === "error" ? problems : parts).push(formatted.message);
    }
    if (companyOutcomes.length > 5) {
      parts.push(`${companyOutcomes.length - 5} more company outcomes in run details`);
    }
  } else if (run.existing_matches?.length) {
    parts.push(`${run.existing_matches.length} existing compan${run.existing_matches.length === 1 ? "y" : "ies"} matched and not reprocessed`);
  } else if (run.scored === 0 && (run.discovered ?? 0) > 0) {
    problems.push("No per-company processing outcomes were returned; inspect the run records for the precise reason.");
  }

  if (typeof run.hiring_signal_unknown === "number" && typeof run.scored === "number" && run.scored > 0) {
    const total = run.scored + (run.disqualified ?? 0);
    if (total > 0 && run.hiring_signal_unknown / total >= 0.5) {
      parts.push(
        `${run.hiring_signal_unknown} of ${total} compan${total === 1 ? "y" : "ies"} had no confirmed job signal after ATS, careers-page, and web checks; this does not establish poor company fit.`,
      );
    }
  }

  const qualify = run.qualify as { qualified: number; errors: number; error?: string } | undefined;
  const contacts = run.contacts as { found: number; contacts_saved: number; error?: string } | undefined;
  if (qualify && "error" in qualify && qualify.error) {
    problems.push(`Qualify step failed: ${qualify.error}`);
  } else if (qualify?.qualified) {
    parts.push(`${qualify.qualified} qualified`);
    if (qualify.errors) problems.push(`${qualify.errors} qualification lookup(s) failed`);
  }
  if (contacts && "error" in contacts && contacts.error) {
    problems.push(`Contacts step failed: ${contacts.error}`);
  } else if (contacts?.contacts_saved) {
    parts.push(`${contacts.contacts_saved} contact(s) found`);
  }

  const summary = parts.length ? parts.join(" · ") : "Run completed.";
  return problems.length
    ? { tone: "error", message: `${summary}\n${problems.join("\n")}` }
    : { tone: "success", message: summary };
}