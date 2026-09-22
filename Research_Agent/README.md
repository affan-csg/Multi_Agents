# CSG Research/ICP Agent

Autonomously finds and ranks US tech companies as staffing prospects.

**You pick a geographic region. That is the only input.** The agent discovers
companies in that region on its own, researches each one (size, revenue,
funding), detects which applicant-tracking system (ATS) it uses, pulls its open
requisitions, and scores it against an Ideal Customer Profile embedded in
[config/icp.yaml](config/icp.yaml). It can then add an LLM-written pain summary
and recruiting contacts.

You never supply company names, domains, size bands, financial thresholds, or
priority rules — those live in the ICP config and apply to every run.

```
Region ─► Discovery ─► Research ─► ATS + jobs ─► Score ─► Tier A/B/C
                                                              │
                                          Tier A/B ─► Qualify ─► Contacts ─► Outreach Agent hand-off
```

See [WORKFLOW.md](WORKFLOW.md) for how the pipeline works end to end.

## Stack

- **Backend**: Python (FastAPI + Typer CLI), talking to Supabase (Postgres)
  over PostgREST via `httpx` — see [src/csg/db.py](src/csg/db.py).
- **Frontend**: none of its own — its web UI lives in the shared
  [Dashboard](../Dashboard) app alongside every other agent. See
  **Web app** below.
- **Data**: a single Supabase project. Schema lives in
  [schema.sql](schema.sql) — run it once in the Supabase SQL editor to
  provision a new project.

## Setup

1. **Python deps** (requires Python ≥3.11):
   ```
   pip install -e .
   ```
