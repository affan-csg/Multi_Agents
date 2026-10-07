/** Dashboard access gate. The password is never in this bundle: the user
 *  types it, the server (DASHBOARD_PASSWORD) validates it at /auth/login and
 *  returns a token kept in sessionStorage, so access lasts for the browser
 *  session only. */
import { agentBaseUrl } from "./agents/registry";

const KEY = "csg-dashboard-token";
const LOGIN_URL = `${agentBaseUrl("research").replace(/\/research$/, "")}/auth/login`;
const API_ORIGIN = new URL(LOGIN_URL, window.location.href).origin;

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

/** Append the token to URLs that can't carry an Authorization header
 *  (WebSocket handshakes, `<a href>` downloads). */
export function withToken(url: string): string {
  const token = getToken();
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

/** Throws Error with a user-facing message when the password is rejected. */
export async function login(password: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(LOGIN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
  } catch {
    throw new Error("Cannot reach the server. Try again.");
  }
  if (res.status === 401) throw new Error("Incorrect password.");
  if (!res.ok) throw new Error(`Login failed (${res.status}).`);
  const { token } = await res.json();
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    /* session storage unavailable: access lasts until reload */
  }
}

/** Attach the token to every request to the backend, and re-lock the
 *  dashboard if the server ever answers 401 (e.g. password rotated). */
export function installAuthFetch() {
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const toApi = new URL(url, window.location.href).origin === API_ORIGIN;
    const token = getToken();
    if (toApi && token && !url.endsWith("/auth/login")) {
      const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("Authorization", `Bearer ${token}`);
      init = { ...init, headers };
    }
    const res = await original(input, init);
    if (toApi && res.status === 401 && !url.endsWith("/auth/login") && getToken()) {
      clearToken();
      window.location.reload();
    }
    return res;
  };
}
