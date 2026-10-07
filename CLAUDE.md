# CSG Agents — Project Guide for Claude Code

This is a three-agent system for autonomous prospecting and outreach. Each agent is independent and uses its own Supabase project.

## Quick reference

| What | Where | Tech stack | Database |
|------|-------|-----------|----------|
| Company discovery, scoring, qualification | [Research_Agent/](Research_Agent/) | Python, FastAPI, Typer CLI | Supabase (own project) |
| Email campaigns, drafting, sending; reply handling; learning & optimization | [Outreach_Agent/](Outreach_Agent/) | Python, FastAPI, Typer CLI | Supabase (own project) |
| Unified UI for both agents | [Dashboard/](Dashboard/) | React, Vite, TypeScript, Recharts | None (API-only) |
| Backend composition layer (mounts both agents on :8000) | [Server/](Server/) | Python, FastAPI | N/A (proxy only) |

## Code structure

```
Research_Agent/
├── src/csg/
│   ├── api.py              # FastAPI routes + WebSocket job progress (/jobs/{id}/ws)
│   ├── cli.py              # Typer CLI (csg discover | report | show | qualify | contacts | export | suppress | dedupe-report)
│   ├── discover.py         # Run orchestration (discover -> research -> ATS/jobs -> score -> qualify -> contacts -> Outreach hand-off)
│   ├── discovery/          # Company discovery + firmographics (Gemini grounded search; Groq+Tavily fallback; Gemini key rotation; domain resolver)
│   ├── ats/                # ATS detection/fetch: Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, BambooHR, Rippling
│   ├── jobs_discovery.py   # ATS -> careers page -> web search -> unknown fallback chain (hiring_signal)
│   ├── dedupe.py           # Global company de-duplication (domain/name normalization, dedupe-report)
│   ├── scoring.py          # ICP-based scoring + tiering (A/B/C/D)
│   ├── qualify.py          # LLM qualification (pain_summary / recommended_angle), attempt ledger
│   ├── contacts.py         # Hunter.io contact discovery
│   ├── delivery.py         # US / LATAM / Pakistan delivery-fit classification
│   ├── export*.py          # CSV/JSONL (export.py), Excel, PDF exports
│   ├── outreach_client.py  # HTTP client for the Outreach hand-off
│   ├── usage_tracker.py    # API usage / quota tracking (api_usage table)
│   ├── models.py           # Pydantic models
│   ├── db.py               # Supabase PostgREST client
│   └── config.py           # Env + ICP loading
├── tests/                  # 323 offline tests
├── schema.sql              # Current DB schema
├── migrations/             # 001-005 SQL migrations
├── prompts/qualify.md      # Qualification prompt
├── vercel_app.py           # Vercel entrypoint (mounts the app at /research)
└── config/icp.yaml         # Ideal Customer Profile

Outreach_Agent/
├── src/outreach/
│   ├── api.py              # Core FastAPI routes (import, campaigns, items, bookings, jobs); includes the routers below
│   ├── cli.py              # Typer CLI (import-prospects, campaign/item/conversation/calendly/automation subcommands)
│   ├── ingest.py           # Prospect import + campaign creation
│   ├── personalize.py      # Email drafting/revision (tiered: qualify data -> cached research -> web search -> template)
│   ├── pipeline.py         # Sending
│   ├── channels/email.py   # SMTP sender (multi-account)
│   ├── research_client.py  # HTTP client for the Research Agent
│   ├── conversation/       # Reply handling: IMAP inbox, classifier, matcher, responder, follow-ups, per-campaign settings
│   ├── automation/         # Scheduled loop: Calendly sync + inbox read + follow-ups (DB lease, AUTO_SYNC_ENABLED)
│   ├── calendly/           # Calendly API client, sync, webhook receiver, routes
│   ├── analytics/          # Read-only /analytics/v2 (per-sender/campaign/prospect metrics)
│   ├── learning/           # Feedback learning from edits & outcomes (/learning/*)
│   ├── optimization/       # Campaign-level strategy optimization (/optimization/*)
│   ├── models.py           # Pydantic models
│   ├── db.py               # Supabase PostgREST client
│   ├── config.py           # Env + outreach.yaml loading
│   └── gemini_keys.py      # Gemini key rotation
├── tests/                  # 724 offline tests
├── prompts/                # LLM prompts (outreach email variants, reply classify/reply/follow-up)
├── config/outreach.yaml    # Sending accounts, personalization, research API base, limits
├── schema.sql              # Current DB schema
├── migrations/             # 001-010, 014, 014b SQL migrations
└── vercel_app.py           # Vercel entrypoint (mounts the app at /outreach)

Dashboard/
├── src/
│   ├── agents/             # Typed HTTP clients (research, outreach, analytics, learning, optimization, calendly) + registry.ts
│   ├── pages/
│   │   ├── Home.tsx
│   │   ├── research/       # Runs (RunConfig), Companies, CompanyDetail, SavedLists, Contacts, Analytics, Settings (+UsageTab)
│   │   └── outreach/       # Prospects, Campaigns, CampaignDetail (kanban), Calendly, Analytics (+AnalyticsDashboard), Learning
│   ├── components/         # Nav, PasswordGate, drawers, modals, charts, ui.tsx design system
│   ├── hooks/useCachedFetch.ts  # Stale-while-revalidate data layer
│   ├── auth.ts             # Login token handling (sessionStorage)
│   ├── router.ts           # Path-based routes
│   ├── App.tsx             # Root
│   └── main.tsx            # Entry point
├── vercel.json             # SPA rewrite
├── package.json
└── tsconfig.json

Server/
├── app.py                  # Composition layer: mounts both agents, dashboard auth, CORS, scheduler lifespan
└── tests/                  # test_single_port.py (mounting, lifespans, WebSockets, CORS, auth, DB isolation)

Repo root: Dockerfile (Railway/Render image running Server), vercel.json (Vercel multi-service config),
Makefile, run.txt (copy-paste run commands), .github/workflows/ci.yml, QUICKSTART.md, DEPLOYMENT.md,
CONTRIBUTING.md, SECURITY.md.
```

