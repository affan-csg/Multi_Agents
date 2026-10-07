# Quick Start Guide

Get the full CSG Agents system (Research + Outreach + Dashboard) running in ~5 minutes.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Two terminal windows/tabs
- API keys: Gemini (required), SMTP credentials (required), Hunter.io, Anthropic, Groq+Tavily (optional)

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

# Dashboard
cp Dashboard/.env.example Dashboard/.env
# Defaults are usually fine for local dev
```

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

The Dashboard opens at http://localhost:5173

## 6. Test the system

Both agents are fully tested offline (no secrets or live APIs needed for tests):

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
- **Check conversations**: Switch on the Conversation Agent per campaign to auto-reply to prospects
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