2. **Environment**: copy `.env.example` to `.env` and fill in:
   - `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — required for everything.
   - `GEMINI_API_KEY` — **required for `csg discover`** (company discovery and
     firmographic research). Also used by `csg qualify`.
     **Multi-key failover**: add `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`, etc.
     to automatically roll over to the next key when one's daily quota is
     exhausted — see [src/csg/discovery/gemini_keys.py](src/csg/discovery/gemini_keys.py).
     Note grounded search (what discovery uses to find real companies) has
     its own, much tighter free-tier daily quota than plain text generation —
     a key that answers a simple prompt fine can still be out of grounded-search
     quota.
   - `ANTHROPIC_API_KEY` — alternative LLM for `csg qualify`. Gemini is tried
     first if both are set.
   - `TAVILY_API_KEY` / `GROQ_API_KEY` — optional fallback discovery backend.
     If every `GEMINI_API_KEY*` is exhausted, `csg discover` automatically
     retries via Tavily (web search) + Groq (parses results into companies,
     no built-in search of its own) instead of failing the run — see
     [src/csg/discovery/groq_tavily.py](src/csg/discovery/groq_tavily.py).
     Both keys have free tiers; only needed once Gemini's discovery quota
     runs out.
   - `HUNTER_API_KEY` — only needed for `csg contacts` (contact discovery).
     Every saved contact is guaranteed to have a verified email (Hunter
     entries without one are discarded at parse time); LinkedIn is carried
     through when Hunter finds it but is never required. A company becomes
     `outreach_ready` once it has one email contact — see
     [models.Prospect.compute_outreach_flags](src/csg/models.py).
   - `OUTREACH_API_BASE` — optional. When set, every run started from the
     API/Dashboard automatically hands its Tier A/B companies off to the
     Outreach Agent after qualify + contacts finish (see **Outreach hand-off**
     below). Unset means this step is skipped entirely; nothing else about a
     run changes.
3. **Database**: paste [schema.sql](schema.sql) into the Supabase SQL editor.
   It is safe on both a fresh and an existing project — the `CREATE TABLE`
   statements are skipped when tables already exist, and the `ALTER TABLE`
   block at the bottom adds any columns a pre-existing database is missing.
   [migrations/001_autonomous_discovery.sql](migrations/001_autonomous_discovery.sql)
   contains just that upgrade, if you prefer to run it on its own.

## Running it

**CLI:**
```
csg discover                                # autonomous run against the ICP's active region
csg discover "East Coast"                   # ...or name a region explicitly
csg discover --auto-qualify-top 5           # ...and qualify + find contacts for whichever of the top 5 are Tier A/B
csg report --run <run_id>                   # ranked prospect table
csg show <domain>                           # full scoring breakdown for one company
csg qualify --run <run_id>                  # LLM pain summary + angle (costs tokens)
csg contacts --run <run_id>                 # HR contact discovery via Hunter.io (costs quota)
csg export --run <run_id>                   # re-export CSV/JSONL
```

**A qualification attempt is never silently dropped on failure.** Every
`csg qualify` domain (automatic or by hand) records its outcome —
`succeeded` or `failed`, with a reason — in the `qualification_attempts`
table, separate from the success-only `qualifications` table. A company
whose LLM call errored out shows as **qualification failed**, not
identical to "never attempted": the Company Detail page and the Outreach
Prospects page both show this state explicitly (with the failure reason and
attempt count), and `GET /runs/{run_id}/qualify/failed` +
`POST /runs/{run_id}/qualify/retry-failed` (surfaced in the Dashboard as a
banner on the Companies page) let you retry just the domains that actually
failed, without re-qualifying everything else in the run. See
[WORKFLOW.md](WORKFLOW.md#4-qualify--srccsgqualifypy-costs-tokens) for the
full mechanics.

**The CLI and the API/Dashboard behave differently on purpose here.** A plain
`csg discover` with no flag spends nothing beyond discovery + scoring —
qualify/contacts stay explicit, opt-in steps (`--auto-qualify-top N`, behind
a cost confirmation you can skip with `--yes`) or run by hand afterwards
(`csg qualify`, `csg contacts`). This is deliberate: a person running raw
commands should consciously choose to spend Hunter.io/LLM quota.

**`POST /discover` (what the Dashboard's "Start research" uses) is
automatic — no flag needed.** Every run there qualifies + finds contacts for
every Tier A/B company it scores, capped at `icp.yaml`'s
`outreach.auto_qualify_top_n` (default 25, ranked by score) so one big run
can't silently exhaust Hunter's 25-searches/month free tier in a single shot.
A Tier C/D company is still discovered, scored, and shown — just never
qualified or contact-searched. Passing an explicit `auto_qualify_top` on the
request overrides the configured cap with that number instead — including
`auto_qualify_top: 0` to skip qualify/contacts (and therefore the Outreach
hand-off, since it only fires after qualify/contacts actually ran) entirely
for that one run, which the Dashboard's "Top N" field does when set to 0.

If `OUTREACH_API_BASE` is set, the automatic path goes one step further: once
qualify + contacts finish, the same Tier A/B companies are handed off to the
Outreach Agent — imported as prospects and drafted into a campaign. See
**Outreach hand-off** below; this never sends anything, and never happens on
the CLI path.

**Web app** (two processes, each in its own terminal): this agent has no
frontend of its own — its pages (Runs, Companies, Company Detail, Saved,
Contacts, Analytics, Settings) live in the shared [Dashboard](../Dashboard)
app alongside every other agent.
```
# Terminal 1 -- this backend, http://127.0.0.1:8000 (run from this directory)
python -m uvicorn csg.api:app --app-dir src --reload --port 8000

# Terminal 2 -- the Dashboard frontend, http://127.0.0.1:5173
cd ../Dashboard
npm install
npm run dev
```

Use `python -m uvicorn ...` rather than the bare `uvicorn` command — on
Windows, a venv's `uvicorn.exe` launcher hardcodes an absolute path to
`python.exe` at creation time, so if the venv folder is ever moved or
renamed after `pip install`, the launcher breaks with `Fatal error in
launcher: Unable to create process...` even though the venv itself is fine.
`python -m uvicorn` sidesteps that by invoking uvicorn as a module through
the (correct, currently-running) `python.exe` you already have activated,
instead of through the stale launcher script.

Also note: on Windows PowerShell (not pwsh/PowerShell 7), `&&` is not a
valid command separator — run each command on its own line, or use `;`, not
`&&`, if you want to chain them.

Leave the backend running in its own terminal; the Dashboard proxies
nothing — it calls `http://127.0.0.1:8000` directly, so both must be up at
once (see Dashboard's `VITE_RESEARCH_API_BASE` in its `.env.example` if you
need to point it elsewhere). Port 8000 is not just a suggestion here — the
Dashboard's default expects Research on that exact port.

