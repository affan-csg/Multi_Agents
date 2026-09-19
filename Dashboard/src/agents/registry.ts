/** Registry of connected agents: name + base URL only. Adding a new agent
 *  (Marketing, Follow-up, ...) later means adding one entry here plus its
 *  own typed client module (see research.ts / outreach.ts) -- never editing
 *  another agent's code. Each agent keeps its own database; the Dashboard
 *  never talks to a database directly, only to each agent's own HTTP API,
 *  the same way each agent's own frontend does. */

export interface AgentInfo {
  id: string;
  label: string;
  baseUrl: string;
}

// Vite env vars (VITE_*) let each deployment point at different hosts/ports
// without a code change -- see .env.example.
export const AGENTS: AgentInfo[] = [
  {
    id: "research",
    label: "Research Agent",
    baseUrl: import.meta.env.VITE_RESEARCH_API_BASE || "http://127.0.0.1:8000",
  },
  {
    id: "outreach",
    label: "Outreach Agent",
    baseUrl: import.meta.env.VITE_OUTREACH_API_BASE || "http://127.0.0.1:8001",
  },
];

export function agentBaseUrl(id: string): string {
  return AGENTS.find((a) => a.id === id)?.baseUrl ?? "";
}
