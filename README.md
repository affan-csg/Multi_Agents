# CSG Agents

Three independent projects that together run CSG's prospecting → outreach
pipeline, plus one shared dashboard to operate them:

| Project | What it does | Port |
|---|---|---|
| [Research_Agent](Research_Agent/README.md) | Discovers US companies, scores/tiers them as staffing prospects, finds contacts, and (optionally) runs an LLM qualification pass (`csg qualify`). | `:8000` |
| [Outreach_Agent](Outreach_Agent/README.md) | Pulls qualified (Tier A/B) companies from Research over HTTP, builds campaigns, drafts personalized emails, and sends them. Also hosts the **Conversation Agent** (replies/follow-ups after send) and the **Feedback Learning / Optimization Agent** (learns from performance, edits, outcomes; feeds insights back into drafts). | `:8001` |
| [Dashboard](Dashboard/README.md) | The only UI for both agents — a React/Vite app that talks to both backends directly from the browser. No database or backend of its own. Includes a dedicated Learning & Optimization tab under Outreach Agent. | `:5173` |

```
                    ┌─────────────────┐        HTTP only        ┌──────────────────────────┐
   companies  ──►   │  Research Agent  │ ───────────────────►   │  Outreach Agent   :8001  │ ──► email
   (discover,       │  (own Supabase   │  qualified prospects   │  ┌────────────────────┐  │     (SMTP)
   score, qualify)  │   project)       │  + auto hand-off       │  │ Conversation Agent │  │ ◄── replies
                    │      :8000       │                        │  └────────────────────┘  │     (IMAP)
                    └─────────▲────────┘                        └─────────▲────────────────┘
                              │                                           │
                              └───────────────  Dashboard  ───────────────┘
                                  (browser-only, no DB, no backend)
```

Inside the Outreach Agent process, two more sibling layers sit alongside the
Conversation Agent — both read-only against outcomes, never in the request
path of import/draft/send itself:

```
+-------------------------------------------------------------+
|   Outreach Agent :8001                                      |
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

Each backend is its own process, each in its own terminal, from its own
directory (see `run.txt` for the copy-pasteable version):

```
# Terminal 1 -- Research Agent's backend, :8000
cd Research_Agent
python -m uvicorn csg.api:app --app-dir src --reload --port 8000

# Terminal 2 -- Outreach Agent's backend, :8001
cd Outreach_Agent
python -m uvicorn outreach.api:app --app-dir src --reload --port 8001

# Terminal 3 -- the shared Dashboard, :5173
cd Dashboard
npm run dev
```

`--port 8001` is not optional the way `:8000` is — the Dashboard's
`VITE_OUTREACH_API_BASE` expects Outreach there specifically. Use
`python -m uvicorn ...`, not the bare `uvicorn` command — on Windows, a
venv's `uvicorn.exe` launcher hardcodes an absolute path to `python.exe` at
creation time, so a venv folder moved/renamed after `pip install` breaks it
(`Fatal error in launcher: Unable to create process...`) even though the
venv itself is fine. Also, on Windows PowerShell (not pwsh/PowerShell 7),
`&&` is not a valid command separator — one command per line, or use `;`.

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

## How a company moves through the pipeline

1. **Discover** (Research Agent) — pulls companies from configured sources
   into a research run.
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
8. **Calendly** (Dashboard) — one page, capsule tabs at the top (Bookings |
   Event Types), no separate nav items. Bookings: every prospect currently
   in `booked`, across every campaign, in one list, with All/Upcoming/Past
   filters, search, sort, and per-booking cancel/mark-no-show/delete actions
   for real Calendly bookings. Event Types: the account's meeting types and
   availability (view-only) plus one-off scheduling link generation — see
   the [Calendly Integration](Outreach_Agent/README.md#calendly-integration)
   section.

## Tests

Both Python agents' suites run fully offline (a fake in-memory PostgREST
fixture stands in for Supabase — see each project's `tests/conftest.py`),
so no live database or API keys are needed to run them:

```
cd Research_Agent && pytest    # 295 passed
cd Outreach_Agent  && pytest   # 584 passed (outreach + conversation + learning + optimization + Calendly)
```

The Dashboard has no test suite; its guard rail is the TypeScript compiler:

```
cd Dashboard && npx tsc -b --noEmit
```

See [.github/workflows/ci.yml](.github/workflows/ci.yml) for how these run
in CI.

## Known limitations

- Reply checking is manual (a button or a CLI tick) — the Conversation Agent
  has no background poller yet, by design, until the flow has been exercised
  against real mail.
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
- The Dashboard has one pre-existing, unrelated TypeScript error in
  `src/pages/research/UsageTab.tsx` (a JSX element assigned where a `string`
  prop is expected) — tracked, not yet fixed; everything else type-checks
  clean.