The Research runs page is a region picker and a Start button — the embedded ICP
does the rest. You can optionally name a run (e.g. "Georgia Q4") so it's easy to find later —
see below.

## Naming and scoping runs

Every run can be given a `label` when it's started (the "Name this research"
field in the UI, or the `label` field on `POST /discover`). Once named, the
frontend's Research selector (shown at the top of Companies, Saved lists,
Contacts, and Analytics) lets you switch between runs by name, or pick "All
research" to see data aggregated across every run.

Every run-scoped API route follows the same shape:
`/runs/{run_id}/...`, where `run_id` is either a real run's id or the literal
string `all`. This covers companies, saved lists, contacts, suppression,
analytics, and the qualify/contacts actions — see
[WORKFLOW.md](WORKFLOW.md#storage--supabase-postgres-via-postgrest) for how
`all` is resolved for tables (saved/contacts/suppression) that don't have
their own `run_id` column.

## Resuming a stalled run

If every provider (Gemini, and the Tavily+Groq fallback) is exhausted while a
run is in progress, some companies can be left scored with no ATS board and
no firmographics — a "stalled" company (see `db.stalled_companies`). The
Settings page's run list shows a **Resume research (N)** action on any run
that has these; it re-researches only the stalled domains (no re-discovery)
rather than requiring a whole new run. See
[WORKFLOW.md](WORKFLOW.md#resuming--discoverresume_stalled) for the mechanics.

## Outreach hand-off

Set `OUTREACH_API_BASE` (`.env`, e.g. `http://127.0.0.1:8001`) to
automatically connect a run's results to the [Outreach Agent](../Outreach_Agent).
When it's set, every run started via `POST /discover` (the Dashboard) does
this automatically once qualify + contacts finish for the run's Tier A/B
companies:

1. **Import** — the same Tier A/B domains are pulled into Outreach's local
   `prospects` cache (`POST /import` on Outreach's API, polled to
   completion) — but only the ones Hunter actually found a contact for. A
   Tier A/B company with no contact is excluded here, not carried through as
   a contact-less row, and is reported back as `excluded_no_contact`.
2. **Draft a campaign** — `POST /campaigns` on Outreach selects the
   contactable cached prospects (up to Outreach's configured limit) and
   drafts a personalized message for each one.

**This never sends anything.** A drafted item lands in Outreach's `pending`
(ready to send, a person still has to click Send) or `needs_review` (Manual
Review — genuinely personalized draft not yet possible). Every selected item
already has a contact by this point, so `needs_review` here means only one
thing: no real `pain_summary`/`recommended_angle` to personalize from (a
thin/failed qualification) — see
[Outreach_Agent/WORKFLOW.md](../Outreach_Agent/WORKFLOW.md#2-create-campaign--ingestpy)
for the exact gating. A company with **no contact at all** never gets an
item, in either stage — it's excluded from the hand-off outright, its
tier/score untouched in Research's own data, and reported via
`stats.outreach["excluded_no_contact"]` (e.g. "No contacts found for Acme
Co, so it was not added to prospects/campaign") rather than silently
missing.

Unset `OUTREACH_API_BASE` (the default) and nothing changes about how a
research run behaves — the hand-off is skipped, not attempted-and-failed.
A hand-off failure (Outreach unreachable, etc.) is recorded on the run's
result (`stats.outreach = {"error": ...}`) but never fails the research run
itself, which already succeeded before this step runs.

The hand-off fires any time qualify + contacts actually ran for Tier A/B
companies — the Dashboard's automatic path, **and** the CLI's
`csg discover --auto-qualify-top N` after its cost confirmation. Only the
*qualify/contacts* step differs in how it's triggered (automatic vs.
explicit opt-in) between the two; once it runs, the Outreach hand-off follows
the same way on either path.

## Config

[config/icp.yaml](config/icp.yaml) is the single source of truth for what the
agent looks for. Editing it changes discovery, scoring, and tiering together —
no code changes:

- `geo.search_regions` — the regions you can target, and which one is active
  by default. **The only thing chosen per run.**
- `size` — the headcount sweet band (a scoring signal, never a disqualifier).
- `financials` — the `$10M+` revenue target and how revenue/funding score.
- `practice_areas` — CSG's ten practice areas as keyword patterns, each
  weighted so AI/ML and MLOps count for more than generic software roles.
- `disqualifiers` — competitors and agencies that are hard-excluded.
- `weights` — the six scoring dimensions. **Must total 100** (enforced at load).
- `tiers` — the A/B/C score cutoffs.
- `outreach.auto_qualify_top_n` — the per-run cap on how many Tier A/B
  companies get automatically qualified + contacted (and, if configured,
  handed off to Outreach) — see **Outreach hand-off** above. Default 25.

Discovery search queries are generated from these values, so the ICP never
drifts out of sync with what the agent actually searches for.

## Tests

```
pytest
```

295 tests, no network and no LLM calls (HTTP boundaries are mocked with
`httpx.MockTransport` or a fake fetcher/DB, not real calls) — the
deterministic core and every provider integration is fully covered:

- [test_scoring.py](tests/test_scoring.py) — scoring, tiering, financial
  strength, and the missing-data policy, including that a company whose job
  discovery was fully exhausted (`hiring_signal="unknown"`) is never scored
  as a confirmed-empty ATS board (`hiring_signal="confirmed"`) on the Hiring
  Need dimension — see [src/csg/scoring.py](src/csg/scoring.py)'s module
  docstring and [src/csg/models.py](src/csg/models.py)'s `HiringSignal`.
- [test_qualify.py](tests/test_qualify.py) — the `csg qualify` LLM prompt
  context, including that `hiring_signal` is always included (defaulting to
  `"unknown"` if absent) so the qualification LLM never asserts a company
  isn't hiring based on a data gap rather than a confirmed empty board.
- [test_delivery.py](tests/test_delivery.py) — US/LATAM/Pakistan delivery-fit
  classification.
- [test_regions.py](tests/test_regions.py) — region resolution and
  ICP-generated discovery prompts.
- [test_db_roundtrip.py](tests/test_db_roundtrip.py) — every model field
  survives a write/read cycle (uses an in-memory fake PostgREST), plus a
  structural audit that fails the moment a new model field is left out of
  the persistence or export path.
- [test_export.py](tests/test_export.py) — the CSV and JSONL handoff
  contracts, including the `outreach_ready`/`has_linkedin_contact` flags and
  that `hiring_signal`/`job_discovery_method` survive into both the CSV and
  the `Prospect` JSONL handed to the Outreach Agent.
- [test_contacts.py](tests/test_contacts.py) — Hunter.io response parsing;
  locks in that every saved contact has a verified email.
- [test_gemini_keys.py](tests/test_gemini_keys.py) — multi-key failover:
  daily-quota detection against real captured error shapes, key rotation
  order, and the distinction between a per-minute limit (retry same key)
  and daily exhaustion (rotate to the next key). Also covers the IPv4 pinning
  and connection-error detection that routes around broken IPv6 paths to
  Google's servers on affected networks.
- [test_ats_detect.py](tests/test_ats_detect.py) — `ats.detect()`'s full
  three-pronged strategy: careers-page board links, following a landing page
  one hop to a listing subpage, the careers-subdomain fallback, and ordering
  between all three.
- [test_workday.py](tests/test_workday.py), [test_smartrecruiters.py](tests/test_smartrecruiters.py),
  [test_workable.py](tests/test_workable.py), [test_bamboohr.py](tests/test_bamboohr.py) —
  per-provider `verify()`/`fetch()` parsing, including each provider's
  specific false-positive/false-negative trap (e.g. SmartRecruiters'
  API returning 200 for any token, real or not — see
  [src/csg/ats/smartrecruiters.py](src/csg/ats/smartrecruiters.py)).
- [test_discover_handoff.py](tests/test_discover_handoff.py) — the automatic
  Tier A/B selection/cap logic and the Outreach hand-off's call sequence
  (import → poll → campaign), including that a hand-off failure never fails
  the research run.
- [test_outreach_client.py](tests/test_outreach_client.py) — `OutreachClient`
  against a mocked HTTP transport: job polling, timeouts, and error surfacing.
- [test_db_qualification_attempts.py](tests/test_db_qualification_attempts.py) —
  round-trip tests for the `qualification_attempts` ledger: attempt-count
  increments across repeated failures, a later success overwriting a stale
  failure's status, and run-scoped/status-filtered reads
  (`get_qualification_attempts`, `get_failed_qualifications`).
- [test_qualify_run.py](tests/test_qualify_run.py) — `qualify_run` end to
  end against a fake backend/DB: a success persists to both
  `qualifications` and `qualification_attempts`; a failure persists as
  `failed` instead of being dropped; partial failure across several
  companies leaves each one's own correct status; `retry_failed_run` only
  re-qualifies domains whose latest attempt failed (never a domain that's
  never been tried or already succeeded) and is a no-op that never touches
  the LLM backend when nothing has failed; and `_complete_with_retry`'s
  existing backoff recovers from a transient error, gives up after
  `MAX_RETRIES`, and never retries a permanent one.
- [test_api_qualify.py](tests/test_api_qualify.py) — the qualify HTTP
  surface: `POST /qualify`'s `domains` restriction (the fix for a single
  company's "Qualify with AI" button previously widening to the whole
  pending set), `GET /qualify/failed`, `POST /qualify/retry-failed`, and
  `company_detail`'s `qualification_attempt` field.

There is still no automated end-to-end test: a real run makes live HTTP calls
to career sites and the Gemini API. See [WORKFLOW.md](WORKFLOW.md) for what a
full run touches before wiring one up, and the **Known limitations** section
below for the current live-run status.

## Applying schema migrations

`CREATE TABLE IF NOT EXISTS` only affects a brand-new Supabase project — it
does not retroactively alter tables that already exist. Schema changes on an
already-provisioned project need `ALTER TABLE` statements, pasted into the
Supabase SQL editor by hand (the `SUPABASE_SERVICE_KEY` used everywhere else
is a PostgREST REST key, not a Postgres connection string, so nothing here can
run DDL for you).

`schema.sql` now carries its own `ALTER TABLE` block at the bottom, so running
the whole file is safe on both new and existing projects. Numbered migrations
live in [migrations/](migrations/):

- [001_autonomous_discovery.sql](migrations/001_autonomous_discovery.sql) —
  adds `companies.{revenue,funding,description,region}` and
  `job_postings.{delivery_fit,practice_area}`, and converts
  `companies.employee_count` from an integer to a JSONB provenance envelope.
  Without it, researched firmographics are silently discarded on write.
- [002_api_usage.sql](migrations/002_api_usage.sql) — adds the `api_usage`
  table the Settings > Usage & Quota tab reads from. Without it,
  `GET /settings/usage` just returns all-zero data instead of erroring.
- [003_job_discovery_fallback.sql](migrations/003_job_discovery_fallback.sql) —
  adds `companies.{job_discovery_method,job_source,hiring_signal,job_discovery_notes}`
  for the ATS → careers-page → web-search → unknown fallback chain (see
  **Job discovery fallback chain** in [WORKFLOW.md](WORKFLOW.md)).
- [004_qualification_attempts.sql](migrations/004_qualification_attempts.sql) —
  adds the `qualification_attempts` table (see **Running it** above). Needed
  on any existing project to get per-domain qualify failure tracking, the
  Company Detail page's failure state, and the retry-failed banner/route —
  without it those simply have nothing to read and behave as if nothing has
  ever failed.

Earlier migrations, kept in `scratchpad/` as a record:
- [migration_add_fk_cascades.sql](scratchpad/migration_add_fk_cascades.sql) —
  `ON DELETE CASCADE` on every table that references `companies.domain`, so
  deleting a company also deletes its postings/scores/qualifications/saved
  entry/contacts/suppression row in one step.
- [migration_add_run_label.sql](scratchpad/migration_add_run_label.sql) —
  adds the `runs.label` column used for naming a research run (see above).

If you add a column or constraint to `schema.sql` going forward, write the
matching `ALTER TABLE` migration alongside it and note here once it's been
run against the live project.

## Known limitations

- **Gemini's grounded-search quota is tight and shared across all
  `GEMINI_API_KEY*` keys** — the `google_search` tool discovery uses to find
  real companies has its own, much lower free-tier daily cap than plain text
  generation, separate from the qualify/enrichment quota. A key that answers
  a simple prompt fine can still be out of grounded-search quota. Multi-key
  failover (`GEMINI_API_KEY_2`, `_3`, ...) rotates through every configured
  key before giving up (see test_gemini_keys.py), and once every key is
  exhausted, both discovery **and** firmographic enrichment automatically
  fall back to Tavily (search) + Groq (parses results into the same
  structured shape a Gemini call would have produced) rather than failing —
  see [src/csg/discovery/groq_tavily.py](src/csg/discovery/groq_tavily.py)
  and [src/csg/discovery/research.py](src/csg/discovery/research.py). A
  company is only reported as a genuine enrichment failure if *both*
  providers fail (e.g. `TAVILY_API_KEY`/`GROQ_API_KEY` also unset) — surfaced
  in the run result as `enrich_failures` and a clear Dashboard toast, not a
  silent per-company skip.
- **ATS detection has real, structural coverage gaps**, not just missed
  edge cases: iCIMS, Taleo, and Jobvite have no reliable public,
  unauthenticated API to detect against (confirmed by direct research, not
  assumed); Phenom (now common among large enterprises, e.g. CVS Health,
  United Airlines) renders career sites entirely via client-side JS with no
  static HTML/API signal, which would need a headless-browser fetcher this
  project deliberately doesn't run. Eight providers *are* covered —
  Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, BambooHR,
  Rippling — via a three-pronged strategy (careers-page board links, following a
  landing page one hop to a listing subpage, a careers-subdomain fallback)
  before falling back to token-guessing. See
  [src/csg/ats/\_\_init\_\_.py](src/csg/ats/__init__.py)'s module docstring
  for the full picture, including which providers are deliberately excluded
  from token-probing because their API can't reliably say "not found."
  A company that falls into one of these gaps (or has a fully JS-rendered
  site with no static signal at all) is never scored, exported, qualified,
  or displayed as "confirmed zero openings" — every layer (scoring,
  CSV/JSONL export, `csg qualify`'s LLM context, the CLI, the Dashboard, and
  the Outreach hand-off) carries `hiring_signal` (`"confirmed"` |
  `"unknown"`) end-to-end specifically so this distinction survives instead
  of collapsing into an ambiguous zero. See
  [src/csg/jobs_discovery.py](src/csg/jobs_discovery.py)'s module docstring
  for the full ATS → careers-page → web-search → UNKNOWN fallback chain.
- **No automated end-to-end test.** A full run makes real HTTP calls to live
  career-site ATS boards and, for `qualify`/discovery/enrichment, real LLM
  API calls — see [WORKFLOW.md](WORKFLOW.md) before wiring one up. Playwright
  (browser automation) could not be installed in this environment (its
  browser binary download was blocked by the network); e2e verification so
  far has been done by driving the FastAPI backend directly and inspecting
  Supabase and the running frontend by hand, plus mocked integration tests
  (`httpx.MockTransport`, fake fetchers) for every provider- and
  service-boundary interaction — see **Tests** above.
- Every Supabase read is a real network round trip (roughly ~0.4s each,
  hosted PostgREST) — unavoidable without adding caching or a pooled
  connection, neither of which is in place. Endpoints that used to issue one
  such call *per row* (Companies' state/keyword filters, the Saved list) have
  been fixed to batch instead, but a page load is still at least a couple of
  sequential round trips.
