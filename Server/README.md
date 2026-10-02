# Server

Composition layer only. Mounts the Research Agent's app
(`Research_Agent/src/csg/api.py`) at `/research` and the Outreach Agent's
app (`Outreach_Agent/src/outreach/api.py`) at `/outreach` onto one FastAPI
instance, so both run under one uvicorn process on one port instead of two.

**No business logic lives here.** Every route, model, background job,
WebSocket, and CORS rule is exactly what it was when each agent ran
standalone — this package only wires them together. See
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
  `Outreach_Agent/src/outreach/` was restructured. The only edits outside
  this directory are the one-line scoped-var fallback in each agent's own
  `db.connect()` described above.
- **Business logic, request/response formats, auth, WebSockets, background
  jobs, CORS.** All unchanged — see [app.py](app.py) for exactly what this
  layer does (chains both apps' lifespans, mounts both apps, adds one root
  alias for the Calendly webhook).

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

For a combined Render service running `Server/app.py`, configure these four
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
`.env`; use the same scoped variable names in that agent's Render environment.
`OUTREACH_API_BASE` /
`RESEARCH_API_BASE` should point at whatever port the other agent is
actually running on (see each project's `.env.example`).

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
same handler as its prefixed route, and the scoped-Supabase-var resolution
(including graceful fallback to the unscoped vars, and a warning when a
shared LLM key differs between the two `.env` files).
