# Server

Composition layer only. Mounts the Research Agent's app
(`Research_Agent/src/csg/api.py`) at `/research` and the Outreach Agent's
app (`Outreach_Agent/src/outreach/api.py`) at `/outreach` onto one FastAPI
instance, so both run under one uvicorn process on one port instead of two.

**No business logic lives here.** Every route, model, and background job
is exactly what it was when each agent ran standalone — this package only
wires them together and adds four cross-cutting concerns: the Dashboard
password middleware, the production CORS origin, the optional background
scheduler, and a root alias for the Calendly webhook. See
[app.py](app.py)'s module docstring for the mechanics.

## Why one port

The Dashboard already treats each agent as a separate HTTP API with its own
base URL — nothing about that changed. What changed is that both APIs are
now reachable through one origin (`http://127.0.0.1:8000`), at `/research`
and `/outreach`, instead of two separate ports (`:8000` and `:8001`).

## What stays separate

- **Databases.** Research and Outreach keep their own Supabase projects and
  use only `RESEARCH_SUPABASE_URL`/`RESEARCH_SUPABASE_SERVICE_KEY` and
  `OUTREACH_SUPABASE_URL`/`OUTREACH_SUPABASE_SERVICE_KEY`, respectively.
  `app.py` reads these names from each agent's own `.env` without merging
  credentials. Render may provide the same four variables directly; process
  environment values take precedence over `.env` values.
- **Code.** Nothing under `Research_Agent/src/csg/` or
  `Outreach_Agent/src/outreach/` is restructured by mounting. Each agent's
  `db.connect()` reads only its own scoped `*_SUPABASE_*` variables.
- **Business logic, request/response formats, WebSockets, background jobs.**
  Unchanged — see [app.py](app.py) for exactly what this layer does (chains
  both apps' lifespans, mounts both apps, adds dashboard auth + CORS, starts
  the optional scheduler, and adds one root alias for the Calendly webhook).
- **Startup check.** The lifespan refuses to start if any of the four
  Supabase variables is missing.

## Setup

One venv holding all three packages editable:

```
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -e ../Research_Agent -e ../Outreach_Agent -e .
```

(paths are relative to this directory; from the repo root, drop the `../`
and point at `Research_Agent`/`Outreach_Agent`/`Server`.)

Each agent's own `.env` still lives in its own project directory
(`Research_Agent/.env`, `Outreach_Agent/.env`) — nothing moves. Fill them in
per each project's own `.env.example`; this server reads both at startup.

For a combined Render/Railway service running `Server/app.py`, configure these four
environment variables in the Render service settings, using each project's
own Supabase URL and service-role key:

```
RESEARCH_SUPABASE_URL
RESEARCH_SUPABASE_SERVICE_KEY
OUTREACH_SUPABASE_URL
OUTREACH_SUPABASE_SERVICE_KEY
```

For separate Render services, set only the matching pair on each agent
service. Do not configure the legacy shared `SUPABASE_URL` or
`SUPABASE_SERVICE_KEY` names.

## Dashboard password

Every route (HTTP and WebSocket, including both sub-apps) except `POST /auth/login`
and the Calendly webhooks (`/calendly/webhook`, `/outreach/calendly/webhook`)
requires a token. Loopback requests from `127.0.0.1`/`::1` that carry no
`Origin` header — the Research↔Outreach hand-off calls — are let through, as
are CORS preflights. Set `DASHBOARD_PASSWORD` (see [.env.example](.env.example)); the
Dashboard's login screen posts it to `/auth/login` and gets back a stateless
HMAC token, sent as `Authorization: Bearer <token>` (or `?token=` for
WebSockets and download links). With the variable unset the server fails
closed: every protected route returns 503.

## Automatic polling

`AUTO_SYNC_ENABLED=true` starts the background loop (`outreach.automation.loop`)
inside this process's lifespan; it syncs Calendly, reads the inbox and
sends/drafts follow-ups every `AUTO_SYNC_INTERVAL_MINUTES` (default 30, clamped
5–1440; afterwards the value stored in `automation_state` wins). It is off by
default and only exists under this server — a standalone Outreach process has
no scheduler. See the
[Outreach README](../Outreach_Agent/README.md#automatic-polling-and-follow-ups).

## Run

```
python -m uvicorn app:app --app-dir . --reload --reload-dir ../Research_Agent/src --reload-dir ../Outreach_Agent/src --reload-dir . --port 8000
```

or from the repo root:

```
python -m uvicorn app:app --app-dir Server --reload --reload-dir Research_Agent/src --reload-dir Outreach_Agent/src --reload-dir Server --port 8000
```

- `http://127.0.0.1:8000/research/...` — Research Agent, identical routes to
  its standalone `:8000`.
- `http://127.0.0.1:8000/outreach/...` — Outreach Agent, identical routes to
  its standalone `:8001`.
- `http://127.0.0.1:8000/docs` — combined OpenAPI UI (each sub-app also has
  its own at `/research/docs` and `/outreach/docs`).
- `POST http://127.0.0.1:8000/calendly/webhook` — root alias for
  `POST /outreach/calendly/webhook`, so an already-registered Calendly
  webhook subscription keeps working with no re-registration. Both paths
  call the exact same handler.

CORS: `https://multi-agents-beta.vercel.app` and any `http://localhost|127.0.0.1:5xxx`
origin are allowed; add other deployed Dashboard origins to `allow_origins` in
[app.py](app.py).

Override the port the server advertises to itself (used to build the
Research→Outreach hand-off URL and the Outreach→Research URL) with
`CSG_API_BASE`, e.g. `CSG_API_BASE=http://127.0.0.1:9000`.

## Running an agent standalone

Each agent is still a normal, independent FastAPI app and can be run
without this server at all, on its own port, exactly as before:

```
cd Research_Agent
python -m uvicorn csg.api:app --app-dir src --reload --port 8000

cd Outreach_Agent
python -m uvicorn outreach.api:app --app-dir src --reload --port 8001
```

In that mode each agent reads its own scoped Supabase variables from its
`.env`; use the same scoped variable names in that agent's hosting environment.
`OUTREACH_API_BASE` / `RESEARCH_API_BASE` should point at whatever port the
other agent is actually running on (`http://127.0.0.1:8001` and
`http://127.0.0.1:8000` with no path suffix). Standalone agents have no
dashboard auth and no scheduler. Point the Dashboard at them only through a
proxy that exposes `/research` and `/outreach` under one origin.

## Tests

Fully offline — same fake-connection pattern as each agent's own suite, no
real Supabase project or network access required:

```
pytest -q
```

Covers: every route from both standalone apps is reachable at the same path
under its prefix, both apps' lifespans actually run (so background jobs and
WebSocket progress publish work), WebSocket close behavior survives
mounting, CORS survives mounting, the Calendly webhook alias calls the exact
same handler as its prefixed route, the dashboard-auth middleware
(login, bearer/`?token=` access, fail-closed 503, webhook exemption), and the
scoped-Supabase-var resolution (including rejection of the legacy unscoped `SUPABASE_*` names, and a warning when a shared LLM key
differs between the two `.env` files). Run it from an environment where all
three packages are installed (`pip install -e Research_Agent -e Outreach_Agent
-e Server`), otherwise collection fails with `No module named 'outreach'`.
