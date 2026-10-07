# Deployment Guide

This system consists of three independent services:

1. **Research Agent** — discovery, scoring, qualification, contacts
2. **Outreach Agent** — campaigns, email drafting, sending, conversation (replies/follow-ups)
3. **Dashboard** — React/Vite UI that calls both agents' APIs

The Research and Outreach agents are typically deployed together as one backend service (using the Server composition layer, `Server/app.py`). The Dashboard is a separate static frontend. Each agent uses its own Supabase project.

Three deployment shapes exist in this repo:

| Shape | Files | Use |
|---|---|---|
| Single container | [Dockerfile](Dockerfile) | Railway/Render/any Docker host: one process serves `/research` and `/outreach` (+ optional background scheduler). **Recommended.** |
| Vercel multi-service | [vercel.json](vercel.json), `Research_Agent/vercel_app.py`, `Outreach_Agent/vercel_app.py` | Dashboard + both agents as three Vercel services. Serverless: no long-lived scheduler. |
| Dashboard only | [Dashboard/vercel.json](Dashboard/vercel.json) | Static SPA host pointing `VITE_API_URL` at a backend elsewhere. |

## Environment Variables

### Research Agent (`.env`)
```
RESEARCH_SUPABASE_URL=https://xxx.supabase.co
RESEARCH_SUPABASE_SERVICE_KEY=eyJ...
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...  # Optional extra keys (rotated when a key's daily quota is exhausted)
ANTHROPIC_API_KEY=...  # Optional (alternative LLM for qualify)
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
OUTREACH_FROM_EMAIL=your-email@gmail.com
IMAP_HOST=imap.gmail.com  # Needed by the Conversation Agent to detect replies
IMAP_USERNAME=your-email@gmail.com
IMAP_PASSWORD=your-app-password
GEMINI_API_KEY=...  # And/or ANTHROPIC_API_KEY / GROQ_API_KEY for drafting (else a static template is used)
CALENDLY_URL=...  # Optional: link offered in reply drafts
# Optional: Calendly integration
CALENDLY_API_TOKEN=...
CALENDLY_WEBHOOK_SIGNING_KEY=...
# Optional: extra sending accounts (each under sending.accounts in config/outreach.yaml)
SENDER2_SMTP_USERNAME=...
SENDER2_SMTP_PASSWORD=...
```

### Server (combine both agents)
When running both agents under one process, configure these in the hosting environment (Render, Railway, etc.):
```
RESEARCH_SUPABASE_URL=https://xxx.supabase.co
RESEARCH_SUPABASE_SERVICE_KEY=eyJ...
OUTREACH_SUPABASE_URL=https://yyy.supabase.co
OUTREACH_SUPABASE_SERVICE_KEY=eyJ...
CSG_API_BASE=https://your-api.example.com  # Optional: advertise to itself
DASHBOARD_PASSWORD=...  # Required: Dashboard login; unset = protected routes return 503
AUTO_SYNC_ENABLED=false  # Optional: true enables background inbox/Calendly polling + follow-ups
AUTO_SYNC_INTERVAL_MINUTES=30  # Optional: only seeds the first interval (5-1440); the Dashboard setting wins afterwards
```

Any other variable from the two agents' `.env` files (LLM, SMTP/IMAP, Calendly, Hunter) must also be present in the process environment on a host with no `.env` files. When `.env` files do exist, `Server/app.py` reads them itself; real environment variables always take precedence.

**Do not** use the legacy `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` names — each agent expects its own.

### Dashboard (`.env`)
```
VITE_API_URL=https://your-api.example.com
```

The Dashboard uses `VITE_API_URL` to append `/research` and `/outreach` automatically.

## Deployment options

### Option 1: Single backend service (recommended)

Deploy one service that runs both agents behind one port using the Server composition layer:

The repo's [Dockerfile](Dockerfile) (Python 3.12-slim) copies `Research_Agent`, `Outreach_Agent` and `Server`, runs `pip install -e` on all three, and starts:

```
export CSG_API_BASE="http://127.0.0.1:${PORT:-8000}"
python -m uvicorn app:app --app-dir Server --host 0.0.0.0 --port ${PORT:-8000}
```

The platform's `PORT` (Railway injects it) is honored, and `CSG_API_BASE` is derived from it so the internal Research↔Outreach hand-off URLs target the right port. Build from the repo root.

**Environment variables** (configure in your hosting platform):
- `RESEARCH_SUPABASE_URL`, `RESEARCH_SUPABASE_SERVICE_KEY`
- `OUTREACH_SUPABASE_URL`, `OUTREACH_SUPABASE_SERVICE_KEY`
- `DASHBOARD_PASSWORD` (required — without it every protected route returns 503)
- API keys: `GEMINI_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, etc.
- Set `OUTREACH_API_BASE` (any non-empty value) only if you want the automatic Research→Outreach hand-off; the Server rewrites it to its own `/outreach` prefix.

**Endpoints**:
- `/research/...` — all Research Agent routes
- `/outreach/...` — all Outreach Agent routes
- `/outreach/analytics/v2/summary` — analytics overview + timeseries (filters: sender, campaign_id, date_from, date_to)
- `/outreach/analytics/v2/prospects` — drill-down prospects list
- `/outreach/automation/status` — scheduler state (`PATCH /outreach/automation/settings`, `POST /outreach/automation/run`)
- `POST /auth/login` — exchange `DASHBOARD_PASSWORD` for a token (all other routes require `Authorization: Bearer <token>`, or `?token=` for WebSockets and download links; the Calendly webhooks are exempt)
- `/docs` — OpenAPI UI (`/research/docs` and `/outreach/docs` per agent)

**CORS**: `Server/app.py` allows the origin `https://multi-agents-beta.vercel.app` plus local `http://localhost|127.0.0.1:5xxx` dev origins. When you host the Dashboard on a different domain, add that origin to `allow_origins` in `Server/app.py` (a code change) or browser requests will be blocked.

