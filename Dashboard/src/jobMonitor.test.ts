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

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }
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
});