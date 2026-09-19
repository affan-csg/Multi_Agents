# CSG Unified Dashboard

A single dashboard across every CSG agent (Research, Outreach, and future
agents like Marketing/Follow-up). It has **no database and no backend of its
own** — it is a React/Vite app that calls each agent's existing FastAPI
directly from the browser, the same way each agent's own frontend does.

```
Browser (this app, Vite dev server -- picks the next free port from :5173)
  ├─→ GET http://127.0.0.1:8000/...   (Research_Agent/src/csg/api.py)
  └─→ GET http://127.0.0.1:8001/...   (Outreach_Agent/src/outreach/api.py)
```

Each agent keeps its own database, its own backend process, and its own
independence — deleting or redeploying this dashboard never touches either
agent, and deleting this dashboard doesn't either. Nothing here merges their
databases; cross-agent linkage happens the way it already does at the API
level (Outreach references `research_domain`, a plain string, never a shared
table).

## Adding a new agent

1. Add its base URL to `src/agents/registry.ts`.
2. Add a typed client module `src/agents/<agent>.ts`, mirroring
   `research.ts`/`outreach.ts` — one file per agent, talking only to that
   agent's own API.
3. Add its pages under `src/pages/<agent>/` and wire routes in `router.ts`,
   `App.tsx`, and `components/Nav.tsx`.

No existing agent's code changes.

## Setup

```
npm install
cp .env.example .env   # point at each agent's API if not on the defaults
npm run dev
```

Requires the agents you want to see to be running (each from its own
directory, each in its own terminal):

```
# Research Agent -- :8000
cd ../Research_Agent
python -m uvicorn csg.api:app --app-dir src --reload --port 8000

# Outreach Agent -- :8001 (not optional -- see its README)
cd ../Outreach_Agent
python -m uvicorn outreach.api:app --app-dir src --reload --port 8001
```

Use `python -m uvicorn ...`, not the bare `uvicorn` command — on Windows, a
venv's `uvicorn.exe` launcher hardcodes an absolute path to `python.exe` at
creation time, so a venv folder moved/renamed after `pip install` breaks the
launcher (`Fatal error in launcher: Unable to create process...`) even
though the venv itself is fine. `python -m uvicorn` runs it as a module
through `python.exe` directly instead. Also, on Windows PowerShell (not
pwsh/PowerShell 7), `&&` is not a valid command separator — put each command
on its own line, as above, or use `;` to chain them.

If an agent's API is unreachable, its section of the Dashboard shows an
empty/error state and the nav's status pill reads "One or more agents
unreachable" rather than failing the whole app. That almost always means one
of the two backends above isn't running yet — start it and refresh.

## Structure

- `src/agents/registry.ts` — the list of connected agents (id, label, base URL).
- `src/agents/research.ts`, `src/agents/outreach.ts` — typed HTTP clients,
  one per agent, each talking to that agent's unmodified FastAPI.
- `src/pages/research/` — Runs, Companies, Company Detail, Saved, Contacts,
  Analytics, Settings. Originally ported from Research Agent's own
  standalone frontend (since retired — this Dashboard is now Research's only
  UI, same as it always was for Outreach).
- `src/pages/outreach/` — native to this dashboard (Outreach has no
  standalone frontend by design): Prospects (import from Research),
  Campaigns (select top-N, create — or create a fixed **Test Campaign** for
  QA, see below), Campaign Detail (Kanban pipeline board: Needs Review →
  Pending → Sent → Waiting → Replied → Booked; cards from Sent onward are
  drag-and-drop between columns, in addition to the item drawer's stage
  buttons — both paths call the same API).
- `src/components/` — shared UI (`ui.tsx` design system, Nav, drawers,
  modals) — one visual system for every agent's pages.
- `App.css`/`index.css` — the shared design system's styles,
  plus a `.kanban-*` block appended at the end for the Outreach pipeline
  board (the only new UI pattern Research didn't already have).
- `src/hooks/useCachedFetch.ts` — stale-while-revalidate data layer every
  page's `useCachedFetch(key, fetcher)` call goes through: cached data
  renders instantly on remount while a background refetch runs, so revisiting
  a page never feels like a cold load. Mutations should call `patch(key,
  updater)` (or `patchPrefix` for a namespace of keys, e.g. every
  `companies:<runId>:<filter>` variant) to apply the change to the cache and
  every mounted listener synchronously, instead of `invalidate(key)`, which
  forces a real network refetch. Use `invalidate` only when the new
  server-side state genuinely isn't knowable client-side (a server-computed
  aggregate, a brand-new record with a server-generated id, a bulk operation
  with per-item outcomes) — everything else should be an optimistic `patch`
  so the UI updates the instant a mutation is fired, not after its response
  comes back. Each hook instance tracks the `key` its most recent fetch was
  issued for (`activeKeyRef`) and ignores a response that resolves after the
  `key` has since changed (e.g. switching a run/filter dropdown quickly) —
  without this, a slow response for an abandoned key could land after a
  newer key's fetch and silently overwrite the current, correctly-filtered
  view with stale data.

### Test Campaign

Campaigns → New campaign defaults to **Test Campaign**, a fixed, repeatable
campaign (`POST /campaigns/test` on the Outreach Agent) with two synthetic
prospects — one lands in Pending, one in Manual Review — for exercising
campaign creation, sending, and the full pipeline (including kanban
drag-and-drop) without any real research run or real prospect. It never
requires selecting a research run, is exempt from the "one campaign per
research run" rule below, and its prospects never mix with real ones (a
separate, hardcoded `research_domain` namespace). Nothing it creates is ever
sent automatically. See the Outreach Agent's
[WORKFLOW.md](../Outreach_Agent/WORKFLOW.md#2-create-campaign--ingestpy) for
the backend side.

A research run can back at most one real campaign — the create-campaign
form disables re-selecting a run that already has one (checked against the
already-loaded campaign list, not a new API constraint), with the Test
Campaign option exempted since it's never tied to a run.

## What this is not

- Not a new source of truth — every value shown is fetched live from an
  agent's own API on each page load; nothing is cached or duplicated here.
- Not a proxy/aggregator backend — there is no server process for this app
  beyond the Vite dev/preview server; all requests go straight from the
  browser to each agent.
