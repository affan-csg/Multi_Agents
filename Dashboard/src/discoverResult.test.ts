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
    expect(result.message).toContain("Source URL missing");
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
    expect(result.message).toContain("operation failed");
  });
});