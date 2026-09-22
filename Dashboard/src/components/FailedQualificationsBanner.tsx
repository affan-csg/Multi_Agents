import { useState } from "react";
import { researchApi, type ScoredCompany } from "../agents/research";
import { useCachedFetch, invalidate } from "../hooks/useCachedFetch";
import { Icon } from "./ui";

interface Props {
  runId: string | null;
  onRetryStarted: (jobId: string, total: number) => void;
}

/** Surfaces "N qualifications failed" for the active run with a one-click
 *  retry -- the requirement that a failed AI qualification must never be
 *  silently indistinguishable from "complete" and must be retryable. Only
 *  renders when there is something to retry; a run with zero failures shows
 *  nothing here (see Research_Agent's qualification_attempts table). */
export function FailedQualificationsBanner({ runId, onRetryStarted }: Props) {
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data } = useCachedFetch<ScoredCompany[]>(
    runId && runId !== "all" ? `qualify-failed:${runId}` : null,
    () => researchApi.listFailedQualifications(runId as string),
  );
  const failed = data ?? [];

  if (!runId || runId === "all" || failed.length === 0) return null;

  async function handleRetry() {
    if (!runId) return;
    setRetrying(true);
    setError(null);
    try {
      const resp = await researchApi.retryFailedQualifications(runId);
      if (resp.job_id) {
        onRetryStarted(resp.job_id, resp.companies || failed.length);
        invalidate(`qualify-failed:${runId}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div className="qualify-failed-banner">
      <Icon name="info" size={16} />
      <span>
        {failed.length} qualification{failed.length === 1 ? "" : "s"} failed for this run and{" "}
        {failed.length === 1 ? "wasn't" : "weren't"} silently marked complete.
      </span>
      <button type="button" className="ghost" onClick={handleRetry} disabled={retrying} style={{ marginLeft: "auto" }}>
        <Icon name="refresh" size={13} />
        {retrying ? "Retrying…" : "Retry failed"}
      </button>
      {error && <p className="error-text" style={{ width: "100%", margin: "6px 0 0" }}>{error}</p>}
    </div>
  );
}
