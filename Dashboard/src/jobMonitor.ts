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

type ListenerTarget = {
  addEventListener: (type: string, listener: () => void) => void;
  removeEventListener: (type: string, listener: () => void) => void;
};

type VisibilityTarget = ListenerTarget & { visibilityState?: string };

type JobMonitorOptions = {
  pollIntervalMs?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  setTimeoutFn?: typeof window.setTimeout;
  clearTimeoutFn?: typeof window.clearTimeout;
  setIntervalFn?: typeof window.setInterval;
  clearIntervalFn?: typeof window.clearInterval;
  /** Injectable for tests; defaults to the real document when present. */
  documentRef?: VisibilityTarget | null;
  /** Injectable for tests; defaults to the real window when present. */
  windowRef?: ListenerTarget | null;
};

const isTerminal = (status: JobSnapshot["status"]) =>
  status === "done" || status === "error" || status === "cancelled" || status === "quota_blocked";

export class JobMonitor {
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private pollTimer: ReturnType<typeof setInterval> | undefined;
  private reconnectAttempt = 0;
  private stopped = false;
  private connecting = false;
  private lastSnapshot: string | null = null;
  private lastUpdatedAt: string | null = null;
  private stateVersion = 0;
  private statusRequests = 0;
  private pendingEventRefresh = false;
  private eventRefreshQueued = false;
  private listenersAttached = false;
  private readonly documentRef: VisibilityTarget | null;
  private readonly windowRef: ListenerTarget | null;
  private readonly onVisibilityChange = (): void => {
    if (this.stopped) return;
    if (this.documentRef?.visibilityState === "visible") this.requestEventRefresh();
  };
  private readonly onOnline = (): void => {
    if (!this.stopped) this.requestEventRefresh();
  };
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
    this.documentRef = options.documentRef ?? (
      typeof document !== "undefined" ? (document as unknown as VisibilityTarget) : null
    );
    this.windowRef = options.windowRef ?? (
      typeof window !== "undefined" ? (window as unknown as ListenerTarget) : null
    );
  }

  start(): void {
    if (this.stopped) return;
    console.info("research:monitor:start", { jobId: this.jobId, activeStorage: this.storage.getItem(ACTIVE_RESEARCH_JOB_KEY) });
    this.storage.setItem(ACTIVE_RESEARCH_JOB_KEY, this.jobId);
    // Start the authoritative poll before the first request resolves: if that
    // request hangs (a stalled proxy/connection), monitoring must not hang
    // with it -- later ticks are independent requests.
    this.startPolling();
    this.attachEventListeners();
    void this.connectWithSync();
  }

  stop(): void {
    this.stopped = true;
    this.detachEventListeners();
    this.pendingEventRefresh = false;
    this.eventRefreshQueued = false;
    this.clearReconnectTimer();
    this.stopPolling();
    this.socket?.close();
    this.socket = null;
  }

  private attachEventListeners(): void {
    if (this.listenersAttached) return;
    this.documentRef?.addEventListener("visibilitychange", this.onVisibilityChange);
    this.windowRef?.addEventListener("online", this.onOnline);
    this.listenersAttached = true;
  }

  private detachEventListeners(): void {
    if (!this.listenersAttached) return;
    this.documentRef?.removeEventListener("visibilitychange", this.onVisibilityChange);
    this.windowRef?.removeEventListener("online", this.onOnline);
    this.listenersAttached = false;
  }

  /** Coalesce wake/online bursts into one request and never overlap one that
   *  is already running. Poll ticks deliberately bypass this: they are the
   *  safety net and must keep retrying even if a request hangs. */
  private requestEventRefresh(): void {
    if (this.stopped || this.eventRefreshQueued) return;
    if (this.statusRequests > 0) {
      this.pendingEventRefresh = true;
      return;
    }
    this.eventRefreshQueued = true;
    queueMicrotask(() => {
      this.eventRefreshQueued = false;
      if (!this.stopped) void this.syncStatus();
    });
  }

  private async syncStatus(): Promise<JobSnapshot | null> {
    const versionAtStart = this.stateVersion;
    this.statusRequests += 1;
    try {
      const snapshot = await this.api.jobStatus(this.jobId);
      console.info("research:monitor:status", { jobId: this.jobId, status: snapshot.status, done: snapshot.done, total: snapshot.total, currentDomain: snapshot.current_domain, runId: snapshot.run_id });
      if (this.stopped) return snapshot;
      // Older backends omit updated_at; then a response that started before a
      // newer applied snapshot is stale by construction.
      if (!snapshot.updated_at && versionAtStart !== this.stateVersion) return snapshot;
      this.receive(snapshot);
      return snapshot;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.info("research:monitor:status-error", { jobId: this.jobId, error: message });
      // A persisted job id can outlive its row (deleted run/job). Retrying a
      // definitive 404 forever would leave the restored overlay stuck.
      if (/^404\b/.test(message)) {
        this.storage.removeItem(ACTIVE_RESEARCH_JOB_KEY);
        this.stop();
      }
      return null;
    } finally {
      this.statusRequests -= 1;
      if (this.statusRequests === 0 && this.pendingEventRefresh) {
        this.pendingEventRefresh = false;
        this.requestEventRefresh();
      }
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
      // Polling stays on while the socket is open. A sleeping laptop or a
      // network change can kill a WebSocket without ever firing onclose, and
      // a monitor that trusted the socket would then show "running" forever
      // even though GET /jobs/{id} already reports a terminal state. The
      // socket is only a latency optimization; the poll is the safety net.
      this.startPolling();
      socket.onopen = () => {
        if (this.stopped || this.socket !== socket) return;
        this.reconnectAttempt = 0;
      };
      socket.onclose = () => this.handleDisconnect(socket);
      socket.onerror = () => socket.close();
    } catch {
      this.handleDisconnect(null);
    }
  }

  private receive(snapshot: JobSnapshot): void {
    if (this.stopped) return;
    const updatedAt = snapshot.updated_at || null;
    if (updatedAt && this.lastUpdatedAt && updatedAt < this.lastUpdatedAt) return;
    // The socket and the poll can deliver the same snapshot; forward each
    // distinct state once so a completed job cannot toast twice. updated_at is
    // excluded from the identity key so a heartbeat alone is not a UI update.
    const key = JSON.stringify({ ...snapshot, updated_at: undefined });
    if (key === this.lastSnapshot) {
      if (updatedAt) this.lastUpdatedAt = updatedAt;
      return;
    }
    this.lastSnapshot = key;
    if (updatedAt) this.lastUpdatedAt = updatedAt;
    this.stateVersion += 1;
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