## Development workflow

### Start the backend and dashboard
```bash
# Terminal 1: Both agents on :8000 via Server composition layer
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux
pip install -e Research_Agent -e Outreach_Agent -e Server
python -m uvicorn app:app --app-dir Server --reload \
  --reload-dir Research_Agent/src --reload-dir Outreach_Agent/src \
  --reload-dir Server --port 8000

# Terminal 2: Dashboard on :5173
cd Dashboard && npm install && npm run dev
```

### Environment files
Each project has a `.env.example` — copy to `.env` and fill in:
- **Research_Agent/.env**: `RESEARCH_SUPABASE_*`, `GEMINI_API_KEY` (+ `_2`, `_3`), `TAVILY_API_KEY`/`GROQ_API_KEY` (discovery fallback), `ANTHROPIC_API_KEY`, `HUNTER_API_KEY`, `OUTREACH_API_BASE` (enables auto hand-off)
- **Outreach_Agent/.env**: `OUTREACH_SUPABASE_*`, `RESEARCH_API_BASE`, `SMTP_*`, `IMAP_*`, `OUTREACH_FROM_*`, LLM keys, `CALENDLY_*`, `AUTO_SYNC_*`, extra senders via `SENDERn_*`
- **Dashboard/.env**: `VITE_API_URL` (backend origin; `/research` and `/outreach` are appended)
- **Server/.env**: `DASHBOARD_PASSWORD` (login password; unset = protected routes return 503), optional `CSG_API_BASE`

### Running tests
```bash
cd Research_Agent && pytest        # Offline, no secrets needed
cd Outreach_Agent && pytest        # Offline, includes conversation + learning tests
cd Server && pytest                # Composition layer tests (needs all three packages installed in the active venv)
cd Dashboard && npx tsc -b --noEmit  # Type checking only
```

## Key architectural decisions

1. **Three independent agents**: Each owns its database, code, and business logic. No cross-agent coupling beyond API calls.
2. **Server = composition layer only**: Routes `/research/*` → Research Agent, `/outreach/*` → Outreach Agent. No business logic. Can be swapped for separate services if needed.
3. **Dashboard = browser-only SPA**: No backend, no database. Calls agent APIs directly from the browser.
4. **Conversation Agent ≠ separate agent**: A sibling module inside Outreach Agent (shares the same database). Automatic replies/follow-ups run in the same process as campaign sending.
5. **Learning & Optimization = fully additive**: Two independent, read-only sibling modules inside Outreach. They observe outcomes and feed guidance into future drafts, but neither blocks nor modifies the pipeline.

6. **Dashboard auth lives in Server**: `DashboardAuthMiddleware` in `Server/app.py` guards every route (HTTP and WebSocket) except `/auth/login` and the Calendly webhooks, using an HMAC token derived from `DASHBOARD_PASSWORD`. Loopback calls without an `Origin` header (the Research<->Outreach hand-off) bypass it. Standalone agent processes have no auth.
7. **Background automation is opt-in**: with `AUTO_SYNC_ENABLED=true` the Server lifespan starts `outreach.automation.loop`, which syncs Calendly, reads IMAP and runs follow-ups on a DB-leased interval (state in `automation_state`). Off by default.
8. **Global company de-duplication (Research)**: a company found again by a later run is matched (`run_company_matches`), not re-inserted, re-scored or re-handed to Outreach. See `csg/dedupe.py`.

