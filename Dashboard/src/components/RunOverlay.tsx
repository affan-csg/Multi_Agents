import { Icon } from "./ui";

export interface GenericJobSnapshot {
  job_id: string;
  kind: string;
  status: "pending" | "running" | "done" | "error";
  done: number;
  total: number;
  current_domain: string | null;
}

const KIND_LABEL: Record<string, string> = {
  discover: "Discovering companies",
  qualify: "Qualifying prospects",
  contacts: "Finding contacts",
  import: "Importing prospects",
  send: "Sending outreach",
};

const KIND_SUB: Record<string, string> = {
  discover: "Searching the region, researching firmographics, and scoring against the ICP.",
  qualify: "Summarizing pain points and outreach angles with the LLM.",
  contacts: "Looking up decision-makers via Hunter.io.",
  import: "Pulling qualified prospects from the Research Agent.",
  send: "Drafting and sending personalized email to each prospect.",
};

// Research's `discover` job is the only one with a fixed multi-stage
// pipeline underneath it (see pipeline.py _process_one) -- other job kinds
// (qualify/contacts/import/send) are a flat per-item loop, so only discover
// gets the step tracker.
const RUN_STEPS = [
  { key: "detect", label: "ATS detection" },
  { key: "sync", label: "Job sync" },
  { key: "score", label: "ICP scoring" },
];

// api.py's job progress callback prefixes current_domain with the backend
// stage as "stage: detail" (see RunConfig.tsx's identical parsing for the
// 7-step pipeline strip) -- mirror that mapping here so the overlay names
// the actual phase instead of just repeating the job kind for the whole run.
const STAGE_LABEL: Record<string, string> = {
  discover: "Company Discovery",
  enrich: "Firmographic Research",
  pipeline: "ATS Detection & Scoring",
  qualify: "Prospect Qualification",
  contacts: "Contact Research",
  outreach: "Campaign Creation",
};

function currentlyRunningLabel(job: GenericJobSnapshot): string | null {
  const backendStage = job.current_domain?.split(":")[0]?.trim();
  if (backendStage && STAGE_LABEL[backendStage]) return STAGE_LABEL[backendStage];
  if (job.kind === "qualify" || job.kind === "contacts") return "Prospect Research";
  return null;
}

export function RunOverlay({ job }: { job: GenericJobSnapshot }) {
  const pct = job.total > 0 ? Math.round((job.done / job.total) * 100) : job.status === "pending" ? 0 : 100;
  const circumference = 2 * Math.PI * 26;
  const offset = circumference * (1 - pct / 100);

  const stepIndex = job.status === "pending" ? 0 : pct >= 100 ? 3 : pct > 0 ? 1 + Math.floor((pct / 100) * 2) : 0;
  const runningLabel = currentlyRunningLabel(job);

  return (
    <div className="run-overlay-wrap">
      <div className="run-overlay-card">
        <div className="run-overlay-ring">
          <svg width="64" height="64" viewBox="0 0 64 64">
            <circle cx="32" cy="32" r="26" fill="none" stroke="var(--border)" strokeWidth="6" />
            <circle
              cx="32"
              cy="32"
              r="26"
              fill="none"
              stroke="url(#run-overlay-grad)"
              strokeWidth="6"
              strokeLinecap="round"
              strokeDasharray={circumference}
              strokeDashoffset={offset}
              transform="rotate(-90 32 32)"
              style={{ transition: "stroke-dashoffset 0.5s ease" }}
            />
            <defs>
              <linearGradient id="run-overlay-grad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#5145e8" />
                <stop offset="100%" stopColor="#8b5cf6" />
              </linearGradient>
            </defs>
          </svg>
          <span className="run-overlay-pct">{pct}%</span>
        </div>

        <div className="run-overlay-body">
          <div className="run-overlay-title">{KIND_LABEL[job.kind] || job.kind}</div>
          <p className="run-overlay-sub">{KIND_SUB[job.kind] || ""}</p>

          {runningLabel && <p className="run-overlay-status">Currently running: {runningLabel}</p>}

          {job.kind === "discover" && (
            <div className="run-overlay-steps">
              {RUN_STEPS.map((step, i) => {
                const state = i < stepIndex ? "done" : i === stepIndex ? "active" : "pending";
                return (
                  <div className="run-overlay-step" key={step.key}>
                    <span className={`run-overlay-step-dot run-overlay-step-dot--${state}`}>
                      {state === "done" ? <Icon name="check" size={12} /> : null}
                    </span>
                    <span className={`run-overlay-step-label run-overlay-step-label--${state}`}>{step.label}</span>
                    {i < RUN_STEPS.length - 1 && <span className="run-overlay-step-line" />}
                  </div>
                );
              })}
            </div>
          )}

          {job.total > 0 && (
            <div className="run-overlay-count">
              {job.done} of {job.total}
              {job.current_domain ? ` — ${job.current_domain}` : ""}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
