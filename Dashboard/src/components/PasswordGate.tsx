import { useState, type FormEvent, type ReactNode } from "react";
import { getToken, installAuthFetch, login } from "../auth";

installAuthFetch();

export function PasswordGate({ children }: { children: ReactNode }) {
  const [unlocked, setUnlocked] = useState(() => !!getToken());
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (unlocked) return <>{children}</>;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(password);
      setUnlocked(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0f1115", padding: 16 }}>
      <form
        onSubmit={submit}
        style={{
          width: "100%", maxWidth: 360, background: "#181b22", border: "1px solid #2a2f3a", borderRadius: 12,
          padding: 28, display: "flex", flexDirection: "column", gap: 14, color: "#e8eaf0", fontFamily: "Inter, system-ui, sans-serif",
        }}
      >
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>CSG Multi Agents</h1>
        <p style={{ margin: 0, fontSize: 14, color: "#9aa3b2" }}>Enter the password to continue.</p>
        <input
          type="password"
          autoFocus
          autoComplete="current-password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={!!error}
          style={{
            padding: "10px 12px", borderRadius: 8, fontSize: 15, background: "#0f1115", color: "inherit",
            border: `1px solid ${error ? "#e5484d" : "#2a2f3a"}`, outline: "none",
          }}
        />
        {error && <div role="alert" style={{ color: "#ff6369", fontSize: 13 }}>{error}</div>}
        <button
          type="submit"
          disabled={busy || !password}
          style={{
            padding: "10px 12px", borderRadius: 8, border: "none", fontSize: 15, fontWeight: 600,
            background: "#4f7cff", color: "#fff", cursor: busy || !password ? "not-allowed" : "pointer", opacity: busy || !password ? 0.6 : 1,
          }}
        >
          {busy ? "Checking…" : "Unlock"}
        </button>
      </form>
    </div>
  );
}
