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
# Research Agent (299+ tests)
cd Research_Agent && pytest

# Outreach Agent (592+ tests: Outreach + Conversation + Learning + Optimization)
cd Outreach_Agent && pytest

# Server (composition layer tests)
cd Server && pytest

# Dashboard (TypeScript type checking)
cd Dashboard && npx tsc -b --noEmit
```

Or run all via CI:
```bash
.github/workflows/ci.yml  # Runs on push to main and PRs
```

## Code style & conventions

- **Python**: Follow PEP 8. Type hints are expected. Use `pytest` for tests.
- **TypeScript/React**: Run `tsc` to catch type errors. Follow existing patterns in `src/agents/` and `src/pages/`.
- **SQL**: Migrations go in `*/migrations/` numbered sequentially; `schema.sql` is the complete current schema.

## Project boundaries

Each agent is **completely independent**:
- **Research Agent** (`Research_Agent/`): Discovery, scoring, qualification, contacts. Uses its own Supabase project.
- **Outreach Agent** (`Outreach_Agent/`): Campaign building, email drafting, sending, conversation (replies/follow-ups), learning & optimization. Uses its own Supabase project. Calls Research's API to fetch prospect data.
- **Server** (`Server/`): Mounts both agents' FastAPI apps under one process on one port. No business logic.
- **Dashboard** (`Dashboard/`): React/Vite UI. Calls both agents' APIs directly from the browser. No backend or database.

**Do not**:
- Merge databases (they are intentionally separate)
- Add cross-agent business logic to the Server layer (keep it a simple composition layer)
- Couple the Dashboard to agent-specific internals (use public APIs)

## Making changes

### Adding a feature

1. **Clarify scope**: Is it Research, Outreach, Dashboard, or multi-agent?
2. **Add tests first** (or alongside). Both agents' test suites are comprehensive and run offline.
3. **Update schemas** if you add/modify database tables (edit `schema.sql` and create a migration in `migrations/NNN_*.sql`).
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
- Dashboard's `VITE_API_URL` points to the backend origin (defaults to `:8000` for local dev).

See [Server/README.md#setup](Server/README.md#setup) for production environment variable names.

## Questions?

- Architecture: See root [README.md](README.md) for system diagrams and module overview.
- Agent-specific details: Check the agent's own README and `src/` structure.
- Tests: Look at `tests/conftest.py` in each agent for mock fixtures.
