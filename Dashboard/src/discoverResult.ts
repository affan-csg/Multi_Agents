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
  complete_with_gaps: "completed with unavailable fields",
  partial: "partially researched",
  needs_review: "needs review",
  failed: "failed during enrichment",
};

/** Older runs stored finished field research as an enrichment failure. */
const FIELD_LEVEL_ENRICH_FAILURES: Record<string, string> = {
  incomplete_result: "partial",
  ungrounded_response: "needs_review",
  complete_with_gaps: "complete_with_gaps",
  partial: "partial",
  needs_review: "needs_review",
};

function presentedEnrichmentStatus(status: string, failureReason?: string): string {
  if (status === "failed" && failureReason && FIELD_LEVEL_ENRICH_FAILURES[failureReason]) {
    return FIELD_LEVEL_ENRICH_FAILURES[failureReason];
  }
  return status;
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
    for (const [, item] of entries) {
      const status = presentedEnrichmentStatus(item.status, item.failure_reason);
      counts.set(status, (counts.get(status) || 0) + 1);
    }
    for (const status of ["complete", "partial", "needs_review", "complete_with_gaps"] as const) {
      const count = counts.get(status) || 0;
      if (!count) continue;
      const label = status === "complete" ? "fully researched" : ENRICHMENT_STATUS_LABEL[status];
      parts.push(`${count} ${label}`);
    }

    const attention = entries.filter(([, item]) => presentedEnrichmentStatus(item.status, item.failure_reason) !== "complete");
    const details = attention.slice(0, 8).map(([domain, item]) => {
      const fieldReasons = Object.entries({
        ...(item.missing_fields || {}),
        ...(item.review_fields || {}),
      }).map(([field, reason]) => `${field}: ${reason}`);
      const statusKey = presentedEnrichmentStatus(item.status, item.failure_reason);
      const status = ENRICHMENT_STATUS_LABEL[statusKey] || statusKey;
      return `${domain} (${status}${fieldReasons.length ? `; ${fieldReasons.join(", ")}` : ""})`;
    });
    if (attention.length > details.length) {
      details.push(`${attention.length - details.length} more company result(s) in run details`);
    }
    if (details.length) parts.push(`Firmographic research: ${details.join(" | ")}`);
    const failed = entries.filter(([, item]) => presentedEnrichmentStatus(item.status, item.failure_reason) === "failed");
    if (failed.length > 0) {
      problems.push(`${failed.length} enrichment operation(s) failed; see provider details`);
    }
  } else {
    if (run.enrich_partial) parts.push(`${run.enrich_partial} partially researched`);
    if (run.enrich_needs_review) parts.push(`${run.enrich_needs_review} need review`);
    if (run.enrich_complete_with_gaps) {
      parts.push(`${run.enrich_complete_with_gaps} completed with unavailable fields`);
    }
  }

  if (run.enrich_failures && typeof run.enrich_failures === "object") {
    const operational: [string, number][] = [];
    const legacyFieldResults: [string, number][] = [];
    for (const [reason, count] of Object.entries(run.enrich_failures)) {
      if (!count) continue;
      if (FIELD_LEVEL_ENRICH_FAILURES[reason]) legacyFieldResults.push([reason, count]);
      else operational.push([reason, count]);
    }
    if (operational.length > 0) {
      const total = operational.reduce((sum, [, count]) => sum + count, 0);
      const reasons = operational.map(([reason, count]) => `${ENRICH_FAILURE_LABEL[reason] || reason} (${count})`);
      problems.push(`Enrichment operation failed for ${total} compan${total === 1 ? "y" : "ies"}: ${reasons.join(", ")}`);
    }
    if (legacyFieldResults.length > 0 && entries.length === 0) {
      for (const [reason, count] of legacyFieldResults) {
        const status = FIELD_LEVEL_ENRICH_FAILURES[reason];
        parts.push(`${count} ${ENRICHMENT_STATUS_LABEL[status] || status}`);
      }
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