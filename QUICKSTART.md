# Quick Start Guide

Get the full CSG Agents system (Research + Outreach + Dashboard) running in ~5 minutes.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Two terminal windows/tabs
- Two Supabase projects (one per agent) with each `schema.sql` applied
- API keys: Gemini (required for discovery), SMTP credentials (required to send), IMAP credentials (to detect replies), Hunter.io (contacts), Anthropic, Groq+Tavily (optional)

## 1. Clone & navigate

```bash
git clone <repo-url>
cd agents
```

## 2. Set up environment files

Each project needs a `.env` file with its API keys. Copy and fill in:

```bash
# Research Agent
cp Research_Agent/.env.example Research_Agent/.env
# Edit and fill: RESEARCH_SUPABASE_URL, RESEARCH_SUPABASE_SERVICE_KEY, GEMINI_API_KEY, HUNTER_API_KEY

# Outreach Agent
cp Outreach_Agent/.env.example Outreach_Agent/.env
# Edit and fill: OUTREACH_SUPABASE_URL, OUTREACH_SUPABASE_SERVICE_KEY, SMTP_*, RESEARCH_API_BASE

# Server (Dashboard login password)
cp Server/.env.example Server/.env
# Edit and fill: DASHBOARD_PASSWORD  (unset = every protected route returns 503)

# Dashboard
cp Dashboard/.env.example Dashboard/.env
# VITE_API_URL defaults to http://127.0.0.1:8000 -- usually fine for local dev
```

Apply each agent's `schema.sql` in its own Supabase project's SQL editor
(`Research_Agent/schema.sql`, `Outreach_Agent/schema.sql`) before the first run.

See the full setup docs in each project's README for details.

## 3. One-time installation

```bash
# Create and activate a Python venv for both agents
python -m venv .venv

# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install Python packages
pip install -e Research_Agent -e Outreach_Agent -e Server

# Install Dashboard dependencies
cd Dashboard && npm ci && cd ..
```

Or use the Makefile:

```bash
make install
```

## 4. Run the backend (Terminal 1)

```bash
python -m uvicorn app:app --app-dir Server --reload \
  --reload-dir Research_Agent/src --reload-dir Outreach_Agent/src \
  --reload-dir Server --port 8000
```

Or use the Makefile:

```bash
make backend
```

Wait for the output: `Uvicorn running on http://127.0.0.1:8000` (with hot-reload enabled).

**API endpoints:**
- Research Agent: http://127.0.0.1:8000/research/docs
- Outreach Agent: http://127.0.0.1:8000/outreach/docs
- Combined API: http://127.0.0.1:8000/docs

## 5. Run the Dashboard (Terminal 2)

```bash
cd Dashboard && npm run dev
```

Or use the Makefile:

```bash
make dashboard
```

The Dashboard opens at http://localhost:5173 and asks for the
`DASHBOARD_PASSWORD` you set in `Server/.env`. If the port is taken, Vite picks
the next free one (the backend accepts any local `:5xxx` origin).

## 6. Test the system

Both agents are fully tested offline (no secrets or live APIs needed for tests).
Run these inside the activated venv (the Server suite imports both agents):

```bash
# Terminal 3 (or after stopping the backend)
make test          # Run all tests
# or individually:
make test-research
make test-outreach
make test-server
make test-dashboard
```

## What's next?

- **Explore the UI**: Open http://localhost:5173 and poke around
- **Run a research discovery**: In the Dashboard, Research tab → Runs → New Run
- **Create a campaign**: Import prospects into Outreach, draft emails, review and send
- **Check conversations**: Switch on the Conversation Agent per campaign (it drafts replies; `auto_reply` sends them). Set `AUTO_SYNC_ENABLED=true` to poll the inbox and Calendly automatically
- **View learning insights**: Outreach → Learning & Optimization tab to see patterns and recommendations

## Common commands

| Goal | Command |
|------|---------|
| Full stack (backend + dashboard) | `make backend` (term 1), `make dashboard` (term 2) |
| Run all tests | `make test` |
| Clean up cache/build files | `make clean` |
| View all commands | `make help` |

## Troubleshooting

**"One or more agents unreachable" in Dashboard?**
- Verify backend is running on :8000 (`http://localhost:8000/docs`)
- A 401/503 on every request means the login password is wrong or `DASHBOARD_PASSWORD` is unset on the server
- Check `.env` files have correct Supabase URLs

**Import fails in Outreach?**
- Verify `RESEARCH_API_BASE` in `Outreach_Agent/.env` points to `:8000/research` (or your backend URL)
- Verify Research Agent is running and has data

**Email won't send?**
- Double-check SMTP credentials (Gmail requires app-specific passwords, not account password)
- Ensure SMTP host/port are correct (Gmail: `smtp.gmail.com:587`)

**Tests fail?**
- Ensure `.venv` is activated: `which python` should show `.venv/...`
- Reinstall: `pip install -e Research_Agent -e Outreach_Agent -e Server`

**More issues?** See [CONTRIBUTING.md](CONTRIBUTING.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

## Next steps

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines
- Check [DEPLOYMENT.md](DEPLOYMENT.md) for production setup
- See [CLAUDE.md](CLAUDE.md) for full project structure (especially useful for Claude Code)
- Each agent has its own README with detailed configuration options
