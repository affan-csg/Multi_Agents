import type { JobSnapshot } from "./agents/research";

export const ACTIVE_RESEARCH_JOB_KEY = "csg.activeResearchJobId";

export function getStoredActiveResearchJobId(
  storage: Pick<Storage, "getItem">,
): string | null {
  return storage.getItem(ACTIVE_RESEARCH_JOB_KEY);
}

type JobMonitorApi = {
  jobStatus: (jobId: string) => Promise<JobSnapshot>;
  jobSocket: (jobId: string, onMessage: (snapshot: JobSnapshot) => void) => WebSocket;
};

type JobMonitorOptions = {
  pollIntervalMs?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  setTimeoutFn?: typeof window.setTimeout;
  clearTimeoutFn?: typeof window.clearTimeout;
  setIntervalFn?: typeof window.setInterval;
  clearIntervalFn?: typeof window.clearInterval;
};

const isTerminal = (status: JobSnapshot["status"]) => status === "done" || status === "error";

export class JobMonitor {
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private reconnectAttempt = 0;
  private stopped = false;
  private connecting = false;
  private readonly setTimeoutFn: typeof setTimeout;
  private readonly clearTimeoutFn: typeof clearTimeout;
  private readonly setIntervalFn: typeof setInterval;
  private readonly clearIntervalFn: typeof clearInterval;
  private readonly pollIntervalMs: number;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly jobId: string;
  private readonly api: JobMonitorApi;
  private readonly onSnapshot: (snapshot: JobSnapshot) => void;
  private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;

  constructor(
    jobId: string,
    api: JobMonitorApi,
    onSnapshot: (snapshot: JobSnapshot) => void,
    storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
    options: JobMonitorOptions = {},
  ) {
    this.jobId = jobId;
    this.api = api;
    this.onSnapshot = onSnapshot;
    this.storage = storage;
    this.pollIntervalMs = options.pollIntervalMs ?? 3000;
    this.reconnectBaseMs = options.reconnectBaseMs ?? 1000;
    this.reconnectMaxMs = options.reconnectMaxMs ?? 30000;
    this.setTimeoutFn = options.setTimeoutFn ?? setTimeout;
    this.clearTimeoutFn = options.clearTimeoutFn ?? clearTimeout;
    this.setIntervalFn = options.setIntervalFn ?? setInterval;
    this.clearIntervalFn = options.clearIntervalFn ?? clearInterval;
  }

  start(): void {
    if (this.stopped) return;
    console.info("research:monitor:start", { jobId: this.jobId, activeStorage: this.storage.getItem(ACTIVE_RESEARCH_JOB_KEY) });
    this.storage.setItem(ACTIVE_RESEARCH_JOB_KEY, this.jobId);
    void this.connectWithSync();
  }

  stop(): void {
    this.stopped = true;
    this.clearReconnectTimer();
    this.stopPolling();
    this.socket?.close();
    this.socket = null;
  }

  private async syncStatus(): Promise<JobSnapshot | null> {
    try {
      const snapshot = await this.api.jobStatus(this.jobId);
      console.info("research:monitor:status", { jobId: this.jobId, status: snapshot.status, done: snapshot.done, total: snapshot.total, currentDomain: snapshot.current_domain, runId: snapshot.run_id });
      if (this.stopped) return snapshot;
      this.receive(snapshot);
      return snapshot;
    } catch (error) {
      console.info("research:monitor:status-error", { jobId: this.jobId, error: error instanceof Error ? error.message : String(error) });
      return null;
    }
  }

  private async connectWithSync(): Promise<void> {
    if (this.stopped || this.connecting || this.socket) return;
    this.connecting = true;
    const snapshot = await this.syncStatus();
    this.connecting = false;
    if (this.stopped || (snapshot && isTerminal(snapshot.status))) return;

    try {
      const socket = this.api.jobSocket(this.jobId, (message) => this.receive(message));
      this.socket = socket;
      this.startPolling();
      socket.onopen = () => {
        if (this.stopped || this.socket !== socket) return;
        this.reconnectAttempt = 0;
        this.stopPolling();
      };
      socket.onclose = () => this.handleDisconnect(socket);
      socket.onerror = () => socket.close();
      if (socket.readyState === 1) {
        this.reconnectAttempt = 0;
        this.stopPolling();
      }
    } catch {
      this.handleDisconnect(null);
    }
  }

  private receive(snapshot: JobSnapshot): void {
    if (this.stopped) return;
    console.info("research:monitor:receive", { jobId: this.jobId, status: snapshot.status, done: snapshot.done, total: snapshot.total, currentDomain: snapshot.current_domain, runId: snapshot.run_id });
    this.onSnapshot(snapshot);
    if (isTerminal(snapshot.status)) {
      console.info("research:monitor:terminal", { jobId: this.jobId, status: snapshot.status, runId: snapshot.run_id });
      this.storage.removeItem(ACTIVE_RESEARCH_JOB_KEY);
      this.stop();
    }
  }

  private handleDisconnect(socket: WebSocket | null): void {
    if (this.stopped || (socket && this.socket !== socket)) return;
    this.socket = null;
    void this.syncStatus();
    this.startPolling();
    this.scheduleReconnect();
  }

  private startPolling(): void {
    if (this.pollTimer !== undefined || this.stopped) return;
    this.pollTimer = this.setIntervalFn(() => {
      void this.syncStatus();
    }, this.pollIntervalMs);
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== undefined || this.stopped) return;
    const delay = Math.min(this.reconnectBaseMs * 2 ** this.reconnectAttempt, this.reconnectMaxMs);
    this.reconnectAttempt += 1;
    this.reconnectTimer = this.setTimeoutFn(() => {
      this.reconnectTimer = undefined;
      void this.connectWithSync();
    }, delay);
  }

  private stopPolling(): void {
    if (this.pollTimer === undefined) return;
    this.clearIntervalFn(this.pollTimer);
    this.pollTimer = undefined;
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === undefined) return;
    this.clearTimeoutFn(this.reconnectTimer);
    this.reconnectTimer = undefined;
  }
}