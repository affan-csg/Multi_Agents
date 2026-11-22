import type { CompanyRunOutcome } from "./agents/research";

export interface FormattedCompanyOutcome {
  tone: "info" | "error";
  message: string;
}

const REASON_LABEL: Record<string, string> = {
  qualified_in_prior_run: "already qualified in an earlier run",
  already_disqualified: "already disqualified",
  suppressed: "on the suppression list",
  already_settled: "already in a terminal research state",
  no_confirmed_job_signal: "no confirmed current job signal",
  no_open_postings: "no open postings were found",
  no_validated_current_postings: "no validated current postings were available",
  already_scored_in_this_run: "already scored in this run",
};

export function formatCompanyOutcome(outcome: CompanyRunOutcome): FormattedCompanyOutcome {
  const label = REASON_LABEL[outcome.reason] || outcome.reason.replaceAll("_", " ");
  if (outcome.outcome === "duplicate") {
    return { tone: "info", message: `${outcome.domain}: not reprocessed; ${label}.` };
  }
  if (outcome.outcome === "failed") {
    return {
      tone: "error",
      message: `${outcome.domain}: scoring failed${outcome.error ? ` (${outcome.error})` : ` (${label})`}.`,
    };
  }
  if (outcome.outcome === "scored" && outcome.job_data_available === false) {
    const jobReason = outcome.job_data_reason
      ? REASON_LABEL[outcome.job_data_reason] || outcome.job_data_reason.replaceAll("_", " ")
      : "no usable current job data";
    return { tone: "info", message: `${outcome.domain}: scored without usable job data (${jobReason}).` };
  }
  if (outcome.outcome === "disqualified") {
    return { tone: "info", message: `${outcome.domain}: disqualified (${label}).` };
  }
  if (outcome.outcome === "skipped") {
    return { tone: "info", message: `${outcome.domain}: skipped; ${label}.` };
  }
  return { tone: "info", message: `${outcome.domain}: score created.` };
}