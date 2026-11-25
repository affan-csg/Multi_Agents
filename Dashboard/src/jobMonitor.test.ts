import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobSnapshot } from "./agents/research";
import {
  ACTIVE_RESEARCH_JOB_KEY,
  getStoredActiveResearchJobId,
  JobMonitor,
} from "./jobMonitor";

class MemoryStorage {
  private values = new Map<string, string>();

  getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string) {
    this.values.set(key, value);
  }

  removeItem(key: string) {
    this.values.delete(key);
  }
}

class TestSocket {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  receive(snapshot: JobSnapshot) {
    this.onmessage?.({ data: JSON.stringify(snapshot) } as MessageEvent);
  }

  open() {
    this.readyState = 1;
    this.onopen?.({} as Event);
  }

  disconnect() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }

  error() {
    this.onerror?.({} as Event);
  }

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }
}

class FakeTarget {
  private listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, listener: () => void) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: () => void) {
    this.listeners.get(type)?.delete(listener);
  }

  dispatch(type: string) {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }

  listenerCount(type: string) {
    return this.listeners.get(type)?.size ?? 0;
  }
}

function fakeDocument(visibilityState: "visible" | "hidden" = "hidden") {
  return Object.assign(new FakeTarget(), { visibilityState });
}

function snapshot(status: JobSnapshot["status"]): JobSnapshot {
  return {
    job_id: "job-1",
    kind: "discover",
    run_id: status === "done" ? "run-1" : null,
    status,
    done: status === "done" ? 2 : 1,
    total: 2,
    current_domain: null,
    result: status === "done" ? { discovered: 2 } : null,
    error: null,
  };
}

function flushPromises() {
  return Promise.resolve().then(() => Promise.resolve());
}

afterEach(() => vi.useRealTimers());

