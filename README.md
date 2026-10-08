# CSG Agents

Three independent projects that together run CSG's prospecting → outreach
pipeline, plus one shared dashboard to operate them. Research and Outreach
are served through **one backend port** (see [Server](Server/README.md)
below); the Dashboard is still its own dev server.

| Project | What it does | Reached at |
|---|---|---|
| [Research_Agent](Research_Agent/README.md) | Discovers US companies, scores/tiers them as staffing prospects, never re-processes a company it has already seen (global de-duplication), finds contacts, and (optionally) runs an LLM qualification pass (`csg qualify`). | `:8000/research` |
| [Outreach_Agent](Outreach_Agent/README.md) | Pulls qualified (Tier A/B) companies from Research over HTTP, builds campaigns, drafts personalized emails, and sends them. Also hosts the **Conversation Agent** (replies/follow-ups after send), the **Feedback Learning / Optimization Agent** (learns from performance, edits, outcomes; feeds insights back into drafts), the **Calendly** integration, **automatic polling** (inbox + Calendly sync + follow-ups on a schedule), and the **Analytics Dashboard** (multi-account, multi-campaign, per-sender analytics with drill-down). | `:8000/outreach` |
| [Dashboard](Dashboard/README.md) | The only UI for both agents — a React/Vite app that talks to both backends directly from the browser behind a password screen. No database or backend of its own. Includes a dedicated Learning & Optimization tab under Outreach Agent. | `:5173` |

```
                    ┌────────────────────────────────────────────────────────────┐
                    │                  Server  :8000  (one process)              │
                    │                                                            │
   companies  ──►   │  ┌─────────────────┐   HTTP,   ┌──────────────────────┐   │  ──► email
   (discover,       │  │ /research        │  same     │ /outreach            │   │      (SMTP)
   score, qualify)  │  │ (own Supabase    │  process  │ ┌──────────────────┐ │   │
                    │  │  project)        │ ────────► │ │Conversation Agent│ │   │  ◄── replies
                    │  └────────▲─────────┘           │ └──────────────────┘ │   │      (IMAP)
                    │           │                      └──────────▲───────────┘   │
                    └───────────┼─────────────────────────────────┼───────────────┘
                                │                                  │
                                └────────────────  Dashboard  ─────┘
                                     (browser-only, no DB, no backend)
```