**Calendly webhook**: register `https://<backend>/outreach/calendly/webhook` (or the root alias `/calendly/webhook`) and set `CALENDLY_WEBHOOK_SIGNING_KEY`; the signature is verified and unsigned calls are rejected. See [Outreach_Agent/README.md](Outreach_Agent/README.md#calendly-integration).

### Option 2: Separate backend services

Deploy Research and Outreach on different services/ports (not recommended for simplicity, but possible):

**Research service** (from `Research_Agent/`, after `pip install -e .`):
```bash
python -m uvicorn csg.api:app --app-dir src --host 0.0.0.0 --port 8000
```

**Outreach service** (from `Outreach_Agent/`, after `pip install -e .`):
Set `RESEARCH_API_BASE=<research-service-url>` (no `/research` suffix when running standalone), then:
```bash
python -m uvicorn outreach.api:app --app-dir src --host 0.0.0.0 --port 8001
```

Caveats of this mode: there is **no dashboard auth** (the password middleware lives in `Server/app.py`) and **no background scheduler** (automatic polling only runs under the Server). The Dashboard supports a single backend origin with `/research` and `/outreach` appended (`VITE_API_URL`), so put a reverse proxy in front that maps those two prefixes to the two services.

### Option 2b: Vercel multi-service

[vercel.json](vercel.json) declares three services — `dashboard` (Vite), `research_agent` and `outreach_agent` (FastAPI via each agent's `vercel_app.py`, which mounts the app at `/research` / `/outreach`) — with rewrites routing `/research/*` and `/outreach/*` to the right service and everything else to the Dashboard. Service bindings inject `RESEARCH_SERVICE_URL` / `OUTREACH_SERVICE_URL`, which the agents prefer over `RESEARCH_API_BASE` / `OUTREACH_API_BASE`. Each agent service needs its own `*_SUPABASE_*` pair and provider keys. This path bypasses `Server/app.py`, so it has no dashboard password middleware and no background scheduler — use Option 1 if you need either.

### Option 3: Dashboard deployment

The Dashboard is a static-friendly SPA. Deploy it to any static host (Vercel, Netlify, AWS S3, etc.):

```bash
cd Dashboard
npm run build
# Output is in dist/ — deploy this folder (a production bundle built without `VITE_API_URL` throws on load)
```

[Dashboard/vercel.json](Dashboard/vercel.json) rewrites every path to `index.html` for SPA routing. The login token is kept in `sessionStorage`.

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
4. Apply any pending migrations in `migrations/NNN_*.sql` if this is an existing project (or re-run `schema.sql`, which is idempotent). For Research, `005_company_dedup.sql` (the `run_company_matches` table behind global de-duplication) is required on projects created before it. For Outreach, `schema.sql` already includes everything through `014_sender_email.sql` (011-013 were folded in), including `followup_attempts` and `automation_state`; on an existing project only `014_sender_email.sql` may be pending. Run `014b_backfill_sender_email.sql` only after confirming every past send came from the default `OUTREACH_FROM_EMAIL` account.

## Monitoring & logs

- **Backend**: Both agents log to stdout (default for FastAPI/Uvicorn). Capture these in your logging platform.
- **Dashboard**: Client-side errors are typically logged to browser console; use a frontend monitoring service for production.

### Key endpoints for health checks

- `GET /research/docs` — Research Agent OpenAPI (if reachable, agent is up)
- `GET /outreach/docs` — Outreach Agent OpenAPI (if reachable, agent is up)
- `GET /outreach/automation/status` — scheduler state and last cycle result (requires auth)

Neither agent exposes a dedicated `/health` route. Every route except `/auth/login` and the Calendly webhooks needs the dashboard token (the `/docs` pages included), so an unauthenticated probe gets 401/503 — which still proves the process is up.

## Scaling considerations

- **Research discovery** (`csg discover`) can be long-running and memory-intensive; consider async job processing if runs grow large.
- **Outreach personalization** is per-item and LLM-dependent; plan for API rate limits on Gemini/Anthropic/Groq.
- **Conversation Agent** polling is background when `AUTO_SYNC_ENABLED=true` (a DB lease in `automation_state` stops multiple replicas double-running a cycle); otherwise manual (Dashboard button / CLI).
- **Learning & Optimization** recomputation is synchronous per-request; at high volumes, move to event-driven or frontend caching.
- **Analytics** uses paginated fetch to work around Supabase's 1000-row cap; high-volume instances may benefit from a dedicated aggregate store.

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
- Verify `VITE_API_URL` points to a running backend (it is baked in at build time).
- Check CORS is configured correctly — the deployed Dashboard's origin must be in `allow_origins` in [Server/app.py](Server/app.py).
- A login screen that rejects the right password, or 503s everywhere, means `DASHBOARD_PASSWORD` is unset or different on the server.
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
- Both agents stream background-job progress over WebSockets (`/research/jobs/{id}/ws`, `/outreach/jobs/{id}/ws`), so configure the proxy to upgrade the connection.
- Set `X-Forwarded-For`, `X-Forwarded-Proto` headers so the backend knows the real client IP and protocol.
- The Backend's CORS is configured in [Server/app.py](Server/app.py); adjust if needed.