describe("JobMonitor", () => {
  it("binds default timer functions so starting a run cannot throw Illegal invocation", async () => {
    // Chrome rejects native timers called with a non-window receiver, which is
    // what `this.setIntervalFn(...)` did before the defaults were bound.
    const realSetInterval = globalThis.setInterval;
    const strictSetInterval = vi.fn(function (
      this: unknown, handler: TimerHandler, timeout?: number,
    ) {
      if (this !== globalThis) throw new TypeError("Illegal invocation");
      return realSetInterval(handler, timeout);
    });
    const spy = vi.spyOn(globalThis, "setInterval")
      .mockImplementation(strictSetInterval as unknown as typeof setInterval);
    const jobStatus = vi.fn(async () => snapshot("running"));
    const jobSocket = vi.fn(() => new TestSocket() as unknown as WebSocket);
    const monitor = new JobMonitor(
      "job-1", { jobStatus, jobSocket }, vi.fn(), new MemoryStorage(),
      { pollIntervalMs: 60_000 },
    );

    try {
      expect(() => monitor.start()).not.toThrow();
      await flushPromises();
      expect(strictSetInterval).toHaveBeenCalled();
    } finally {
      monitor.stop();
      spy.mockRestore();
    }
  });

  it("restores a persisted job after reload and clears it when the API says completed", async () => {
    vi.useFakeTimers();
    const storage = new MemoryStorage();
    storage.setItem(ACTIVE_RESEARCH_JOB_KEY, "job-1");
    const jobStatus = vi.fn().mockResolvedValue(snapshot("done"));
    const jobSocket = vi.fn();
    const onSnapshot = vi.fn();
    const restoredJobId = getStoredActiveResearchJobId(storage);
    expect(restoredJobId).toBe("job-1");

    const monitor = new JobMonitor(restoredJobId!, { jobStatus, jobSocket }, onSnapshot, storage);
    monitor.start();
    await flushPromises();

    expect(jobStatus).toHaveBeenCalledWith("job-1");
    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(jobSocket).not.toHaveBeenCalled();
  });

  it("fetches authoritative state after disconnect, polls while offline, then reconnects", async () => {
    vi.useFakeTimers();
    let current = snapshot("running");
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => current);
    const jobSocket = vi.fn((_jobId: string, onMessage: (value: JobSnapshot) => void) => {
      const socket = new TestSocket();
      socket.onmessage = (event) => onMessage(JSON.parse(event.data) as JobSnapshot);
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
      reconnectBaseMs: 100,
      reconnectMaxMs: 400,
    });

    monitor.start();
    await flushPromises();
    expect(jobSocket).toHaveBeenCalledTimes(1);
    sockets[0].open();
    sockets[0].disconnect();
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(200);
    expect(jobStatus).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(100);
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(4);
    expect(jobSocket).toHaveBeenCalledTimes(2);

    current = snapshot("done");
    sockets[1].receive(current);
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    monitor.stop();
  });

  it("still polls to a terminal state when the socket stays open but stops delivering", async () => {
    // Reproduces a run that showed "running" until the page was refreshed:
    // the WebSocket never fired onclose (sleep/network change), so no
    // snapshot arrived, while GET /jobs/{id} had already returned done.
    vi.useFakeTimers();
    let current = snapshot("running");
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => current);
    const jobSocket = vi.fn(() => {
      const socket = new TestSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    storage.setItem(ACTIVE_RESEARCH_JOB_KEY, "job-1");
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();
    sockets[0].open();
    expect(sockets[0].readyState).toBe(1);

    current = snapshot("done");
    await vi.advanceTimersByTimeAsync(200);
    await flushPromises();

    expect(jobStatus.mock.calls.length).toBeGreaterThan(1);
    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(sockets[0].readyState).toBe(3);
  });

  it("clears a persisted job that the API reports as missing instead of retrying forever", async () => {
    vi.useFakeTimers();
    const jobStatus = vi.fn().mockRejectedValue(new Error("404 Not Found: job not found"));
    const jobSocket = vi.fn();
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    storage.setItem(ACTIVE_RESEARCH_JOB_KEY, "deleted-job");
    const monitor = new JobMonitor("deleted-job", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();

    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(jobSocket).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(600);
    expect(jobStatus).toHaveBeenCalledTimes(1);
  });

  it("starts polling even when the first status request never resolves", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const jobStatus = vi.fn(() => {
      calls += 1;
      return calls === 1 ? new Promise<JobSnapshot>(() => {}) : Promise.resolve(snapshot("done"));
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket: vi.fn() }, onSnapshot, storage, {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();
    expect(onSnapshot).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(200);
    await flushPromises();

    expect(jobStatus).toHaveBeenCalledTimes(2);
    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
  });

  it("keeps polling after a socket error closes the connection", async () => {
    vi.useFakeTimers();
    let current = snapshot("running");
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => current);
    const jobSocket = vi.fn(() => {
      const socket = new TestSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
      reconnectBaseMs: 10_000,
    });

    monitor.start();
    await flushPromises();
    sockets[0].open();
    sockets[0].error();
    await flushPromises();

    current = snapshot("done");
    await vi.advanceTimersByTimeAsync(200);
    await flushPromises();

    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
  });

  it.each(["done", "error", "cancelled"] as const)(
    "treats a socket-delivered %s as terminal and releases all timers",
    async (status) => {
      vi.useFakeTimers();
      const sockets: TestSocket[] = [];
      const jobStatus = vi.fn(async () => snapshot("running"));
      const jobSocket = vi.fn((_jobId: string, onMessage: (value: JobSnapshot) => void) => {
        const socket = new TestSocket();
        socket.onmessage = (event) => onMessage(JSON.parse(event.data) as JobSnapshot);
        sockets.push(socket);
        return socket as unknown as WebSocket;
      });
      const onSnapshot = vi.fn();
      const storage = new MemoryStorage();
      const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
        pollIntervalMs: 200,
      });

      monitor.start();
      await flushPromises();
      sockets[0].open();

      sockets[0].receive(snapshot(status));

      expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ status }));
      expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
      expect(sockets[0].readyState).toBe(3);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it("notifies once when socket and poll deliver the same snapshot", async () => {
    vi.useFakeTimers();
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => snapshot("running"));
    const jobSocket = vi.fn((_jobId: string, onMessage: (value: JobSnapshot) => void) => {
      const socket = new TestSocket();
      socket.onmessage = (event) => onMessage(JSON.parse(event.data) as JobSnapshot);
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();
    sockets[0].open();

    sockets[0].receive(snapshot("running"));
    sockets[0].receive(snapshot("running"));
    expect(onSnapshot).toHaveBeenCalledTimes(1);

    sockets[0].receive(snapshot("done"));
    sockets[0].receive(snapshot("done"));
    expect(onSnapshot).toHaveBeenCalledTimes(2);
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
  });

  it("releases the reconnect timer when monitoring is stopped on unmount", async () => {
    vi.useFakeTimers();
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => snapshot("running"));
    const jobSocket = vi.fn(() => {
      const socket = new TestSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, vi.fn(), storage, {
      pollIntervalMs: 200,
      reconnectBaseMs: 100,
    });

    monitor.start();
    await flushPromises();
    sockets[0].disconnect();
    await flushPromises();
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    const callsAtStop = jobStatus.mock.calls.length;
    const socketsAtStop = jobSocket.mock.calls.length;
    monitor.stop();

    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(1000);
    expect(jobStatus).toHaveBeenCalledTimes(callsAtStop);
    expect(jobSocket).toHaveBeenCalledTimes(socketsAtStop);
  });

  it("ignores a stale poll or socket snapshot that is older than the last applied version", async () => {
    vi.useFakeTimers();
    let current: JobSnapshot = { ...snapshot("running"), updated_at: "2026-11-25T10:00:00+00:00" };
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => current);
    const jobSocket = vi.fn((_jobId: string, onMessage: (value: JobSnapshot) => void) => {
      const socket = new TestSocket();
      socket.onmessage = (event) => onMessage(JSON.parse(event.data) as JobSnapshot);
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();
    sockets[0].open();

    const newer: JobSnapshot = {
      ...snapshot("running"), done: 2, current_domain: "enrich: newer.example",
      updated_at: "2026-11-25T10:00:02+00:00",
    };
    sockets[0].receive(newer);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ done: 2 }));

    const stale: JobSnapshot = {
      ...snapshot("running"), done: 1, current_domain: "enrich: stale.example",
      updated_at: "2026-11-25T10:00:01+00:00",
    };
    sockets[0].receive(stale);
    current = stale;
    await vi.advanceTimersByTimeAsync(200);
    await flushPromises();

    expect(onSnapshot).toHaveBeenCalledTimes(2);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ done: 2 }));
  });

  it("discards a poll response that started before a newer socket snapshot when the API has no version", async () => {
    vi.useFakeTimers();
    let resolvePoll: (value: JobSnapshot) => void = () => {};
    const pendingPoll = new Promise<JobSnapshot>((resolve) => { resolvePoll = resolve; });
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn()
      .mockResolvedValueOnce(snapshot("running"))
      .mockReturnValueOnce(pendingPoll);
    const jobSocket = vi.fn((_jobId: string, onMessage: (value: JobSnapshot) => void) => {
      const socket = new TestSocket();
      socket.onmessage = (event) => onMessage(JSON.parse(event.data) as JobSnapshot);
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, new MemoryStorage(), {
      pollIntervalMs: 200,
    });

    monitor.start();
    await flushPromises();
    sockets[0].open();

    await vi.advanceTimersByTimeAsync(200);
    expect(jobStatus).toHaveBeenCalledTimes(2);

    const newer: JobSnapshot = { ...snapshot("running"), done: 2 };
    sockets[0].receive(newer);
    resolvePoll({ ...snapshot("running"), done: 1 });
    await flushPromises();

    expect(onSnapshot).toHaveBeenCalledTimes(2);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ done: 2 }));
    monitor.stop();
  });

  it("refreshes immediately when the tab becomes visible, but not while hidden", async () => {
    vi.useFakeTimers();
    let current = snapshot("running");
    const jobStatus = vi.fn(async () => current);
    const documentRef = fakeDocument("hidden");
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const jobSocket = vi.fn(() => new TestSocket() as unknown as WebSocket);
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 30_000,
      documentRef,
    });

    monitor.start();
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(1);

    documentRef.dispatch("visibilitychange");
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(1);

    current = snapshot("done");
    documentRef.visibilityState = "visible";
    documentRef.dispatch("visibilitychange");
    await flushPromises();

    expect(jobStatus).toHaveBeenCalledTimes(2);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
  });

  it("recovers from an offline failure when the online event fires", async () => {
    vi.useFakeTimers();
    const windowRef = new FakeTarget();
    const jobStatus = vi.fn()
      .mockRejectedValueOnce(new Error("Failed to fetch"))
      .mockResolvedValue(snapshot("done"));
    const jobSocket = vi.fn(() => {
      const socket = new TestSocket();
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 30_000,
      windowRef,
    });

    monitor.start();
    await flushPromises();
    expect(onSnapshot).not.toHaveBeenCalled();
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBe("job-1");

    windowRef.dispatch("online");
    await flushPromises();

    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ status: "done" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("coalesces wake/online bursts into one queued request while a status call is in flight", async () => {
    vi.useFakeTimers();
    let resolveFirst: (value: JobSnapshot) => void = () => {};
    const first = new Promise<JobSnapshot>((resolve) => { resolveFirst = resolve; });
    const jobStatus = vi.fn().mockReturnValueOnce(first).mockResolvedValue(snapshot("running"));
    const jobSocket = vi.fn(() => new TestSocket() as unknown as WebSocket);
    const documentRef = fakeDocument("visible");
    const windowRef = new FakeTarget();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, vi.fn(), new MemoryStorage(), {
      pollIntervalMs: 30_000,
      documentRef,
      windowRef,
    });

    monitor.start();
    documentRef.dispatch("visibilitychange");
    windowRef.dispatch("online");
    documentRef.dispatch("visibilitychange");
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(1);

    resolveFirst(snapshot("running"));
    await flushPromises();

    expect(jobStatus).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(1);
    monitor.stop();
  });

  it("removes visibility/online listeners when monitoring stops", async () => {
    vi.useFakeTimers();
    const documentRef = fakeDocument("visible");
    const windowRef = new FakeTarget();
    const jobStatus = vi.fn(async () => snapshot("running"));
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket: vi.fn() }, vi.fn(), new MemoryStorage(), {
      documentRef,
      windowRef,
    });

    monitor.start();
    await flushPromises();
    expect(documentRef.listenerCount("visibilitychange")).toBe(1);
    expect(windowRef.listenerCount("online")).toBe(1);

    monitor.stop();

    expect(documentRef.listenerCount("visibilitychange")).toBe(0);
    expect(windowRef.listenerCount("online")).toBe(0);
    const callsAtStop = jobStatus.mock.calls.length;
    documentRef.dispatch("visibilitychange");
    windowRef.dispatch("online");
    await flushPromises();
    expect(jobStatus).toHaveBeenCalledTimes(callsAtStop);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("caps reconnect delay and stops polling/reconnecting after a terminal poll result", async () => {
    vi.useFakeTimers();
    let current = snapshot("running");
    const sockets: TestSocket[] = [];
    const jobStatus = vi.fn(async () => current);
    const jobSocket = vi.fn(() => {
      const socket = new TestSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 100,
      reconnectBaseMs: 50,
      reconnectMaxMs: 100,
    });

    monitor.start();
    await flushPromises();
    sockets[0].disconnect();
    await flushPromises();
    await vi.advanceTimersByTimeAsync(50);
    await flushPromises();
    expect(jobSocket).toHaveBeenCalledTimes(2);
    sockets[1].disconnect();
    await flushPromises();

    await vi.advanceTimersByTimeAsync(99);
    expect(jobSocket).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(jobSocket).toHaveBeenCalledTimes(3);
    sockets[2].disconnect();
    await flushPromises();
    await vi.advanceTimersByTimeAsync(99);
    expect(jobSocket).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(jobSocket).toHaveBeenCalledTimes(4);

    current = snapshot("done");
    await vi.advanceTimersByTimeAsync(100);
    await flushPromises();
    const callsAtTerminal = jobStatus.mock.calls.length;
    const socketsAtTerminal = jobSocket.mock.calls.length;
    await vi.advanceTimersByTimeAsync(1000);

    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "done" }));
    expect(jobStatus).toHaveBeenCalledTimes(callsAtTerminal);
    expect(jobSocket).toHaveBeenCalledTimes(socketsAtTerminal);
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
  });

  it("treats cancelled jobs as terminal and clears any stored active job", async () => {
    vi.useFakeTimers();
    const jobStatus = vi.fn().mockResolvedValue(snapshot("cancelled"));
    const jobSocket = vi.fn();
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();
    storage.setItem(ACTIVE_RESEARCH_JOB_KEY, "job-1");

    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage, {
      pollIntervalMs: 200,
      reconnectBaseMs: 100,
      reconnectMaxMs: 400,
    });

    monitor.start();
    await flushPromises();

    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "cancelled" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(jobSocket).not.toHaveBeenCalled();
  });

  it("treats quota-blocked jobs as terminal while preserving their snapshot", async () => {
    vi.useFakeTimers();
    const jobStatus = vi.fn().mockResolvedValue(snapshot("quota_blocked"));
    const jobSocket = vi.fn();
    const onSnapshot = vi.fn();
    const storage = new MemoryStorage();

    const monitor = new JobMonitor("job-1", { jobStatus, jobSocket }, onSnapshot, storage);
    monitor.start();
    await flushPromises();

    expect(onSnapshot).toHaveBeenCalledWith(expect.objectContaining({ status: "quota_blocked" }));
    expect(storage.getItem(ACTIVE_RESEARCH_JOB_KEY)).toBeNull();
    expect(jobSocket).not.toHaveBeenCalled();
  });
});