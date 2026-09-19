import { useEffect, useRef, useState } from "react";

// Module-level (not React state) so entries survive a page unmount/remount --
// switching tabs and back should show the last-known data instantly instead
// of re-paying the network round trip every time. TTL keeps it from ever
// serving arbitrarily stale data; invalidate() covers mutations (save,
// create campaign, etc.) that should force a fresh fetch immediately, even
// in a component that's still mounted and watching that key.
const cache = new Map<string, { data: unknown; fetchedAt: number }>();
const DEFAULT_TTL_MS = 30_000;

type Listener = (opts: { force: boolean }) => void;
const listeners = new Map<string, Set<Listener>>();

function notify(key: string, opts: { force: boolean }) {
  listeners.get(key)?.forEach((fn) => fn(opts));
}

function matches(key: string, keyOrPrefix: string): boolean {
  return key === keyOrPrefix || key.startsWith(`${keyOrPrefix}:`);
}

/** Drops cached entries for an exact key or every key under a `prefix:...`
 *  namespace, and tells any currently-mounted `useCachedFetch` watching an
 *  affected key to refetch immediately -- including a mounted listener whose
 *  key was never cached yet (e.g. its first fetch is still in flight). */
export function invalidate(keyOrPrefix: string): void {
  for (const key of cache.keys()) {
    if (matches(key, keyOrPrefix)) cache.delete(key);
  }
  for (const key of listeners.keys()) {
    if (matches(key, keyOrPrefix)) notify(key, { force: true });
  }
}

/** Rewrites an exact key's cached value in place (keeping its `fetchedAt`,
 *  so the TTL clock isn't reset) and pushes the change to every mounted
 *  `useCachedFetch(key, ...)` synchronously -- no network round trip. For
 *  optimistic UI: apply the local change here right before firing the
 *  mutating request, so the screen updates instantly instead of waiting on
 *  refresh()/invalidate() to refetch. If the request then fails, the caller
 *  should call invalidate(key) to reconcile with the server's real state. */
export function patch<T>(key: string, updater: (current: T | null) => T): void {
  const entry = cache.get(key) as { data: T; fetchedAt: number } | undefined;
  const next = updater(entry ? entry.data : null);
  cache.set(key, { data: next, fetchedAt: entry?.fetchedAt ?? Date.now() });
  notify(key, { force: false });
}

/** Same as `patch`, but applied to every cached key under a `prefix:...`
 *  namespace at once -- for a mutation (e.g. suppressing a domain) that
 *  should disappear from every differently-filtered/scoped list caching it,
 *  not just one exact key. `updater` gets each matching key's own value. */
export function patchPrefix<T>(prefix: string, updater: (current: T) => T): void {
  for (const [key, entry] of cache.entries()) {
    if (!matches(key, prefix)) continue;
    cache.set(key, { data: updater(entry.data as T), fetchedAt: entry.fetchedAt });
    notify(key, { force: false });
  }
}

interface Options {
  ttlMs?: number;
  enabled?: boolean;
}

interface Result<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

/**
 * Stale-while-revalidate fetch: returns cached data for `key` immediately
 * (if present and not expired) while a fresh fetch runs in the background,
 * so `loading` is only ever true on a key's first-ever fetch. Pass a
 * stable `key` (e.g. `companies:${runId}:${JSON.stringify(filter)}`) --
 * changing it fetches (or serves cached data for) the new key. Call
 * `invalidate(key)` after a mutation to force every mounted consumer of
 * that key (or `prefix:...` namespace) to refetch immediately.
 */
export function useCachedFetch<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  { ttlMs = DEFAULT_TTL_MS, enabled = true }: Options = {},
): Result<T> {
  const cached = key ? (cache.get(key) as { data: T; fetchedAt: number } | undefined) : undefined;
  const [data, setData] = useState<T | null>(cached?.data ?? null);
  const [loading, setLoading] = useState(!cached);
  const [error, setError] = useState<Error | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // Tracks the key this hook instance most recently issued a fetch for, so a
  // slow response for a key someone has since navigated away from (e.g.
  // switching the run filter before the previous run's fetch resolves) can't
  // land after the fact and clobber the current key's state with stale data --
  // the exact "filters sometimes don't work" symptom this was causing.
  const activeKeyRef = useRef<string | null>(null);

  function load(key: string, force: boolean) {
    activeKeyRef.current = key;
    const entry = cache.get(key) as { data: T; fetchedAt: number } | undefined;
    const isFresh = !force && entry && Date.now() - entry.fetchedAt < ttlMs;

    if (entry) {
      setData(entry.data);
      setLoading(false);
    } else {
      setData(null);
      setLoading(true);
    }
    setError(null);

    if (isFresh) return;
    if (!entry) setLoading(true);

    fetcherRef.current()
      .then((result) => {
        cache.set(key, { data: result, fetchedAt: Date.now() });
        if (activeKeyRef.current === key) setData(result);
      })
      .catch((e) => {
        if (activeKeyRef.current === key) setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (activeKeyRef.current === key) setLoading(false);
      });
  }

  useEffect(() => {
    if (!key || !enabled) return;
    load(key, false);

    const set = listeners.get(key) ?? new Set();
    const onInvalidate = (opts: { force: boolean }) => load(key, opts.force);
    set.add(onInvalidate);
    listeners.set(key, set);

    return () => {
      set.delete(onInvalidate);
      if (set.size === 0) listeners.delete(key);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, ttlMs]);

  function refresh() {
    if (key) load(key, true);
  }

  return { data, loading, error, refresh };
}