## Common tasks

### Add a new Research Agent API endpoint
1. Add route in `Research_Agent/src/csg/api.py`
2. Add test in `Research_Agent/tests/test_*.py` (for DB-touching tests copy the in-memory fake PostgREST pattern in `test_db_roundtrip.py`; Research has no `conftest.py`)
3. Update `Research_Agent/README.md` if user-facing
4. Dashboard automatically picks it up if it's registered in `src/agents/research.ts`

### Add a new Outreach Agent feature
1. Add domain logic in `Outreach_Agent/src/outreach/<module>/`
2. Add/update Supabase schema in `Outreach_Agent/schema.sql` (or create a migration)
3. Add tests using the in-memory PostgREST fixture in `Outreach_Agent/tests/conftest.py`
4. Add API route in `Outreach_Agent/src/outreach/api.py`
5. Optionally add Dashboard UI in `Dashboard/src/pages/outreach/`

### Deploy to production
See [DEPLOYMENT.md](DEPLOYMENT.md).

### Extend the Dashboard with a new agent's pages
1. Add client in `Dashboard/src/agents/<agent>.ts` (mirroring `research.ts`)
2. Add registry entry in `Dashboard/src/agents/registry.ts`
3. Add pages in `Dashboard/src/pages/<agent>/`
4. Wire routes in `App.tsx` and nav in `components/Nav.tsx`
5. Existing agents' code doesn't change.

## Database schema

Both agents use PostgreSQL via Supabase PostgREST (service-role key, backend only):

**Research Agent** (`Research_Agent/schema.sql`):
- `runs` — research runs (id, label, region, status, stats)
- `companies` — one row per company, keyed by canonical bare-host `domain` (firmographics, ATS, `hiring_signal`, outreach-readiness flags)
- `discovery_sources`, `job_postings`, `scores` (per run), `qualifications` (successes), `qualification_attempts` (success/failure ledger), `contacts`, `saved_companies`, `suppression`
- `run_company_matches` — companies a later run found that already existed (global de-duplication; migration 005)
- `api_usage` — provider call/quota tracking

**Outreach Agent** (`Outreach_Agent/schema.sql`):
- `prospects` — local snapshot of imported Research companies (`research_domain` is the join key, never a shared table)
- `campaigns`, `outreach_items` (stage: needs_review/pending/sent/waiting/replied/booked/not_interested/failed; `sender_email`), `outreach_events` (stage changes, send attempts)
- `conversation_settings`, `conversation_messages` — per-campaign Conversation Agent toggle; replies and drafts
- `followup_attempts`, `automation_state` — follow-up ledger and the DB-leased scheduler state
- `calendly_events` — synced Calendly bookings; `calendly_deleted_events` — tombstones so a booking deleted in the Dashboard is never re-created by sync/webhooks (migration 015)
- `draft_versions`, `learning_insights`, `learning_events` — Learning Agent
- `campaign_optimization_insights`, `optimization_recommendation_adoptions` — Optimization layer

Migrations are numbered in `migrations/NNN_*.sql` and tracked in git. Research has 001-005. Outreach has 001-010, `014_sender_email.sql`, `015_calendly_deleted_events.sql` and the optional `014b_backfill_sender_email.sql` (only valid if every past send came from the single default account); 011-013 were folded into `schema.sql` and deleted. `schema.sql` in each agent is the full current schema and is safe to re-run on an existing project.

## Common issues & solutions

| Issue | Solution |
|-------|----------|
| "One or more agents unreachable" in Dashboard | Verify backend is running on :8000. Check CORS in `Server/app.py`. |
| Tests fail locally but pass in CI | Ensure `.venv/` is activated. Run `pip install -e .` in each agent directory (Server tests need `pip install -e Research_Agent -e Outreach_Agent -e Server`). |
| Every Dashboard request returns 401/503 | 503 = `DASHBOARD_PASSWORD` not set on the server; 401 = wrong/expired token (log in again). |
| TypeScript errors in Dashboard | Run `npx tsc -b --noEmit` to see full error list. |
| Database auth errors | Check `.env` has correct Supabase URL + service-role key for that agent. |
| Email not sending | Verify SMTP credentials in `.env`. Test with a mail client if unsure. |
| Discovery quota exhausted | Configure fallback keys: `GEMINI_API_KEY_2=...`. System auto-falls back to Tavily+Groq if all Gemini keys exhaust. |

## Editing this file

**Do not** remove this CLAUDE.md file. It helps Claude Code understand the project structure and make smarter decisions about edits, refactoring, and new work. Keep it up-to-date when:
- Adding a new agent
- Changing the database schema
- Restructuring the codebase
- Adding new deployment patterns

Coordinate with the project lead before major changes.
