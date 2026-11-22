import { describe, expect, it } from "vitest";
import { formatCompanyOutcome } from "./discoverOutcome";
import type { CompanyRunOutcome } from "./agents/research";

describe("formatCompanyOutcome", () => {
  it("reports an already-qualified duplicate without calling it a scoring failure", () => {
    expect(formatCompanyOutcome({
      domain: "acme.example",
      outcome: "duplicate",
      reason: "qualified_in_prior_run",
      matched_by: "domain",
    })).toEqual({
      tone: "info",
      message: "acme.example: not reprocessed; already qualified in an earlier run.",
    });
  });

  it("distinguishes a score with missing job data from a skipped score", () => {
    const outcome: CompanyRunOutcome = {
      domain: "acme.example",
      outcome: "scored",
      reason: "score_created",
      job_data_available: false,
      job_data_reason: "no_confirmed_job_signal",
    };
    expect(formatCompanyOutcome(outcome)).toEqual({
      tone: "info",
      message: "acme.example: scored without usable job data (no confirmed current job signal).",
    });
  });

  it("shows the actual scoring exception", () => {
    expect(formatCompanyOutcome({
      domain: "acme.example",
      outcome: "failed",
      reason: "pipeline_error",
      error: "ValueError: invalid score input",
    })).toEqual({
      tone: "error",
      message: "acme.example: scoring failed (ValueError: invalid score input).",
    });
  });

  it("reports successful scoring directly", () => {
    expect(formatCompanyOutcome({
      domain: "acme.example",
      outcome: "scored",
      reason: "score_created",
      job_data_available: true,
    })).toEqual({ tone: "info", message: "acme.example: score created." });
  });
});