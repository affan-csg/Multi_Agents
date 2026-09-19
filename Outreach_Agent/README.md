# CSG Outreach Agent

Personalizes and sends outreach to prospects qualified by the Research Agent.

**It never touches the Research Agent's database.** It pulls qualified
companies over HTTP from Research's own API, caches them locally, selects the
top-N into a campaign, drafts a personalized email per prospect (LLM, using
Research's own `pain_summary`/`recommended_angle`), and sends it. Everything
after Sent — Waiting, Replied, Booked — is moved by a person, not this agent.

**Import + campaign creation can happen two ways**: by hand (CLI or the
Dashboard's Prospects/Campaigns pages), or **automatically**, triggered by
the Research Agent right after it qualifies + finds contacts for a run's
Tier A/B companies — see [Research_Agent/README.md's "Outreach
hand-off"](../Research_Agent/README.md#outreach-hand-off). Either way, the
exact same `import_prospects()`/`create_campaign_from_prospects()` code runs
— this agent has no separate "automatic" code path, only a caller that hits
the same `POST /import` / `POST /campaigns` routes its own frontend uses.
Sending is never automatic either way — a campaign always stops at a
drafted, reviewable state.

```
Research Agent's API ─► Import ─► Campaign (top-N) ─► Personalize ─► Send
     (by hand, or automatically                                        │
      after Research qualifies a run)        Pending ─► Sent ─► Waiting ─► Replied ─► Booked
                                                        (manual from here on)
```

A research run is *meant* to back at most one real campaign — creating a
second against the same run is blocked in the Dashboard's campaign-creation
form, but **`POST /campaigns` itself enforces nothing server-side**; the
guard is a client-side UX rule in
`Dashboard/src/pages/outreach/Campaigns.tsx`, checked against the
already-loaded campaign list. The synthetic Test Campaign above is exempt
since its `research_run_id` is always `null`.

This means the automatic Research→Outreach hand-off (see above), which
calls `POST /campaigns` directly and bypasses the Dashboard form entirely,
is **not** covered by that guard either. In practice this is not a problem
today — a fresh `csg discover`/`POST /discover` always mints a new
timestamp-based `run_id`, and resuming a stalled run
([discover.resume_stalled()](../Research_Agent/src/csg/discover.py)) does
not re-trigger the hand-off at all — but if you ever build a path that could
call the automatic hand-off twice for the same `run_id` (e.g. manually
re-invoking it, or a future retry mechanism), know that it would currently
create a second campaign, not skip it. Worth a real server-side guard on
`POST /campaigns` if that ever becomes a live risk rather than a
theoretical one.

## Stack

- **Backend**: Python (FastAPI + Typer CLI), talking to its own Supabase
  project (Postgres) over PostgREST via `httpx` — see
  [src/outreach/db.py](src/outreach/db.py). This is a **separate** Supabase
  project from the Research Agent's; the two never share a database.
- **Frontend**: none of its own by design. Its pages
  (Prospects/Campaigns/Campaign Detail) live in the
  [Dashboard](../Dashboard)'s `src/pages/outreach/`, the same way every other
  agent's UI is reached — see Dashboard's README.
- **Email**: provider-agnostic SMTP (`src/outreach/channels/email.py`) —
  works with Gmail SMTP relay, SendGrid, Postmark, SES, etc., whichever
  `SMTP_HOST` points at.

## Setup

1. **Python deps** (requires Python ≥3.11):
   ```
   pip install -e .
   ```
2. **Environment**: copy `.env.example` to `.env` and fill in:
   - `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — required for everything. Get
     these from **this** Outreach project's Supabase Settings → API page —
     not the Research Agent's project.
   - `RESEARCH_API_BASE` — where the Research Agent's backend is running
     (defaults to `http://127.0.0.1:8000`, i.e. `config/outreach.yaml`'s
     `research.api_base` unless overridden here). Outreach pulls prospects
     from `GET /runs/{run_id}/companies` and `/companies/{domain}` — the
     Research Agent's backend must be running for imports to work.
   - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` — required
     to actually send email. For Gmail: use an **App Password**
     (myaccount.google.com/apppasswords, needs 2FA enabled), not your normal
     Gmail password. Leave these blank and set `sending.dry_run: true` in
     `config/outreach.yaml` to exercise the pipeline without sending real
     email.
   - `OUTREACH_FROM_EMAIL` / `OUTREACH_FROM_NAME` — the From header on sent
     mail.
   - `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` — optional, for personalizing the
     drafted email from a prospect's `pain_summary`/`recommended_angle`, or
     (if that's not available yet) from other cached research fields
     directly. Gemini tried first if both are set, same convention as the
     Research Agent's `csg qualify`. Without either, a plain templated email
     is sent instead (see
     [src/outreach/personalize.py](src/outreach/personalize.py)).
   - `GROQ_API_KEY` / `TAVILY_API_KEY` — optional, used only by the Redraft
     button's "go find more information" tier (live web search), as a
     backend independent of Gemini/Anthropic: Tavily does the actual search,
     Groq drafts from its results. Tried between Gemini+`google_search` and
     Anthropic+`web_search`; skipped (not an error) if either key is unset.
3. **Database**: paste [schema.sql](schema.sql) into **this** Outreach
   Supabase project's SQL editor (a fresh project, separate from Research's).

## Running it

**CLI:**
```
outreach import-prospects --run <run_id>   # pull qualified prospects from Research
outreach campaign create --limit 25        # select top-N cached prospects into a campaign
outreach campaign send <campaign_id>       # draft + send email for pending items
outreach campaign pipeline <campaign_id>   # the 5-stage pipeline board, as a table
outreach item set-stage <item_id> <stage>  # manually advance past 'sent'
```

### Test Campaign — QA fixture, no CLI equivalent

`POST /campaigns/test` (Dashboard: Campaigns → New campaign → "Test Campaign")
creates a fixed, repeatable campaign with two synthetic prospects so the full
pipeline UI (creation, sending, stage changes, kanban drag-and-drop) can be
exercised without touching real prospects or a Research run:

- `affanjan007@gmail.com` → lands directly in `pending`.
- `affanjan008@gmail.com` → lands directly in `needs_review` (Manual Review).

Both bodies are built from the *same* `personalize._sender_signature()` /
`draft_message()` machinery real campaigns use — never hand-duplicated
content — so a test send looks and links exactly like a real one (signature
block, website link included). See
[`create_test_campaign()`](src/outreach/ingest.py) and `_TEST_PROSPECTS`.

Nothing is ever sent automatically; a person still has to click Send. The
campaign's `research_run_id` is always `null` and `selection` is tagged
`{"test": true}`, which is what exempts it from the "one campaign per
research run" guard below and keeps it from ever being mistaken for a real
campaign in the Campaigns list or Analytics scope selector.

### Running both backends (required for the web app)

Outreach imports prospects from the Research Agent over HTTP, so **both
backends need to be running at the same time**, each in its own terminal —
Outreach on its own does not work standalone once you're past the CLI's
`import-prospects` step:

```
# Terminal 1 -- Research Agent's backend, :8000
cd ../Research_Agent
python -m uvicorn csg.api:app --app-dir src --reload --port 8000

# Terminal 2 -- this (Outreach Agent's) backend, :8001
cd ../Outreach_Agent
python -m uvicorn outreach.api:app --app-dir src --reload --port 8001
```

Use `python -m uvicorn ...`, not the bare `uvicorn` command — on Windows, a
venv's `uvicorn.exe` launcher hardcodes an absolute path to `python.exe` at
creation time, so if a venv folder is ever moved/renamed after `pip
install`, the launcher breaks with `Fatal error in launcher: Unable to
create process...` even though the venv itself is fine. `python -m uvicorn`
runs uvicorn as a module through `python.exe` directly instead, sidestepping
the launcher entirely. Also, on Windows PowerShell (not pwsh/PowerShell 7),
`&&` is not a valid command separator — put each command on its own line, as
above, or use `;` if you want to chain them.

`--port 8001` here is not optional the way `--port 8000` is for Research's
backend — the Dashboard's `VITE_OUTREACH_API_BASE` (see
[Dashboard/.env.example](../Dashboard/.env.example)) expects Outreach on
`:8001` specifically. If the Dashboard's sidebar shows "agent unreachable"
for Outreach, this backend usually isn't running.

### Adding the web UI (Dashboard, a third process)

Outreach has no frontend of its own — its pages live inside the shared
[Dashboard](../Dashboard) app. With both backends above already running:

```
# Terminal 3 -- the Dashboard frontend, http://127.0.0.1:5173
cd ../Dashboard
npm install
npm run dev
```

Open the Dashboard, go to **Outreach Agent → Campaigns**, and click into a
campaign to see the pipeline board (Needs Review → Pending → Sent → Waiting →
Replied → Booked). Cards from Sent onward are drag-and-drop between columns
(in addition to the item drawer's stage buttons — both call the same
`POST /items/{id}/stage`); that page is
[Dashboard/src/pages/outreach/CampaignDetail.tsx](../Dashboard/src/pages/outreach/CampaignDetail.tsx).

## Config

[config/outreach.yaml](config/outreach.yaml) mirrors the Research Agent's
`icp.yaml` — single source of truth for selection and sending behavior, no
code changes needed to adjust it:

- `research.api_base` — where to pull prospects from (env var
  `RESEARCH_API_BASE` overrides this).
- `research.qualified_tiers` — which Research tiers count as "qualified"
  (default `A`, `B`).
- `selection` — how `campaign create` picks the top-N.
  `require_contact_email` (default `true`) controls the **import** step —
  whether a tier-matching prospect with no contact email is cached at all.
  Left at the default, a contact-less Tier A/B company never reaches the
  `prospects` cache, so it can never be selected into a campaign either
  (`create_campaign_from_prospects` only ever sees what's cached, and filters
  on `has_contact` again independently at selection time regardless of this
  setting — see [WORKFLOW.md](WORKFLOW.md#2-create-campaign--ingestpy)). Set
  to `false` to import a no-contact prospect anyway, purely so it's visible
  on the Prospects page for manual follow-up — it will still never be
  selected into a campaign on its own. Either way, every company excluded
  for lacking a contact is reported back as `excluded_no_contact` from both
  `POST /import` and `POST /campaigns`, not silently dropped.
- `sending` — `from_email`/`from_name`/`reply_to`, and `dry_run` to test the
  pipeline without spending real send quota.
- `personalization` — LLM prompt/behavior for drafting emails.
- `stages.order` — the pipeline's stage sequence (defaults to
  pending → sent → waiting → replied → booked).

## Tests

```
pytest
```

56 tests, no network (a fake in-memory PostgREST fixture in
[tests/conftest.py](tests/conftest.py) stands in for Supabase):

- [test_ingest.py](tests/test_ingest.py) — importing prospects from
  Research's API response shape; that a tier-matching company with no
  contact is excluded from both import and campaign selection outright
  (never padded into a `limit` slot, never given a `needs_review` item); and
  the one remaining `pending` vs. `needs_review` gate at campaign creation
  (draft genuinely personalized), which only ever holds an already-contactable
  prospect for review, never excludes it.
- [test_personalize.py](tests/test_personalize.py) — the full tiered
  personalization strategy: `pain_summary`/`recommended_angle` (qualify
  output) → other cached research data (industry/description/open
  reqs/score explanation) via a plain LLM call → (redraft only) a live
  web-search-augmented LLM call across three independent backends
  (Gemini+`google_search` → Groq+Tavily → Anthropic+`web_search`) → static
  template — including that each tier is skipped once an earlier one
  already succeeds, and that `use_web_search` defaults to `False` so
  campaign creation never triggers a live search on its own.
- [test_groq_tavily_fallback.py](tests/test_groq_tavily_fallback.py) — the
  HTTP-level mechanics of the Groq+Tavily backend in isolation: Tavily
  search results feeding Groq's draft, and clean fallback (`None` or the
  explicit `BODY_UNAVAILABLE` sentinel) when either call fails or a
  required key is missing.
- [test_pipeline.py](tests/test_pipeline.py) — campaign creation, sending,
  and stage transitions.
- [test_db_roundtrip.py](tests/test_db_roundtrip.py) — every model field
  survives a write/read cycle against Supabase.
- [test_redraft.py](tests/test_redraft.py) — the redraft auto-promotion
  rule: a `needs_review` item only advances to `pending` when the new draft
  is genuinely personalized *and* the prospect still has a contact email —
  a redraft fixes personalization, never contact discovery, so it must
  never promote a contact-less item to send-ready. Also asserts that both
  redraft endpoints request `use_web_search=True`, since a deliberate
  redraft is exactly the case worth the cost of a live search.
- [test_delete_research_run.py](tests/test_delete_research_run.py) —
  `db.delete_by_research_run`'s cascade cleanup (prospects, campaigns,
  items, events) when the Research Agent deletes a run, including the
  idempotent no-op case.

## Known limitations

- **No automated reply-tracking agent yet.** This agent's job stops at Sent —
  Waiting/Replied/Booked are moved manually via `outreach item set-stage` or
  the pipeline board's item drawer, per the note in
  [config/outreach.yaml](config/outreach.yaml).
- Sending real email requires a working SMTP relay and, for Gmail
  specifically, an App Password rather than the account password. A message
  that sends successfully can still land in spam if the From domain's
  SPF/DKIM records don't authorize the relay you're sending through — see
  [channels/email.py](src/outreach/channels/email.py).
- **`POST /campaigns` has no server-side "one campaign per run" guard** — see
  the note near the top of this README. Today's callers (the Dashboard form,
  and the Research Agent's automatic hand-off) each only call it once per
  run in practice, so this has not been a live problem, but nothing in this
  codebase actually prevents a duplicate.
