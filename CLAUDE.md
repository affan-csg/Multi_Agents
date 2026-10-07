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
│   ├── api.py              # FastAPI routes
│   ├── discovery/          # Company discovery (Gemini, Groq+Tavily)
│   ├── scoring/            # ICP-based scoring rules
│   ├── models.py           # Pydantic data models
│   ├── db.py               # Supabase PostgREST client
│   └── cli.py              # Typer CLI (csg discover, csg report, etc.)
├── tests/                  # 299+ offline tests
├── schema.sql              # Current DB schema
├── migrations/             # Sequential SQL migrations
└── config/icp.yaml         # Ideal Customer Profile

Outreach_Agent/
├── src/outreach/
│   ├── api.py              # FastAPI routes
│   ├── campaigns/          # Campaign logic
│   ├── personalization/    # Email drafting & personalization
│   ├── channels/email.py   # SMTP email sender
│   ├── conversation/       # Reply handling, LLM conversation agent
│   ├── analytics/          # Read-only /analytics/v2 (per-sender/campaign/booking metrics)
│   ├── automation/         # Scheduled inbox/Calendly polling + follow-ups
│   ├── learning/           # Feedback/learning from edits & outcomes
│   ├── optimization/       # Campaign-level strategy optimization
│   ├── calendly/           # Calendly API & webhook integration
│   ├── models.py           # Pydantic data models
│   ├── db.py               # Supabase PostgREST client
│   └── cli.py              # Typer CLI (campaign operations, imports)
├── tests/                  # 592+ offline tests
├── schema.sql              # Current DB schema
└── migrations/             # Sequential SQL migrations

Dashboard/
├── src/
│   ├── agents/             # Typed HTTP clients (research.ts, outreach.ts)
│   ├── pages/              # UI components
│   │   ├── research/       # Runs, Companies, Contacts, Settings, Analytics
│   │   └── outreach/       # Prospects, Campaigns, Campaign Detail, Calendly
│   ├── components/         # Shared UI components (Nav, Layout, etc.)
│   ├── App.tsx             # Root
│   └── main.tsx            # Entry point
├── package.json
└── tsconfig.json

Server/
├── app.py                  # FastAPI composition layer (mounts both agents)
└── tests/                  # 10+ tests for single-port setup
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
- **Research_Agent/.env**: `RESEARCH_SUPABASE_*`, `GEMINI_API_KEY`, `HUNTER_API_KEY`, etc.
- **Outreach_Agent/.env**: `OUTREACH_SUPABASE_*`, `SMTP_*`, `RESEARCH_API_BASE`, etc.
- **Dashboard/.env**: `VITE_API_URL` (backend origin; `/research` and `/outreach` are appended)
- **Server/.env**: `DASHBOARD_PASSWORD` (login password; unset = protected routes return 503)

### Running tests
```bash
cd Research_Agent && pytest        # Offline, no secrets needed
cd Outreach_Agent && pytest        # Offline, includes conversation + learning tests
cd Server && pytest                # Composition layer tests
cd Dashboard && npx tsc -b --noEmit  # Type checking only
```

## Key architectural decisions

1. **Three independent agents**: Each owns its database, code, and business logic. No cross-agent coupling beyond API calls.
2. **Server = composition layer only**: Routes `/research/*` → Research Agent, `/outreach/*` → Outreach Agent. No business logic. Can be swapped for separate services if needed.
3. **Dashboard = browser-only SPA**: No backend, no database. Calls agent APIs directly from the browser.
4. **Conversation Agent ≠ separate agent**: A sibling module inside Outreach Agent (shares the same database). Automatic replies/follow-ups run in the same process as campaign sending.
5. **Learning & Optimization = fully additive**: Two independent, read-only sibling modules inside Outreach. They observe outcomes and feed guidance into future drafts, but neither blocks nor modifies the pipeline.

## Common tasks

### Add a new Research Agent API endpoint
1. Add route in `Research_Agent/src/csg/api.py`
2. Add test in `Research_Agent/tests/test_*.py` (use the PostgREST fixture in `conftest.py`)
3. Update `Research_Agent/README.md` if user-facing
4. Dashboard automatically picks it up if it's registered in `src/agents/research.ts`

### Add a new Outreach Agent feature
1. Add domain logic in `Outreach_Agent/src/outreach/<module>/`
2. Add/update Supabase schema in `Outreach_Agent/schema.sql` (or create a migration)
3. Add tests using the PostgREST fixture
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

Both agents use PostgreSQL via Supabase PostgREST:

**Research Agent** (`Research_Agent/schema.sql`):
- `runs` — discovery batches (ID, timestamp, region, status)
- `prospects` — companies (domain, name, tier, score, hiring signal, contacts, qualifications)
- `qualification_attempts` — LLM attempt log (success/failure reason)
- `outreach_readiness` — when a prospect is ready to export

**Outreach Agent** (`Outreach_Agent/schema.sql`):
- `campaigns` — email campaigns (ID, name, research_run_id)
- `outreach_items` — individual prospect + email in a campaign (stage: needs_review/pending/sent/waiting/replied/booked/failed)
- `outreach_messages` — sent emails (subject, body, to, status)
- `conversation_messages` — replies and drafts (thread_id, role, message, classification)
- `draft_versions` — AI + human edits, for learning
- `learning_insights` — detected patterns from edits
- `campaign_optimizations` — strategy recommendations + adoption tracking

All migrations are numbered in `migrations/NNN_*.sql` and tracked in git. Outreach migrations 011-013 were folded into `schema.sql` and deleted; `014_sender_email.sql` adds `outreach_items.sender_email` (per-send sender attribution for analytics), and `014b_backfill_sender_email.sql` is an optional one-time backfill, only valid if every past send came from the single default account.

## Common issues & solutions

| Issue | Solution |
|-------|----------|
| "One or more agents unreachable" in Dashboard | Verify backend is running on :8000. Check CORS in `Server/app.py`. |
| Tests fail locally but pass in CI | Ensure `.venv/` is activated. Run `pip install -e .` in each agent directory. |
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
