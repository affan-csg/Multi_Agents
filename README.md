# CSG Agents

Three independent projects that together run CSG's prospecting → outreach
pipeline, plus one shared dashboard to operate them:

| Project | What it does | Port |
|---|---|---|
| [Research_Agent](Research_Agent/README.md) | Discovers US companies, scores/tiers them as staffing prospects, finds contacts, and (optionally) runs an LLM qualification pass (`csg qualify`). | `:8000` |
| [Outreach_Agent](Outreach_Agent/README.md) | Pulls qualified (Tier A/B) companies from Research over HTTP, builds campaigns, drafts personalized emails, and sends them. | `:8001` |
| [Dashboard](Dashboard/README.md) | The only UI for both agents — a React/Vite app that talks to both backends directly from the browser. No database or backend of its own. | `:5173` |

```
                    ┌─────────────────┐        HTTP only        ┌──────────────────┐
   companies  ──►   │  Research Agent  │ ───────────────────►   │  Outreach Agent   │  ──► email
   (discover,       │  (own Supabase   │  qualified prospects   │  (own Supabase    │
   score, qualify)  │   project)       │  + auto hand-off       │   project)        │
                    └─────────▲────────┘                        └─────────▲─────────┘
                              │                                           │
                              └───────────────  Dashboard  ───────────────┘
                                  (browser-only, no DB, no backend)
```

**Each agent owns a separate Supabase project.** They never share a
database, a table, or a direct DB connection — the only link between them is
Outreach's [research_client.py](Outreach_Agent/src/outreach/research_client.py)
calling Research's own API, and a plain `research_domain` string used as the
join key. Deleting or redeploying any one of the three projects never
touches the other two.

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
   the only usable signal (see next point).
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
5. **Send / track** (Outreach Agent + Dashboard) — a person reviews and
   sends from the pipeline board; Sent → Waiting → Replied → Booked are
   moved by hand, there is no automated reply-tracking yet.

## Tests

Both Python agents' suites run fully offline (a fake in-memory PostgREST
fixture stands in for Supabase — see each project's `tests/conftest.py`),
so no live database or API keys are needed to run them:

```
cd Research_Agent && pytest    # 220 passed
cd Outreach_Agent  && pytest   # 56 passed
```

The Dashboard has no test suite; its guard rail is the TypeScript compiler:

```
cd Dashboard && npx tsc -b --noEmit
```

See [.github/workflows/ci.yml](.github/workflows/ci.yml) for how these run
in CI.

## Known limitations

- No automated reply-tracking — Outreach's job stops at Sent.
- `POST /campaigns` has no server-side "one campaign per research run"
  guard; today's only two callers (the Dashboard form, and Research's
  automatic hand-off) each only call it once per run in practice, but
  nothing in the code itself prevents a duplicate. See
  [Outreach_Agent/README.md](Outreach_Agent/README.md#known-limitations).
- The Dashboard has one pre-existing, unrelated TypeScript error in
  `src/pages/research/UsageTab.tsx` (a JSX element assigned where a `string`
  prop is expected) — tracked, not yet fixed; everything else type-checks
  clean.
