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

// VITE_API_URL is the backend origin. Local development defaults to the
// combined local API server; production must provide an explicit backend.
const API_URL = import.meta.env.VITE_API_URL?.replace(/\/+$/, "") ||
  (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");

if (!API_URL) {
  throw new Error("VITE_API_URL must be configured for production builds.");
}

export const AGENTS: AgentInfo[] = [
  {
    id: "research",
    label: "Research Agent",
    baseUrl: `${API_URL}/research`,
  },
  {
    id: "outreach",
    label: "Outreach Agent",
    baseUrl: `${API_URL}/outreach`,
  },
];

export function agentBaseUrl(id: string): string {
  return AGENTS.find((a) => a.id === id)?.baseUrl ?? "";
}
