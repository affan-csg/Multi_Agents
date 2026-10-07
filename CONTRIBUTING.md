# Contributing to CSG Agents

This project consists of three independent agents (Research, Outreach, Server) and a shared Dashboard. Each agent has its own business logic, database, and test suite.

## Development Setup

### One-time setup
```bash
# Root-level venv for all agents and server
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

# Install all three agents and server in editable mode
pip install -e Research_Agent -e Outreach_Agent -e Server

# Install Dashboard dependencies
cd Dashboard
npm install
cd ..
```

### Running the full stack

**Terminal 1 — Backend (both agents + server on :8000)**
```bash
# Activate the shared venv, then:
python -m uvicorn app:app --app-dir Server --reload \
  --reload-dir Research_Agent/src --reload-dir Outreach_Agent/src --reload-dir Server \
  --port 8000
```

**Terminal 2 — Dashboard (:5173)**
```bash
cd Dashboard
npm run dev
```

See each project's README for environment variable setup:
- [Research_Agent/README.md](Research_Agent/README.md#setup)
- [Outreach_Agent/README.md](Outreach_Agent/README.md#setup)
- [Dashboard/README.md](Dashboard/README.md#setup)

## Testing

All tests run offline against an in-memory PostgREST fixture — no live database or API keys needed.

```bash
# Research Agent (323 tests)
cd Research_Agent && pytest

# Outreach Agent (724 tests: Outreach + Conversation + Learning + Optimization + Calendly + Automation + Analytics)
cd Outreach_Agent && pytest

# Server (composition layer tests; needs all three packages installed in the active venv)
cd Server && pytest

# Dashboard (TypeScript type checking)
cd Dashboard && npx tsc -b --noEmit
```

Or `make test` runs all of the above. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the same four jobs on push to `main` and on PRs (Python 3.11, Node 20; the Dashboard typecheck step is currently `continue-on-error`).

The in-memory fake PostgREST used by the suites lives in `Outreach_Agent/tests/conftest.py` and, for Research, `Research_Agent/tests/test_db_roundtrip.py` (Research has no `conftest.py`).

## Code style & conventions

- **Python**: Follow PEP 8. Type hints are expected. Use `pytest` for tests.
- **TypeScript/React**: Run `npx tsc -b --noEmit` to catch type errors (`npm run lint` runs oxlint). Follow existing patterns in `src/agents/` and `src/pages/`. The Makefile has no formatter configured.
- **SQL**: Migrations go in `*/migrations/` numbered sequentially; `schema.sql` is the complete current schema.

## Project boundaries

Each agent is **completely independent**:
- **Research Agent** (`Research_Agent/`): Discovery, scoring, qualification, contacts. Uses its own Supabase project.
- **Outreach Agent** (`Outreach_Agent/`): Campaign building, email drafting, sending, conversation (replies/follow-ups), learning & optimization. Uses its own Supabase project. Calls Research's API to fetch prospect data.
- **Server** (`Server/`): Mounts both agents' FastAPI apps under one process on one port. No business logic — only composition, dashboard password auth (`DASHBOARD_PASSWORD`), CORS, and the opt-in background scheduler lifespan.
- **Dashboard** (`Dashboard/`): React/Vite UI. Calls both agents' APIs directly from the browser. No backend or database.

**Do not**:
- Merge databases (they are intentionally separate)
- Add cross-agent business logic to the Server layer (keep it a simple composition layer)
- Couple the Dashboard to agent-specific internals (use public APIs)

## Making changes

### Adding a feature

1. **Clarify scope**: Is it Research, Outreach, Dashboard, or multi-agent?
2. **Add tests first** (or alongside). Both agents' test suites are comprehensive and run offline.
3. **Update schemas** if you add/modify database tables (edit `schema.sql` and create a migration in `migrations/NNN_*.sql`). Research migrations are 001-005; Outreach uses 001-010, 014, 014b (011-013 were folded into `schema.sql`) — pick the next free number.
4. **Update READMEs** for user-facing changes (setup, CLI commands, config options, deployment).

### Database changes

Migrations are numbered and run sequentially. After editing `schema.sql`:
1. Create a new file `migrations/NNN_description.sql` with only your changes.
2. Run it in Supabase's SQL editor to test.
3. Update `schema.sql` to include the change (so new projects get it from day one).
4. Commit both files together.

### Updating CI

The workflow in `.github/workflows/ci.yml` runs tests for each agent independently, plus the Server (which installs all three). If you add a new test file or change test structure, update the workflow's test commands.

## Deployment notes

- Each agent and the Dashboard can be deployed independently.
- Research and Outreach use their own Supabase projects — configure environment variables per project.
- The Server needs all four Supabase env vars (both projects' URLs and service keys) to run.
- The Server also needs `DASHBOARD_PASSWORD` (login; unset = 503 on protected routes) and optionally `AUTO_SYNC_ENABLED=true` for background polling.
- Dashboard's `VITE_API_URL` points to the backend origin (defaults to `:8000` for local dev; required for production builds). A non-default Dashboard origin must be added to `allow_origins` in `Server/app.py`.
- See [DEPLOYMENT.md](DEPLOYMENT.md) for the Docker, Vercel and standalone options.

See [Server/README.md#setup](Server/README.md#setup) for production environment variable names.

## Questions?

- Architecture: See root [README.md](README.md) for system diagrams and module overview.
- Agent-specific details: Check the agent's own README and `src/` structure.
- Tests: Look at `tests/conftest.py` in each agent for mock fixtures.
