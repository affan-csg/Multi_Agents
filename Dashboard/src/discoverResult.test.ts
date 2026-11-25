import { describe, expect, it } from "vitest";
import { describeDiscoverResult } from "./discoverResult";

describe("describeDiscoverResult", () => {
  it("keeps partial and unavailable field results informational", () => {
    const result = describeDiscoverResult({
      discovered: 2,
      scored: 2,
      enrichment_summary: {
        "partial.example": {
          status: "partial",
          missing_fields: { employee_range: "Gemini omitted the field" },
          blocking_fields: { employee_range: "Gemini omitted the field" },
        },
        "unknown.example": {
          status: "complete_with_gaps",
          missing_fields: { funding: "No reliable information was returned" },
          blocking_fields: {},
        },
        "review.example": {
          status: "needs_review",
          missing_fields: { revenue: "Source URL missing" },
          blocking_fields: { revenue: "Source URL missing" },
          review_fields: { revenue: "Source URL missing" },
        },
      },
    });

    expect(result.tone).toBe("success");
    expect(result.message).toContain("partially researched");
    expect(result.message).toContain("completed with unavailable fields");
    expect(result.message).toContain("needs review");
    // Field-validation text stays in field-level details/diagnostics, never
    // in the run summary (see prompt.txt section 3).
    expect(result.message).not.toContain("Source URL missing");
    expect(result.message).not.toContain("failed");
  });

  it("does not treat a historical field-level failure as an operation failure", () => {
    const result = describeDiscoverResult({
      discovered: 1,
      scored: 1,
      enrich_failures: { incomplete_result: 1, ungrounded_response: 1 },
      enrichment_summary: {
        "old.example": {
          status: "failed",
          failure_reason: "incomplete_result",
          missing_fields: { funding: "No reliable information was returned" },
          blocking_fields: {},
        },
      },
    });

    expect(result.tone).toBe("success");
    expect(result.message).toContain("partially researched");
    expect(result.message).not.toContain("operation failed");
    expect(result.message).not.toContain("incomplete_result");
  });

  it("reports a genuine Gemini quota failure as an error", () => {
    const result = describeDiscoverResult({
      discovered: 1,
      scored: 1,
      enrich_failures: { quota_exhausted: 1 },
      enrichment_summary: {
        "quota.example": {
          status: "failed",
          missing_fields: {},
          blocking_fields: {},
          failure_reason: "quota_exhausted",
        },
      },
    });

    expect(result.tone).toBe("error");
    expect(result.message).toContain("quota limit");
    expect(result.message).toContain("Enrichment did not finish");
  });

  it("presents the reported mixed run without internal validation codes", () => {
    const result = describeDiscoverResult({
      discovered: 4,
      scored: 4,
      // Shape produced by Research's present_discover_result: field-level
      // rejection is already needs_review, only a real timeout is failed.
      enrichment_summary: {
        "wiz.io": { status: "needs_review", missing_fields: {}, blocking_fields: {}, review_fields: {} },
        "snyk.io": { status: "needs_review", missing_fields: {}, blocking_fields: {}, review_fields: {} },
        "acme.io": { status: "needs_review", missing_fields: {}, blocking_fields: {}, review_fields: {} },
        "slow.io": { status: "failed", failure_reason: "timeout", missing_fields: {}, blocking_fields: {} },
      },
      enrich_failures: { timeout: 1 },
    });

    expect(result.tone).toBe("error");
    expect(result.message).toContain("4 companies discovered");
    expect(result.message).toContain("4 scored");
    expect(result.message).toContain("3 need review");
    expect(result.message).toContain("request timed out");
    expect(result.message).not.toContain("ungrounded_response");
    expect(result.message).not.toContain("supporting URL");
    expect(result.message).not.toContain("failed;");
  });
});