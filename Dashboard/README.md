# CSG Multi Agents

A single dashboard across every CSG agent (Research, Outreach, and future
agents like Marketing/Follow-up). It has **no database and no backend of its
own** — it is a React/Vite app that calls each agent's existing FastAPI
directly from the browser, the same way each agent's own frontend does.

```
Browser (this app, Vite dev server -- picks the next free port from :5173)
  ├─→ GET http://127.0.0.1:8000/research/...   (Research_Agent/src/csg/api.py)
  └─→ GET http://127.0.0.1:8000/outreach/...   (Outreach_Agent/src/outreach/api.py)
```

Both agents are normally served through one backend process/port — see
[Server](../Server) — but remain separate FastAPI apps with separate code
and separate databases; only the path prefix (`/research` vs `/outreach`)
tells them apart. Each agent keeps its own database, and its own
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

Requires the backend(s) for the agents you want to see to be running.
Normally that's both agents together through the single-port
[Server](../Server):

```
python -m uvicorn app:app --app-dir ../Server --reload --reload-dir ../Research_Agent/src --reload-dir ../Outreach_Agent/src --reload-dir ../Server --port 8000
```

which serves Research at `:8000/research` and Outreach at `:8000/outreach`
— matching this app's `.env.example` default. For deployment, set
`VITE_API_URL` in the hosting environment to the backend origin; Vercel
builds then send API requests to that origin under `/research` and
`/outreach`.

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
  Pending → Sent → Waiting → Replied → Booked → Failed; cards from Sent
  onward are drag-and-drop between columns, in addition to the item drawer's
  stage buttons — both paths call the same API; the drawer's Conversation
  section shows the reply thread, a pending draft to approve/redraft/edit,
  and the original outreach message collapsed below it; Failed is reached
  automatically when a reply is classified as a clear rejection, and is the
  only column besides Booked a card doesn't leave on its own), Calendly (one
  page, capsule tabs at the top — same tab pattern as the Research Agent's
  Settings page — instead of separate nav items:
  - **Bookings tab**: every prospect currently `booked`, pooled across every
    campaign, from `GET /bookings` — row click opens the specific Calendly
    event when one is matched (real Calendly API/webhook integration, see
    [Outreach_Agent/README.md](../Outreach_Agent/README.md#calendly-integration)),
    the account's shared scheduling page otherwise, or the campaign if
    neither is available; a "Sync Calendly" button pulls the latest
    bookings/cancellations on demand; All/Upcoming/Past filter tabs plus
    search and sort, all server-side via `GET /bookings`'s query params; a
    per-row menu (any booking with an `event_uri`, matched or not) for
    Cancel booking, Mark/unmark no-show, and Delete — cancel calls `POST
    /calendly/events/cancel` and reverts the item's stage the same way an
    organic cancellation webhook would; delete (`POST
    /calendly/events/delete`) removes the local row entirely, any status,
    canceling on Calendly first only if the event is still active/upcoming;
    unmatched Calendly bookings — made outside this agent's own outreach —
    appear in the same table.
  - **Event Types tab**: view-only list of the account's meeting types and
    availability schedule(s) (Calendly's API has no write endpoint for
    either), plus a "Get scheduling link" action per event type (a real
    mutation, `POST /calendly/scheduling-links`).
  
  and Learning & Optimization (observes draft edits,
  tracks performance, displays learned patterns, before/after comparisons,
  human-edit trends, campaign comparison, drill-down into the data backing
  each insight, plus the Campaign-Level Optimization layer's own Strategy
  Insights and Adoption Tracking sections — six tabs total on one page,
  `src/pages/outreach/Learning.tsx`).
- `src/components/` — shared UI (`ui.tsx` design system, Nav, drawers,
  modals) — one visual system for every agent's pages.
- `src/agents/learning.ts` / `src/agents/optimization.ts` — typed HTTP
  clients for the Outreach Agent's `/learning/*` and `/optimization/*`
  endpoints, respectively — two independent backend layers, one dashboard
  page.
- `src/agents/calendly.ts` — typed HTTP client for the Outreach Agent's
  `/calendly/status`, `/calendly/sync`, `/calendly/events/cancel`,
  `/calendly/events/delete`, `/calendly/events/no-show`,
  `/calendly/event-types`, `/calendly/availability`, and
  `/calendly/scheduling-links` endpoints (the Calendly page's Bookings and
  Event Types tabs); the webhook receiver itself is server-to-server and
  never called from here.
- `App.css`/`index.css` — the shared design system's styles,
  plus a `.kanban-*` block for the Outreach pipeline board, a `.tabs` block
  for the Learning & Optimization page's internal section navigation (also
  used by the Calendly page's own All/Upcoming/Past filter), and a
  `.settings-tabs` block (originally the Research Agent Settings page's
  General/Usage switcher) reused by the Calendly page's top-level
  Bookings/Event Types tabs.
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