`/research` and `/outreach` are two separate FastAPI apps (separate code,
separate Supabase projects, separate business logic) mounted onto one parent
app by [Server/app.py](Server/app.py) — a composition layer with no business
logic of its own (it only adds dashboard password auth, CORS, the optional
background scheduler, and a root alias for the Calendly webhook). Each agent can still be run standalone on its own port
(`:8000` / `:8001`) exactly as before; see
[Server/README.md](Server/README.md#running-an-agent-standalone).

Inside the Outreach Agent process (`:8000/outreach`, or `:8001` standalone), two more sibling layers sit alongside the
Conversation Agent — both read-only against outcomes, never in the request
path of import/draft/send itself:

```
+-------------------------------------------------------------+
|   Outreach Agent                                            |
|                                                             |
|   outreach_items / conversation_messages  (read by both)    |
|               |                          |                  |
|               v                          v                  |
|       Learning Agent            Campaign-Level Optimization |
|   (per-edit diffs, human       (whole-campaign/strategy     |
|    vs. AI-original drafts)      comparisons, ICP segments)  |
|               |                          |                  |
|               +------------+-------------+                  |
|                            v                                |
|         structured guidance appended to the next            |
|         personalize.draft_message() / revise_message() call |
+-------------------------------------------------------------+
```

Both layers are fully additive — if either fails, drafting/sending/
Conversation Agent behavior is unaffected, and either can be deleted without
touching the other or the core pipeline.

**Research and Outreach own separate Supabase projects.** They never share a
database, a table, or a direct DB connection — the only link between them is
Outreach's [research_client.py](Outreach_Agent/src/outreach/research_client.py)
calling Research's own API, and a plain `research_domain` string used as the
join key. Deleting or redeploying either never touches the other.

**The Conversation Agent deliberately shares the Outreach Agent's project**,
unlike that split. It is not a third database or a third process: it operates
on the very same `outreach_items` rows a person watches on the kanban board,
so there is nothing to copy across a boundary, and a second database would
mean two sources of truth for one card's stage. The two agents are isolated
from each other by **stage ownership** instead — Outreach writes
`needs_review/pending → sent/failed`, Conversation writes
`sent → waiting → replied → booked → not_interested` (the last one shown as
"Failed" in the Dashboard, reached automatically from a clear rejection
reply), and neither queries the other's stages.
It remains a self-contained package
([src/outreach/conversation/](Outreach_Agent/src/outreach/conversation/)) that
can be switched off per campaign or removed entirely without touching the
Outreach Agent.

## Quickstart

Two terminals: one venv holding both agents' backends behind one port, plus
the Dashboard (see `run.txt` for the copy-pasteable version):

```
# Terminal 1 -- one venv, both agents' backends, :8000
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux: source .venv/bin/activate
pip install -e Research_Agent -e Outreach_Agent -e Server
python -m uvicorn app:app --app-dir Server --reload --reload-dir Research_Agent/src --reload-dir Outreach_Agent/src --reload-dir Server --port 8000

# Terminal 2 -- the shared Dashboard, :5173
cd Dashboard
npm run dev
```

`--port 8000` is not optional — the Dashboard's `VITE_API_URL` defaults to
`http://127.0.0.1:8000` in dev and appends `/research` and `/outreach`.
Set `DASHBOARD_PASSWORD` (in `Server/.env` or the shell) before starting: it
is the Dashboard login password, and with it unset every protected route
returns 503 (fail closed). Use `python -m uvicorn ...`, not the bare `uvicorn` command —
on Windows, a venv's `uvicorn.exe` launcher hardcodes an absolute path to
`python.exe` at creation time, so a venv folder moved/renamed after
`pip install` breaks it (`Fatal error in launcher: Unable to create
process...`) even though the venv itself is fine. Also, on Windows
PowerShell (not pwsh/PowerShell 7), `&&` is not a valid command separator —
one command per line, or use `;`.

See [Server/README.md](Server/README.md) for why this is one process, what
it does and does not change, and how to fall back to running an agent
standalone on its own port.

First-time setup (env vars, Supabase schema, dependencies) is documented per
project — see each one's own README:

- [Research_Agent/README.md](Research_Agent/README.md) — discovery sources,
  scoring/ICP config, contact discovery, `csg qualify`, the automatic
  Research→Outreach hand-off.
- [Outreach_Agent/README.md](Outreach_Agent/README.md) — campaign creation,
  the tiered email-personalization strategy (qualify data → cached research
  data → live web search → static template), sending, the pipeline board.
- [Dashboard/README.md](Dashboard/README.md) — adding a new agent's pages,
  the shared design system, the stale-while-revalidate data layer.
- [Server/README.md](Server/README.md) — the single-port composition layer:
  how it mounts both agents, keeps their databases separate, and how to run
  an agent standalone instead.

## How a company moves through the pipeline

1. **Discover** (Research Agent) — Gemini grounded search (falling back to
   Tavily + Groq when every Gemini key is out of quota) finds companies in the
   chosen region and researches their firmographics. Each candidate is checked
   against every company already stored (normalized domain, then normalized
   name). A match is only skipped (shown as "Already exists" and not
   re-inserted/re-scored/re-handed to Outreach) if it has a successful
   qualification in any run, or is suppressed/disqualified. Companies that were
   previously discovered but never qualified, scored but didn't make the
   top-N cut, or had only failed qualification attempts remain eligible and
   will be re-evaluated in the new run, competing in that run's own top-N pool.
2. **Score / tier** (Research Agent) — rules-based scoring assigns a tier
   (A/B/C/D). `csg qualify` is a separate, optional LLM pass that adds a
   `pain_summary`/`recommended_angle` on top of a tier — it can lag behind or
   fail independently of scoring, which is why Outreach never treats it as
   the only usable signal (see next point). Zero open reqs is scored,
   exported, and displayed as a real "no current hiring need" signal only
   when a job board/careers page was actually read and found empty
   (`hiring_signal="confirmed"`) — a company whose ATS/careers page couldn't
   be found at all (e.g. a fully JS-rendered site) is tracked as
   `hiring_signal="unknown"` instead, and is never scored, qualified, or
   displayed as a confirmed zero.
3. **Hand off** (automatic or manual) — Tier A/B companies with a contact are
   imported into Outreach, either by hand or automatically right after
   Research finishes qualifying + finding contacts for a run.
4. **Personalize** (Outreach Agent) — drafts an email per prospect using the
   richest data actually available, in order: `pain_summary`/
   `recommended_angle` (if `csg qualify` finished) → other cached research
   fields (industry/description/open reqs/score explanation) via a plain LLM
   call → (Redraft only) a live web-search-augmented LLM call, tried across
   three independent backends (Gemini+`google_search` → Groq+Tavily →
   Anthropic+`web_search`) → a static template as the last resort. Only that
   final static-template path sends an item to **Needs Review** — anything
   an LLM could genuinely personalize from goes straight to **Pending**.
5. **Send** (Outreach Agent + Dashboard) — a person reviews and sends from
   the pipeline board. This is where the Outreach Agent's job ends.
6. **Converse** (Conversation Agent) — once switched on for a campaign, it
   reads replies over IMAP, classifies them, and drives Sent → Waiting →
   Replied → Booked, drafting replies and follow-ups along the way. A `sent`
   item with no reply for 24 hours moves itself to **Waiting** automatically
   (checked on every tick, not a background timer). Every reply draft steers
   toward a booked call via a Calendly link, and a real booking (detected
   from the scheduling tool's own confirmation email, not just prospect
   prose) moves the card straight to **Booked**. A separate, optional
   [Calendly API/webhook integration](Outreach_Agent/README.md#calendly-integration)
   layers real calendar data onto the same stage — including catching
   cancellations, which the email-based detection above cannot. A reply that clearly
   declines ("not interested", "no thanks", "remove me", etc.) moves the card
   to the terminal **Failed** stage instead — no automatic follow-up is ever
   sent once a card lands there. It is **off by default per campaign**, and
   even when on it only *drafts* outgoing email unless `auto_reply` is also
   enabled. With it off, those stages are moved by hand exactly as before.
   Replies are checked by the Dashboard button / CLI tick, or automatically
   when `AUTO_SYNC_ENABLED=true` (see step 10).
   See
   [Outreach_Agent/README.md](Outreach_Agent/README.md#conversation-agent-agent-3).
7. **Learn** (Feedback Learning Agent + Campaign-Level Optimization) —
   two independent, fully additive sibling layers that observe everything and
   feed guidance back into future drafts, neither causal, neither able to
   block the pipeline if it fails. The Learning Agent snapshots AI-original
   vs. human-final drafts and computes confidence-scored patterns from
   single-edit diffs; Optimization extends the same evidence-gating
   philosophy to whole-campaign/strategy comparisons (ICP segment, email/
   subject/CTA style, length, personalization, follow-up count) and lets a
   person explicitly mark a campaign as having adopted a recommendation, then
   reports its observed before/after performance. See the Dashboard's
   **Learning & Optimization** tab for overview, performance comparisons,
   detected patterns, human-edit trends, campaign comparison, strategy
   insights, adoption tracking, and drill-down into the data behind each
   insight.
8. **Analyze** (Analytics Dashboard) — real-time email and campaign performance
   by sending account, campaign, and individual prospect. Overview tab shows
   key metrics (prospects, sends, reply rate, booking rate), pipeline funnel,
   personalization sources, tier mix, and sends over time. Email Accounts tab
   breaks down per-sender metrics and trends. Campaigns tab compares campaigns
   with response/booking rates. Bookings tab shows booked prospects across
   campaigns. Filters allow drilling by account, campaign, and date range.
   Prospect drill-down shows full conversation thread and follow-up history.
9. **Calendly** (Dashboard) — one page, capsule tabs at the top (Bookings |
   Event Types), no separate nav items. Bookings: every prospect currently
   in `booked`, across every campaign, in one list, with All/Upcoming/Past
   filters, search, sort, and per-booking cancel/mark-no-show/delete actions
   for real Calendly bookings. Event Types: the account's meeting types and
   availability (view-only) plus one-off scheduling link generation — see
   the [Calendly Integration](Outreach_Agent/README.md#calendly-integration)
   section.

10. **Automate** (opt-in) — with `AUTO_SYNC_ENABLED=true` the Server runs a
    background loop (`outreach.automation`) that, every
    `AUTO_SYNC_INTERVAL_MINUTES` (default 30; afterwards the Dashboard /
    `PATCH /outreach/automation/settings` wins), syncs Calendly, reads the
    inbox once, ticks every campaign whose Conversation Agent is on, and
    sends/drafts follow-ups. Timers and a lease live in the `automation_state`
    table, so restarts and multiple replicas neither duplicate nor skip work.
    A follow-up is only attempted when both the Calendly sync and the inbox
    read succeeded. Off by default.

## Tests

Both Python agents' suites run fully offline (a fake in-memory PostgREST
fixture stands in for Supabase),
so no live database or API keys are needed to run them (Research's fake lives
in `tests/test_db_roundtrip.py`; Outreach's in `tests/conftest.py`):

```
cd Research_Agent && pytest    # 323 tests (incl. global de-duplication)
cd Outreach_Agent  && pytest   # 724 tests (outreach + conversation + learning + optimization + Calendly + automation + analytics)
cd Server          && pytest   # single-port mounting, lifespans, WebSockets, CORS, auth, DB isolation
                               # (needs all three packages installed: pip install -e Research_Agent -e Outreach_Agent -e Server)
```

The Dashboard has no test suite; its guard rail is the TypeScript compiler:

```
cd Dashboard && npx tsc -b --noEmit
```

See [.github/workflows/ci.yml](.github/workflows/ci.yml) for how these run
in CI.

## Known limitations

- Automatic polling (`AUTO_SYNC_ENABLED`) is off by default and runs inside
  the Server process; a standalone Outreach process has no scheduler, so reply
  checking there stays manual (Dashboard button / `outreach conversation tick`
  / `outreach automation run-once`).
- Dashboard authentication is a single shared password (`DASHBOARD_PASSWORD`)
  enforced by the Server; there are no per-user accounts, and standalone
  agent processes have no auth at all.
- `POST /campaigns` has no server-side "one campaign per research run"
  guard; today's only two callers (the Dashboard form, and Research's
  automatic hand-off) each only call it once per run in practice, but
  nothing in the code itself prevents a duplicate. See
  [Outreach_Agent/README.md](Outreach_Agent/README.md#known-limitations).
- Learning and Optimization insight recomputation are synchronous per-request;
  fine at current volumes but could move to frontend caching or event-driven
  if `draft_versions`/`outreach_items` grow very large. Confidence thresholds
  (20/50/150 samples; 1.5pp effect size) are shared between the two layers
  (Optimization imports, not re-declares, the Learning Agent's constants) and
  are tunable constants, not yet exposed as config. `learning_era_comparison`
  endpoint exists but is not yet surfaced on the Dashboard.
- Optimization's ICP segmentation is fixed to `prospects.tier` and
  `prospects.industry` — adding a new segment axis means editing
  `optimization/engine.py`'s `SEGMENT_DIMENSIONS` explicitly.
- Optimization's adoption-outcome comparisons are observed associations, not
  controlled experiments — campaigns are chosen by whoever creates them, not
  randomly assigned, so other differences between an adopting campaign and
  the comparison set (timing, ICP mix, sender) are not accounted for. Every
  such API response includes an explicit `causality_note` saying so.
- CI's Dashboard typecheck step is still marked `continue-on-error` for a
  historical `UsageTab.tsx` error; `npx tsc -b --noEmit` currently passes
  cleanly, so that flag can be dropped.
- Research's de-duplication (migration 005 required) distinguishes between
  genuinely qualified companies (which are never re-scored) and
  re-eligible companies (discovered, scored, or attempted-but-failed in an
  earlier run). See
  [Research_Agent/README.md](Research_Agent/README.md#global-company-de-duplication).
