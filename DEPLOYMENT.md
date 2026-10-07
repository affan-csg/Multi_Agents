# Deployment Guide

This system consists of three independent services:

1. **Research Agent** — discovery, scoring, qualification, contacts
2. **Outreach Agent** — campaigns, email drafting, sending, conversation (replies/follow-ups)
3. **Dashboard** — React/Vite UI that calls both agents' APIs

The Research and Outreach agents are typically deployed together as one backend service (using the Server composition layer) on port 8000. The Dashboard is a separate static/dynamic frontend service. Each agent uses its own Supabase project.

## Environment Variables

### Research Agent (`.env`)
```
RESEARCH_SUPABASE_URL=https://xxx.supabase.co
RESEARCH_SUPABASE_SERVICE_KEY=eyJ...
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...  # Optional fallback keys
ANTHROPIC_API_KEY=...  # Optional
TAVILY_API_KEY=...  # Optional fallback
GROQ_API_KEY=...  # Optional fallback
HUNTER_API_KEY=...  # For contact discovery
OUTREACH_API_BASE=http://localhost:8000/outreach  # Optional: enables automatic hand-off
```

### Outreach Agent (`.env`)
```
OUTREACH_SUPABASE_URL=https://yyy.supabase.co
OUTREACH_SUPABASE_SERVICE_KEY=eyJ...
RESEARCH_API_BASE=http://localhost:8000/research  # Where to fetch prospects
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
# Optional: Calendly integration
CALENDLY_API_TOKEN=...
CALENDLY_WEBHOOK_SECRET=...
```

### Server (combine both agents)
When running both agents under one process, configure these in the hosting environment (Render, Railway, etc.):
```
RESEARCH_SUPABASE_URL=https://xxx.supabase.co
RESEARCH_SUPABASE_SERVICE_KEY=eyJ...
OUTREACH_SUPABASE_URL=https://yyy.supabase.co
OUTREACH_SUPABASE_SERVICE_KEY=eyJ...
CSG_API_BASE=https://your-api.example.com  # Optional: advertise to itself
```

**Do not** use the legacy `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` names — each agent expects its own.

### Dashboard (`.env`)
```
VITE_RESEARCH_API_BASE=http://localhost:8000/research
VITE_OUTREACH_API_BASE=http://localhost:8000/outreach
# OR for production:
VITE_API_URL=https://your-api.example.com
```

When `VITE_API_URL` is set, the Dashboard appends `/research` and `/outreach` automatically. For local dev, use the explicit per-agent URLs.

## Deployment options

### Option 1: Single backend service (recommended)

Deploy one service that runs both agents behind one port using the Server composition layer:

**Dockerfile** (if using Railway, Render, or similar):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -e Research_Agent -e Outreach_Agent -e Server
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--app-dir", "Server", "--host", "0.0.0.0", "--port", "8000"]
```

**Environment variables** (configure in your hosting platform):
- `RESEARCH_SUPABASE_URL`, `RESEARCH_SUPABASE_SERVICE_KEY`
- `OUTREACH_SUPABASE_URL`, `OUTREACH_SUPABASE_SERVICE_KEY`
- API keys: `GEMINI_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, etc.

**Endpoints**:
- `/research/...` — all Research Agent routes
- `/outreach/...` — all Outreach Agent routes
- `/docs` — OpenAPI UI

### Option 2: Separate backend services

Deploy Research and Outreach on different services/ports (not recommended for simplicity, but possible):

**Research service**:
```bash
python -m uvicorn src.csg.api:app --host 0.0.0.0 --port 8000
```

**Outreach service**:
Set `RESEARCH_API_BASE=<research-service-url>`, then:
```bash
python -m uvicorn src.outreach.api:app --host 0.0.0.0 --port 8001
```

Adjust Dashboard's `.env` accordingly (explicit per-agent URLs).

### Option 3: Dashboard deployment

The Dashboard is a static-friendly SPA. Deploy it to any static host (Vercel, Netlify, AWS S3, etc.):

```bash
cd Dashboard
npm run build
# Output is in dist/ — deploy this folder
```

Set `VITE_API_URL` in your hosting environment to point to your backend:
```
VITE_API_URL=https://your-api.example.com
```

## Database setup

Each agent uses its own Supabase project. For each project:

1. Create a new Supabase project.
2. In the SQL editor, paste the schema:
   - Research Agent: [Research_Agent/schema.sql](Research_Agent/schema.sql)
   - Outreach Agent: [Outreach_Agent/schema.sql](Outreach_Agent/schema.sql)
3. Copy the project's URL and service-role key (Settings → API) to your `.env`.
4. Apply any pending migrations in `migrations/NNN_*.sql` if this is an existing project.

## Monitoring & logs

- **Backend**: Both agents log to stdout (default for FastAPI/Uvicorn). Capture these in your logging platform.
- **Dashboard**: Client-side errors are typically logged to browser console; use a frontend monitoring service for production.

### Key endpoints for health checks

- `GET /research/docs` — Research Agent OpenAPI (if reachable, agent is up)
- `GET /outreach/docs` — Outreach Agent OpenAPI (if reachable, agent is up)
- `GET /research/health` or equivalent — if agent exports a health endpoint (check agent's README)

## Scaling considerations

- **Research discovery** (`csg discover`) can be long-running and memory-intensive; consider async job processing if runs grow large.
- **Outreach personalization** is per-item and LLM-dependent; plan for API rate limits on Gemini/Anthropic/Groq.
- **Conversation Agent** polling is currently manual (Dashboard button); for larger volumes, add a background poller.
- **Learning & Optimization** recomputation is synchronous per-request; at high volumes, move to event-driven or frontend caching.

## Rollback & disaster recovery

- Each agent's database is separate. Deleting or redeploying one never affects the other.
- Dashboard has no persistence; a deployment/redeploy has no data loss risk.
- To rollback: Revert to a prior git commit, redeploy the service, and your data remains untouched (both agents' databases are on Supabase).

## Troubleshooting

### Backend won't start
- Check all four Supabase env vars are set (Research and Outreach URLs + service keys).
- Verify API key env vars exist (at least one LLM key for Research, SMTP for Outreach).
- Check Python version is 3.11+.

### Dashboard shows "One or more agents unreachable"
- Verify `VITE_API_URL` (or `VITE_RESEARCH_API_BASE`/`VITE_OUTREACH_API_BASE`) points to a running backend.
- Check CORS is configured correctly (see [Server/app.py](Server/app.py)).
- Verify the backend port is accessible from the Dashboard's network.

### Email not sending
- Verify SMTP credentials are correct (test with `telnet` or a mail client).
- Check Gmail requires app passwords, not account password.
- Ensure the SMTP host allows connections on the given port (587 for TLS, 465 for SSL).

### Discovery quota exhausted
- If all `GEMINI_API_KEY*` are out of quota, the system falls back to Tavily + Groq. Ensure both are configured.
- Add more Gemini keys (`GEMINI_API_KEY_2`, etc.) for higher throughput.

## Reverse proxy / load balancing

If you're running behind a reverse proxy (nginx, CloudFlare, etc.):

- Ensure the proxy forwards all HTTP methods (GET, POST, PUT, PATCH, DELETE).
- If using WebSockets (Outreach Agent may for real-time updates), configure the proxy to upgrade the connection.
- Set `X-Forwarded-For`, `X-Forwarded-Proto` headers so the backend knows the real client IP and protocol.
- The Backend's CORS is configured in [Server/app.py](Server/app.py); adjust if needed.
