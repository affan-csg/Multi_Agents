# CSG Outreach Agent

Personalizes and sends outreach to prospects qualified by the Research Agent,
and — via the **Conversation Agent** bundled in this project — handles what
happens after the send. Also includes two independent, fully additive
sibling layers that observe outcomes and feed guidance back into future
drafts: the **Feedback Learning Agent** (learns from individual draft edits)
and **Campaign-Level Optimization** (compares whole campaigns and strategies
against each other). Neither modifies or routes through the other, and
either can be removed without affecting anything else in this agent. A third
sibling, the **[Calendly Integration](#calendly-integration)**, layers a real
Calendly API sync + webhook receiver onto the Dashboard's Calendly page
(Bookings tab) — optional, and the page behaves exactly as before without it
configured.

**It never touches the Research Agent's database.** It pulls qualified
companies over HTTP from Research's own API, caches them locally, selects the
top-N into a campaign, drafts a personalized email per prospect (LLM, using
Research's own `pain_summary`/`recommended_angle`), and sends it.

Everything after Sent — Waiting, Replied, Booked — belongs to the
[Conversation Agent](#conversation-agent-agent-3), which is **off by default
per campaign**. Until you switch it on, those stages are moved by a person
exactly as they always were.

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
      after Research qualifies a run)                                  │
                                    Needs Review ─► Pending ─► Sent ─► Waiting ─► Replied ─► Booked
                                   └────── Outreach Agent ──────┘ └─ Conversation Agent ─┘
                                                                    (when switched on; a
                                                                     person otherwise)
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
   - `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` / `GROQ_API_KEY` — optional, for
     personalizing the drafted email from a prospect's
     `pain_summary`/`recommended_angle`, or (if that's not available yet)
     from other cached research fields directly. Three independent tiers,
     tried in order: Gemini (rotating through `GEMINI_API_KEY_2`,
     `GEMINI_API_KEY_3`, ... on a daily-quota 429 — see
     [src/outreach/gemini_keys.py](src/outreach/gemini_keys.py)), then
     Anthropic, then Groq (plain text, no search) — same convention as the
     Research Agent's `csg qualify`, extended with the Groq tier so a single
     provider's quota/outage/billing issue can't stall every draft. Without
     any of the three, a plain templated email is sent instead (see
     [src/outreach/personalize.py](src/outreach/personalize.py)).
   - `GROQ_API_KEY` / `TAVILY_API_KEY` together — additionally used by the
     Redraft button's "go find more information" tier (live web search), as
     a backend independent of Gemini/Anthropic: Tavily does the actual
     search, Groq drafts from its results. Tried between Gemini+`google_search` and
     Anthropic+`web_search`; skipped (not an error) if either key is unset.
3. **Database**: paste [schema.sql](schema.sql) into **this** Outreach
   Supabase project's SQL editor (a fresh project, separate from Research's).
   On an existing project, re-running the whole file is safe
   (`CREATE TABLE IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`), or apply just the
   individual migration files in `migrations/` (e.g.
   [005_learning_agent.sql](migrations/005_learning_agent.sql) for the new
   Learning Agent tables).

## Qualification status — reflecting Research's qualify outcome

A cached prospect's `qualification_failed` flag (`models.ImportedProspect`)
mirrors Research's own `qualification_attempt` for that domain/run:
`true` only when the *latest* qualify attempt over there failed and nothing
has succeeded since (`ingest._to_imported_prospect`). It's refreshed on
every re-import — there is no live join back to Research, so if a company
was retried and fixed on the Research side, re-importing (the Prospects
page's "Import qualified prospects" button) is what picks that up here.

The Prospects page shows this as a distinct "failed" badge (with the
failure reason in a tooltip) in the Qualified column, separate from both
"yes" (has a `pain_summary`) and "—" (never attempted) — see
[Dashboard/src/pages/outreach/Prospects.tsx](../Dashboard/src/pages/outreach/Prospects.tsx).

## Running it

**CLI:**
```
outreach import-prospects --run <run_id>   # pull qualified prospects from Research
outreach campaign create --limit 25        # select top-N cached prospects into a campaign
outreach campaign send <campaign_id>       # draft + send email for pending items
outreach campaign pipeline <campaign_id>   # the 7-stage pipeline board, as a table
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

## Conversation Agent (Agent 3)

Owns the pipeline after Sent: `sent → waiting → replied → booked`, plus the
terminal `not_interested` stage (shown as "Failed" in the Dashboard) reached
from any of those three when a reply is classified as a clear rejection. It
lives in [src/outreach/conversation/](src/outreach/conversation/) and runs
inside this same backend process, against this same Supabase project.

**It is off by default, per campaign.** Deploying it changes nothing until
you switch it on for a specific campaign — every existing campaign keeps
behaving exactly as before.

### Two switches, not one

| Switch | Off (default) | On |
|---|---|---|
| `enabled` | The agent does nothing at all: no inbox reads, no card moves, no sends. | Detects replies, classifies them, moves cards, drafts replies, handles follow-ups. |
| `auto_reply` | Drafts every outgoing email and waits for you to approve it. | Sends them itself. |

They are separate because "watch my inbox and keep the board current" and
"talk to my prospects unattended" are different levels of trust. **Switching
the agent on never, by itself, emails anybody** — you have to turn on
`auto_reply` as well.

### Setup

Add IMAP credentials to `.env` (see [.env.example](.env.example)) pointing at
the same mailbox your SMTP settings send *from* — that is where replies land.
For Gmail: `imap.gmail.com`, port 993, and the same **App Password** you use
for SMTP.

Then apply the schema. A fresh Supabase project gets everything from
[schema.sql](schema.sql); an existing one needs
[migrations/001_conversation_agent.sql](migrations/001_conversation_agent.sql)
pasted into the SQL editor. It is additive and re-runnable — two new tables
and three new columns, no existing table altered.

### Using it

From the Dashboard: open a campaign and flip **Conversation Agent** in the
header, then click **Check replies**. `auto_reply` is in the kebab menu.

From the CLI:

```
outreach conversation status <campaign_id>     # is it on? how is it configured?
outreach conversation enable <campaign_id>     # on, drafts only (safe default)
outreach conversation enable <campaign_id> --auto-reply   # on, and may send
outreach conversation disable <campaign_id>    # off; nothing is deleted
outreach conversation tick <campaign_id>       # run one cycle now
outreach conversation tick                     # ...for every enabled campaign
```

Reply checking is **manual today** — the button or the CLI. There is no
background poller yet; that comes once the flow has been exercised on real
mail.

### What a tick actually does

1. Refuses immediately unless the agent is enabled for that campaign.
2. Loads only conversation-stage items — an item in `pending` or
   `needs_review` is never even queried, so the Outreach Agent's work is
   structurally out of reach.
3. Fetches recent mail and matches each message to an item, trying four
   strategies in order and stopping at the first hit:
   threading headers (In-Reply-To/References against the item's own
   `provider_message_id`) → the same headers against any prior *outbound*
   message on the item (so a reply to an earlier follow-up still matches) →
   sender address against the item's `to_email` → **`Reply-To`** against the
   item's `to_email`. That last one exists specifically for scheduling-tool
   notifications: a Calendly "New Event" email is From
   `notifications@calendly.com` and To the account owner — neither names the
   prospect — but Calendly puts the actual invitee's address in `Reply-To`.
4. Classifies each genuinely new message. A scheduling-tool confirmation
   (sender domain or subject shape, e.g. `notifications@calendly.com` /
   `calendar.google.com`, "New Event:", "You're scheduled") is recognized
   deterministically as `booked` before any LLM call runs — no risk of an
   LLM misreading an automated notification as a bounce or ignoring it.
   Advances the card accordingly.
5. Moves items silent for 24 hours to Waiting (exact elapsed time since
   `sent_at`, not a calendar-day boundary — see `followup_after_days` below)
   and nudges Waiting items within the follow-up caps.

### Safety properties

- **Idempotent.** Dedupe is by `UNIQUE (item_id, provider_message_id)`, so
  re-reading the same reply is normal and free — the agent deliberately does
  not trust IMAP's `\Seen` flags, which other mail clients can clobber. Tick
  as often as you like; the same email is stored once, classified once, and
  moves a card once.
- **Forward-only.** Transitions are whitelisted in
  `models.CONVERSATION_TRANSITIONS`, so a bad classification cannot move a
  card backwards, skip a stage, or resurrect a booked prospect.
- **Off means off.** `enabled` is re-read immediately before *every* send, not
  once per tick — flipping it off mid-run stops the next send.
- **No lost history.** Turning the agent off writes one boolean. Every
  message, stage and counter survives, and re-enabling resumes from recorded
  state rather than replaying it.
- **No double-processing.** An advisory lock (`conversation_locked_at`) means
  two overlapping ticks never both act on one item; a lock stranded by a
  crashed tick is reclaimable after 10 minutes.
- **Autoresponders and bounces are not replies.** Both are detected
  deterministically, before any LLM call, and neither moves a card nor marks
  the prospect as having engaged — so someone who was merely on holiday still
  gets their follow-up.
- **Rejections get silence.** A `not_interested` reply moves the card to
  Replied but is never answered automatically.
- **Manual control always works.** Dragging cards and the drawer's stage
  buttons stay live whether the agent is on or off, and a person's move
  always overrides the agent's. Every transition is attributed in
  `outreach_events` — `conversation_agent`, `system`, or `user`.
- **Replies actually thread.** Every reply and follow-up the agent sends
  carries real `In-Reply-To`/`References` headers against the message it's
  answering (or, for a follow-up with nothing to answer yet, against the
  original outreach send) — it lands as a reply in the prospect's mail
  client, not a new, disconnected message. See
  [channels/email.py](src/outreach/channels/email.py)'s `in_reply_to`
  parameter.

### Drafting toward a booked call

Every `interested` reply is drafted to end with either the Calendly link
(`CALENDLY_URL` in `.env`, or `personalization.calendly_url` in
`config/outreach.yaml`) or a concrete time-ask — never an open-ended "let me
know!" — because the agent's job doesn't end at a reply, it ends at a booked
call. A `booked` reply confirms warmly without renegotiating anything the
prospect already proposed. Left unconfigured, the reply asks for a few times
that work instead of linking anywhere. See
[prompts/conversation_reply.md](prompts/conversation_reply.md).

### Bookings tab

Every item in `booked`, across every campaign, also shows up on the
Dashboard's **Calendly** page (Bookings tab) — company, contact, campaign,
when it was booked, and whether the Conversation Agent or a person moved the
card (`GET /bookings`, [api.py](src/outreach/api.py)). It reads the same
`outreach_events` audit trail the item drawer does, so there's no separate
booking record to fall out of sync.

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
  `personalization.calendly_url` (or the `CALENDLY_URL` env var, which wins
  if both are set) is the scheduling link a Conversation Agent reply offers
  once the prospect is interested. Separate from, and independent of,
  `CALENDLY_API_TOKEN`/`CALENDLY_WEBHOOK_SIGNING_KEY` (env-only, no YAML
  equivalent) — those enable the real API/webhook integration described in
  [Calendly Integration](#calendly-integration); this is just a link in
  outbound text.
- `stages.order` — the pipeline's stage sequence (defaults to
  needs_review → pending → sent → waiting → replied → booked → not_interested;
  `failed`, the Outreach Agent's own send-error stage, is separate -- see
  `owned_by_this_agent`).
- `conversation` — the Conversation Agent's defaults for a campaign that has
  never been configured (both switches off). The live switch is per campaign
  and lives in the database, not here. `followup_after_days` (default `1`)
  is exact elapsed hours since `sent_at`, not a calendar-day boundary — `1`
  means precisely 24 hours of silence before a `sent` item moves to Waiting.

## Tests

```
pytest
```

524 tests, no network (a fake in-memory PostgREST fixture in
[tests/conftest.py](tests/conftest.py) stands in for Supabase; the Calendly
suite additionally never calls `api.calendly.com` — `httpx.Client.get/post`
is monkeypatched per test, same convention as the Gemini/Anthropic/Tavily
tests) — 114 Outreach + 155 Conversation + 105 Learning + 38 Campaign-Level
Optimization + 60 Calendly Integration tests, roughly:

- [test_ingest.py](tests/test_ingest.py) — importing prospects from
  Research's API response shape; that a tier-matching company with no
  contact is excluded from both import and campaign selection outright
  (never padded into a `limit` slot, never given a `needs_review` item); the
  one remaining `pending` vs. `needs_review` gate at campaign creation
  (draft genuinely personalized), which only ever holds an already-contactable
  prospect for review, never excludes it; that `hiring_signal` survives
  the Research → Outreach handoff onto `ImportedProspect` (`"confirmed"` |
  `"unknown"`, see the Research Agent's `models.HiringSignal`); and that
  `qualification_failed` is set from Research's `qualification_attempt`
  field only when the latest attempt actually failed with no success since —
  never for a domain that hasn't been qualified yet, and never once a retry
  succeeds (see **Qualification status** below).
- [test_personalize.py](tests/test_personalize.py) — the full tiered
  personalization strategy: `pain_summary`/`recommended_angle` (qualify
  output) → other cached research data (industry/description/open
  reqs/score explanation) via a plain LLM call → (redraft only) a live
  web-search-augmented LLM call across three independent backends
  (Gemini+`google_search` → Groq+Tavily → Anthropic+`web_search`) → static
  template — including that each tier is skipped once an earlier one
  already succeeds, that `use_web_search` defaults to `False` so campaign
  creation never triggers a live search on its own, and that the
  `{{open_reqs}}` prompt placeholder never reads an empty req list as
  "confirmed not hiring" when `hiring_signal == "unknown"` (see
  `personalize._render_open_reqs`).
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

Conversation Agent (197 of the 464), all driven through two injectable seams —
a supplied batch of `InboundEmail` instead of IMAP, and a supplied clock —
so the full reply path runs with no mailbox, no LLM, and no waiting:

- [test_conversation_engine.py](tests/test_conversation_engine.py) — the
  funnel `sent → waiting → replied → booked`, and the rejection funnel
  `sent → waiting → not_interested` ("Failed"); that autoresponders and
  bounces never move a card; forward-only transitions, including that
  `not_interested` is terminal for the agent's own moves but always
  reopenable by a person; that a Failed item never gets an automated
  follow-up; that the original message/classification/audit trail survive a
  move to Failed; that items in `pending`/`needs_review`/`failed` are never
  even loaded; and an end-to-end regression (real classifier + real matcher,
  nothing mocked) for a Calendly "New Event" notification moving a card
  straight to `booked`.
- [test_conversation_toggle.py](tests/test_conversation_toggle.py) — OFF is a
  complete no-op (asserted as no stage write, no stored message, no send —
  not just a zeroed counter); manual moves still work while off; toggling off
  preserves all data; re-enabling does not replay history; `auto_reply` off
  drafts without sending; and flipping the switch mid-tick stops the send.
- [test_conversation_idempotency.py](tests/test_conversation_idempotency.py) —
  the same reply across five ticks yields one message, one classification,
  one card move and one send; locking is exclusive and stale locks are
  reclaimable; a lock is released even when processing raises; and the two
  agents running on one campaign never touch each other's cards.
- [test_conversation_followup.py](tests/test_conversation_followup.py) —
  follow-up timing (including the exact 24-hour boundary a brand-new
  campaign's default resolves to), the `max_followups` cap under repeated
  ticks, spacing between consecutive nudges, that a reply stops further
  follow-ups, and that a follow-up threads against the original send.
- [test_conversation_classifier.py](tests/test_conversation_classifier.py) —
  deterministic bounce/auto-reply/booking-confirmation detection (never
  reaching the LLM for any of the three), intent parsing, degradation to
  `unclear` when no LLM is available, quoted-text stripping, all four
  matching strategies (including `Reply-To`-only matches and that it's only
  tried after sender-address matching fails), and raw RFC 822 parsing
  (including the `Reply-To` header).
- [test_conversation_api.py](tests/test_conversation_api.py) — that the
  routes are actually mounted on the Outreach Agent's app (every other test
  calls the engine directly, so only this catches a wiring mistake), that a
  partial settings update preserves the other fields, that a tick on a
  disabled campaign is a 200 no-op rather than an error, that a draft can be
  approved and sent while the agent is off, drafting/revising/redrafting a
  reply on demand, and that the Outreach Agent's own routes still behave
  exactly as before.
- [test_conversation_quoting.py](tests/test_conversation_quoting.py) —
  stripping quoted history from a reply for both the classifier's LLM input
  and the API's display output, including real Gmail CRLF line endings.
- [test_conversation_responder.py](tests/test_conversation_responder.py) —
  composing and revising reply/follow-up drafts, and that a stored draft's
  own `in_reply_to` is forwarded to the actual send call when approved.
- [test_email_channel.py](tests/test_email_channel.py) — that `send()` only
  sets threading headers when `in_reply_to` is given, and sets them
  correctly when it is.
- [test_llm_fallback.py](tests/test_llm_fallback.py) — the three-tier
  fallback chain (Gemini → Anthropic → Groq), Gemini's multi-key rotation
  (advancing only on a *daily* quota 429, not a per-minute rate limit), and
  that a non-quota error is not swallowed by the key pool.
- [test_bookings.py](tests/test_bookings.py) — `GET /bookings`: pooling
  across campaigns, excluding every non-`booked` stage, stable sort order
  (including a same-microsecond tie on Windows), and the Calendly link
  included on each row.

### Feedback Learning Agent (105 of the 412)

Data-driven pattern detection without ML or causal claims, fed back into future drafts.

- [test_learning_diffing.py](tests/test_learning_diffing.py) — pure, rule-based
  diffs (subject/opening/CTA-before-signature/length/sentences/personalization/
  formatting), no LLM, no DB.
- [test_learning_db.py](tests/test_learning_db.py) — round-trip insert/list/get
  for the 3 new tables (draft_versions, learning_insights, learning_events).
- [test_learning_hooks.py](tests/test_learning_hooks.py) — snapshot writes at
  each `/revise`, `/redraft`, `/redraft-generic`, and conversation-reply hook
  point; idempotency (two revises → two distinct rows).
- [test_learning_failure_isolation.py](tests/test_learning_failure_isolation.py) —
  the critical spec requirement: learning layer failures never block drafting,
  sending, or conversation behavior.
- [test_learning_engine.py](tests/test_learning_engine.py) — confidence scoring
  (two gates: sample size + effect size; never "active" below 20 emails with 1.5pp
  difference); insight recomputation; state-transition history.
- [test_learning_performance.py](tests/test_learning_performance.py) — before/after
  windowing, campaign comparison, follow-up buckets; explicit "Not enough data yet"
  rather than fabricated metrics.
- [test_learning_routes.py](tests/test_learning_routes.py) — all 9 `/learning/*`
  endpoints.
- [test_learning_malformed_data.py](tests/test_learning_malformed_data.py) —
  None/empty text, missing outcome joins, empty tables.
- [test_learning_fixtures.py](tests/test_learning_fixtures.py) — deterministic
  synthetic dataset (120+ emails with exact 50% vs 5% reply rate gap; n=6 samples
  must not create active insight; 30-edit fixture with exact 73% shorten-CTA
  pattern).

### Campaign-Level Optimization (38 of the 412)

Independent sibling layer to the Feedback Learning Agent above — compares whole
campaigns/strategies against each other rather than diffing single edits.

- [test_optimization_strategy.py](tests/test_optimization_strategy.py) —
  rule-based strategy classification (length bucket, subject/opening/CTA
  style, personalization level, email style) from stored subject/body text,
  no LLM, no DB.
- [test_optimization_engine.py](tests/test_optimization_engine.py) —
  dimension-comparison insight computation; confidence gating shared with the
  Learning Agent's exact thresholds; global vs. segmented (ICP tier/industry)
  computation staying independent of each other; metric fallback
  (booking → positive-reply → reply rate) when booked events are too sparse
  to support a booking-rate claim.
- [test_optimization_tracking.py](tests/test_optimization_tracking.py) —
  recommendation-adoption outcome reporting: insufficient-data states when no
  adoption is recorded or either side of the comparison is too small, and
  that a real comparison always carries the `causality_note` disclaimer.
- [test_optimization_routes.py](tests/test_optimization_routes.py) — all
  `/optimization/*` endpoints, including that the one write endpoint
  (`POST /optimization/adoptions`) 404s against a nonexistent campaign.
- [test_optimization_failure_isolation.py](tests/test_optimization_failure_isolation.py) —
  the same critical spec requirement as the Learning Agent's own isolation
  test: optimization layer failures never block drafting, sending, or any
  other existing behavior.
- [test_optimization_fixtures.py](tests/test_optimization_fixtures.py) —
  deterministic synthetic dataset exercising the segmentation and
  confidence-gating logic end to end.

### Calendly Integration

- [test_calendly_client.py](tests/test_calendly_client.py) — the httpx
  wrapper's request shaping (filters, pagination token passthrough, UUID
  extraction from a full resource URI), cancellation request shaping
  (`POST /scheduled_events/{uuid}/cancellation`, with and without a reason),
  and error handling for an unset token, an unreachable API, and a 4xx/5xx
  status.
- [test_calendly_webhook_verify.py](tests/test_calendly_webhook_verify.py) —
  HMAC-SHA256 signature verification: a valid signature passes; a missing
  header, missing signing key, wrong key, tampered body, stale timestamp
  (replay), and malformed header/timestamp are all rejected; plus payload
  normalization for `invitee.created`/`invitee.canceled`.
- [test_calendly_sync.py](tests/test_calendly_sync.py) — prospect matching by
  invitee email (case-insensitive, most-recent-send tiebreak across
  campaigns, no match still recorded not discarded); idempotent
  create/update on repeated `apply_event` calls for the same `event_uri` (no
  duplicate rows, no redundant stage-change event on an already-`booked`
  item); cancellation reverting a `booked` item to `waiting` (and being a
  no-op on a non-booked item); `run_sync`'s full pagination loop across
  multiple pages; graceful (not exception-raising) reporting when
  `CALENDLY_API_TOKEN` is unset; webhook ingestion for both handled and
  unrecognized event types, and duplicate-delivery idempotency;
  `cancel_booking` — cancels in Calendly before touching the local row,
  reverts a matched item's stage, raises for an unknown/already-canceled
  event or a missing token, and leaves the local row untouched if the
  Calendly API call itself fails.
- [test_calendly_routes.py](tests/test_calendly_routes.py) — `POST
  /calendly/webhook` end to end against the real signature header (missing,
  bad, and valid signatures), matched and unmatched bookings, cancellation
  handling, and ignoring an unhandled event type without erroring; `POST
  /calendly/sync` and `GET /calendly/status`; `POST /calendly/events/cancel`
  end to end (cancels and reverts stage, 404 for an unknown event, 502 on a
  Calendly API failure).
- [test_bookings.py](tests/test_bookings.py) (extended) — a matched
  `calendly_events` row enriching a booking entry with real join
  link/meeting time; an unmatched Calendly booking still appearing on the
  page; and, critically, that a booking with **no** `calendly_events` row at
  all (the pre-existing classifier-only path) is completely unchanged —
  falls back to the shared account-wide `calendly_url` exactly as before.

## Feedback Learning Agent

The Learning Agent observes everything: captures AI-original vs. human-edited
drafts, tracks performance (reply rates, positive replies, bookings), and learns
explainable patterns (never causal claims). Insights feed back into future drafts
as structured guidance. Fully additive — if it fails, the pipeline works normally.

### How it works

1. **Observe drafts** — every `/revise`, `/redraft`, `/redraft-generic`, and
   conversation-reply operation snapshots before/after subject+body, computes
   observable diffs (subject changed, opening changed, CTA shortened, length
   delta, personalization added/removed, formatting changed), and stores them.
2. **Track outcomes** — reads existing `outreach_items` and `conversation_messages`
   to determine reply/positive-reply/booking outcomes per draft.
3. **Learn patterns** — runs a fixed catalog of detectors on-demand (no background
   job): "shorter CTAs → higher reply rate," "more personalization → more
   bookings," "human editors shorten CTAs 72% of the time," etc.
4. **Gate with confidence** — patterns only become "active" when sample size ≥20
   AND group-size-adjusted effect size ≥1.5pp. Five emails never create a
   high-confidence rule.
5. **Feed insights to new drafts** — when generating a new draft, retrieves active
   insights and injects them as structured prompt context (never hard-coded
   sentences, never auto-apply anything).

### Setup

No additional setup beyond the main Outreach schema. New Learning Agent tables are
in [schema.sql](schema.sql); the migration [005_learning_agent.sql](migrations/005_learning_agent.sql)
is idempotent. Once applied, the agent observes drafts automatically and exposes
9 read-only `/learning/*` endpoints. The Dashboard's **Learning & Optimization**
tab (Outreach section) displays overview, performance metrics, detected patterns,
human-edit trends, campaign comparison, and clickable drill-down.

### Using it

No CLI commands — the agent works silently in the background. Access insights via:
- Dashboard: **Outreach Agent → Learning & Optimization** tab (requires at least
  one draft to have been created/edited).
- API: `GET /learning/overview`, `GET /learning/insights`, `GET /learning/performance/*`,
  `GET /learning/human-edits`, etc. (see [src/outreach/learning/routes.py](src/outreach/learning/routes.py)).

## Campaign-Level Optimization

An independent sibling layer to the Feedback Learning Agent above — same
evidence-gated, no-causal-claims philosophy, extended from single-edit diffs up
to whole-campaign and strategy-level comparisons. It runs inside this same
backend process and Supabase project, reusing the Learning Agent's
`draft_versions` for style evidence, but it does **not** modify, subclass, or
route through anything in [src/outreach/learning/](src/outreach/learning/) —
each layer can be removed independently without affecting the other. See
[src/outreach/optimization/\_\_init\_\_.py](src/outreach/optimization/__init__.py)
for the full module map and invariants.

### How it works

1. **Extract strategy** — every sent item's stored subject/body is classified,
   on-demand and rule-based (no LLM), into `length_bucket`, `subject_style`,
   `opening_style`, `cta_style`, `personalization_level`, `email_style`. This
   covers every sent item, not just ones a human later edited — the one signal
   source the Learning Agent's edit-diffing alone can't produce.
2. **Compare dimensions** — splits sent items into two comparable groups per
   dimension (e.g. direct vs. soft CTA, short vs. long) and compares reply,
   positive-reply, booking, and no-reply rates, picking whichever metric the
   data can actually support as the headline number.
3. **Segment by ICP** — every dimension is compared both globally and within
   every ICP segment (`prospects.tier`, `prospects.industry`) that
   independently has enough evidence — a segment's pattern is never inferred
   from the global number, or vice versa.
4. **Gate with confidence** — reuses the Learning Agent's exact thresholds
   (imported, not re-declared): sample size ≥20, effect size ≥1.5pp. A
   pattern with too little evidence stays `insufficient_data`, never a
   fabricated `low`.
5. **Feed guidance to new drafts** — active insights are appended to campaign/
   draft prompts as a short bullet-list blurb, independently of (never merged
   with) the Learning Agent's own blurb.
6. **Track adoption** — a person can explicitly mark a campaign as "built
   following pattern X" (`POST /optimization/adoptions`); the outcome endpoint
   then reports that campaign's observed rates next to all other campaigns',
   always labeled an observed association, never a proven cause.

### Setup

No additional setup beyond the main Outreach schema. New tables are in
[schema.sql](schema.sql); the migration
[006_campaign_optimization.sql](migrations/006_campaign_optimization.sql) is
idempotent and purely additive — both new tables only reference `campaigns`,
never the reverse, so they can be dropped cleanly if this layer is ever
removed. Once applied, the layer observes automatically and exposes read-only
`/optimization/*` endpoints (plus the one opt-in adoption-recording write).
The Dashboard's Learning & Optimization tab's **Strategy Insights** and
**Adoption Tracking** sections display it.

### Using it

No CLI commands. Access via:
- Dashboard: **Outreach Agent → Learning & Optimization → Strategy Insights /
  Adoption Tracking**.
- API: `GET /optimization/overview`, `/insights`, `/insights/{pattern_key}`,
  `/recommendations`, `/adoptions`, `/adoptions/{pattern_key}/outcome`, and
  `POST /optimization/adoptions` (see
  [src/outreach/optimization/routes.py](src/outreach/optimization/routes.py)).

## Calendly Integration

A real Calendly API integration for the Dashboard's Calendly page (Bookings
tab), layered on top of (not replacing) the pre-existing classifier-only
booking detection — see
[src/outreach/calendly/\_\_init\_\_.py](src/outreach/calendly/__init__.py)
for the full module map. Optional: with no `CALENDLY_API_TOKEN` configured,
the Bookings tab behaves exactly as it always did.

### How it works

1. **Webhook receiver** — `POST /calendly/webhook` accepts Calendly's
   `invitee.created`/`invitee.canceled` deliveries. Every request is verified
   against the `Calendly-Webhook-Signature` header (HMAC-SHA256 over
   `{timestamp}.{raw body}`, constant-time compared, with a 5-minute
   staleness tolerance against replay) before the body is even parsed — an
   unverified request is rejected with 401 and never written anywhere. See
   [calendly/webhook.py](src/outreach/calendly/webhook.py).
2. **API sync (backfill)** — `POST /calendly/sync` (or `outreach calendly
   sync`) paginates `GET /scheduled_events` for the whole account over a
   configurable lookback window (default 90 days), fetching each event's
   invitees, for bookings made before a webhook subscription existed or made
   through any channel a webhook delivery might have missed. See
   [calendly/sync.py](src/outreach/calendly/sync.py).
3. **Prospect matching** — both paths match a booking to a pipeline item by
   invitee email against `outreach_items.to_email` (the address outreach was
   actually sent to — a stronger signal than the cached prospect's
   `contact_email`, which can drift). The same-person-in-multiple-campaigns
   tiebreak matches `conversation/matcher.py`'s own address strategies: most
   recently sent wins. **A booking with no matching item still appears** on
   the Bookings tab (`matched: false`, no item/campaign), never discarded.
4. **Stage sync** — a matched, active booking moves the item to `booked`
   (any live conversation stage → booked); a matched cancellation reverts a
   `booked` item to `waiting` (the meeting fell through, but the
   conversation is still live) — something the classifier-only path could
   never do, since it has no way to learn a booking was later canceled. Both
   go through the same `db.set_stage` audit-trail path every other stage
   move uses, with a new actor, `"calendly"`, so the drawer/Bookings tab can
   always tell a Calendly-confirmed move apart from a classified-reply move
   or a person's manual override. See `calendly/sync.py`'s `_sync_item_stage`.
5. **Idempotency** — both the webhook and the sync path funnel through the
   same `apply_event()`, upserting one `calendly_events` row keyed on
   Calendly's own `event_uri`. A sync re-run or a webhook retry/re-delivery
   updates that one row (e.g. `active` → `canceled`) rather than duplicating
   it or re-advancing an already-`booked` item a second time.
6. **Canceling a booking** — the Bookings tab's "Cancel booking" action,
   `POST /calendly/events/cancel` (`{event_uri, reason?}`), cancels the event
   in Calendly itself (`POST /scheduled_events/{uuid}/cancellation`) — not a
   local row delete, which would desync from Calendly and silently
   reappear on the next sync/webhook — then applies the same
   status-flip/stage-revert path a real `invitee.canceled` webhook would
   (see `calendly/sync.py`'s `cancel_booking`). If the Calendly API call
   itself fails, the local row is left untouched so a failed cancel never
   lies about the booking's real status; 404 if the event isn't known
   locally or is already canceled, 502 on a Calendly API failure.
7. **Deleting a booking** — the Bookings tab's "Delete" action (available on
   any booking, any status), `POST /calendly/events/delete`
   (`{event_uri}`), permanently removes the local `calendly_events` row so
   it stops appearing anywhere in the dashboard. If the event is still
   active/upcoming on Calendly, it's canceled there first (same call as
   above); a past or already-canceled event has nothing left to cancel, so
   that step is skipped. Unlike cancel, this never leaves a row behind, and
   it never touches the matched pipeline item (only unlinked, never
   deleted) — see `calendly/sync.py`'s `delete_booking`.

### Setup

1. Create a Calendly Personal Access Token (Integrations → API & Webhooks)
   with at least `scheduled_events:read`, and set `CALENDLY_API_TOKEN` in
   `.env` — see [.env.example](.env.example). Add `scheduled_events:write`
   too if you want the Bookings tab's per-row cancel/delete actions to
   work — without it, `POST /calendly/events/cancel` (or `/delete` when the
   event is still active) fails with a 502 ("Insufficient scope") that names
   the exact scope to add; sync/viewing is unaffected either way.
2. Run [migrations/008_calendly_integration.sql](migrations/008_calendly_integration.sql)
   against an existing project (a fresh project's `schema.sql` already has
   it) — purely additive, one new table (`calendly_events`), safe to re-run.
3. Optional, for live updates instead of only manual/scheduled syncs: run
   `outreach calendly register-webhook https://your-public-host/calendly/webhook`
   (needs the token's `webhooks:write` scope). **Save the printed
   `signing_key` immediately** as `CALENDLY_WEBHOOK_SIGNING_KEY` in `.env` —
   Calendly returns it exactly once and never shows it again.
4. Without step 3, `POST /calendly/sync` (or the Bookings tab's "Sync
   Calendly" button, or `outreach calendly sync`) is still a complete,
   correct way to keep the page current — just not real-time.

### Using it

- Dashboard: **Outreach Agent → Calendly → Bookings tab → Sync Calendly**
  button; matched and unmatched bookings both appear in the same table. The
  **Event Types** tab on the same page shows the account's meeting types and
  availability (view-only) and lets you generate one-off scheduling links.
- CLI: `outreach calendly register-webhook <url>`, `outreach calendly sync
  [--lookback-days N]`.
- API: `POST /calendly/webhook` (Calendly calls this, not the Dashboard),
  `POST /calendly/sync`, `GET /calendly/status` (see
  [src/outreach/calendly/routes.py](src/outreach/calendly/routes.py)).
  `GET /bookings` is extended (not replaced) to merge in any matched
  `calendly_events` row's real join link/meeting time, and to list unmatched
  Calendly bookings alongside pipeline-matched ones.

## Known limitations

- **Reply checking is manual.** The Conversation Agent runs on a button click
  or a CLI tick — there is no background poller yet, by design, until the
  flow has been exercised against real mail.
- **Email only.** The Conversation Agent reads and replies over IMAP/SMTP.
  LinkedIn and other channels are not handled.
- **Meeting detection has two independent paths, not fully unified.** A
  `booked` classification still comes from either the reply's wording (a
  prospect proposing a time in prose) or recognizing a scheduling tool's own
  confirmation email by sender/subject shape, exactly as before — see
  [conversation/classifier.py](src/outreach/conversation/classifier.py). On
  top of that, a real Calendly integration
  ([src/outreach/calendly/](src/outreach/calendly/)) now exists: a
  `POST /calendly/webhook` receiver (signature-verified) and a
  `POST /calendly/sync` backfill pull against Calendly's own API, both
  matching bookings to pipeline items by invitee email and moving/reverting
  the `booked` stage from real calendar fact (including catching
  cancellations, which the email-heuristic path cannot). The two paths do
  not know about each other — a booking either classifier-detected or
  Calendly-confirmed both just land on `booked`, and the Bookings tab shows
  whichever data is available (the real per-booking join link/time when a
  `calendly_events` row is matched, the shared account-wide scheduling link
  otherwise). Setup (API token, optional webhook registration via
  `outreach calendly register-webhook`) is required for the new path; without
  it the page behaves exactly as before.
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
- **Learning and Optimization insight recomputation are synchronous
  per-request.** Acceptable at current volumes but could move to frontend
  caching or event-driven if `draft_versions`/`outreach_items` grow very
  large. Confidence thresholds and the minimum-evidence gate are tunable
  constants in [src/outreach/learning/engine.py](src/outreach/learning/engine.py)
  (Optimization imports the same ones — see
  [src/outreach/optimization/engine.py](src/outreach/optimization/engine.py) —
  rather than declaring a separate, looser bar), not yet exposed as config.
- **Optimization segmentation is fixed to two ICP dimensions.** Only
  `prospects.tier` and `prospects.industry` are enumerable segments today
  ([src/outreach/optimization/engine.py](src/outreach/optimization/engine.py)'s
  `SEGMENT_DIMENSIONS`) — adding a new segment axis means adding it there
  explicitly, not something the data auto-discovers.
- **Adoption-outcome comparisons are not controlled experiments.** Campaigns
  are chosen by whoever creates them, not randomly assigned to a strategy, so
  every reported before/after or adopting-vs-other comparison is an observed
  association only — other differences between campaigns (timing, ICP mix,
  sender, follow-up cadence) are not accounted for. Every such response
  includes an explicit `causality_note` saying so.
