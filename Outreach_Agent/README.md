# CSG Outreach Agent

Personalizes and sends outreach to prospects qualified by the Research Agent,
and — via the **Conversation Agent** bundled in this project — handles what
happens after the send.

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

## Conversation Agent (Agent 3)

Owns the pipeline after Sent: `sent → waiting → replied → booked`. It lives in
[src/outreach/conversation/](src/outreach/conversation/) and runs inside this
same backend process, against this same Supabase project.

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

### Bookings page

Every item in `booked`, across every campaign, also shows up on the
Dashboard's **Bookings** page — company, contact, campaign, when it was
booked, and whether the Conversation Agent or a person moved the card
(`GET /bookings`, [api.py](src/outreach/api.py)). It reads the same
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
  once the prospect is interested.
- `stages.order` — the pipeline's stage sequence (defaults to
  pending → sent → waiting → replied → booked).
- `conversation` — the Conversation Agent's defaults for a campaign that has
  never been configured (both switches off). The live switch is per campaign
  and lives in the database, not here. `followup_after_days` (default `1`)
  is exact elapsed hours since `sent_at`, not a calendar-day boundary — `1`
  means precisely 24 hours of silence before a `sent` item moves to Waiting.

## Tests

```
pytest
```

254 tests, no network (a fake in-memory PostgREST fixture in
[tests/conftest.py](tests/conftest.py) stands in for Supabase):

- [test_ingest.py](tests/test_ingest.py) — importing prospects from
  Research's API response shape; that a tier-matching company with no
  contact is excluded from both import and campaign selection outright
  (never padded into a `limit` slot, never given a `needs_review` item); the
  one remaining `pending` vs. `needs_review` gate at campaign creation
  (draft genuinely personalized), which only ever holds an already-contactable
  prospect for review, never excludes it; and that `hiring_signal` survives
  the Research → Outreach handoff onto `ImportedProspect` (`"confirmed"` |
  `"unknown"`, see the Research Agent's `models.HiringSignal`).
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

Conversation Agent (155 of the 254), all driven through two injectable seams —
a supplied batch of `InboundEmail` instead of IMAP, and a supplied clock —
so the full reply path runs with no mailbox, no LLM, and no waiting:

- [test_conversation_engine.py](tests/test_conversation_engine.py) — the
  funnel `sent → waiting → replied → booked`; that autoresponders and bounces
  never move a card; forward-only transitions; that items in
  `pending`/`needs_review`/`failed` are never even loaded; and an end-to-end
  regression (real classifier + real matcher, nothing mocked) for a Calendly
  "New Event" notification moving a card straight to `booked`.
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

## Known limitations

- **Reply checking is manual.** The Conversation Agent runs on a button click
  or a CLI tick — there is no background poller yet, by design, until the
  flow has been exercised against real mail.
- **Email only.** The Conversation Agent reads and replies over IMAP/SMTP.
  LinkedIn and other channels are not handled.
- **Meeting detection has no real calendar integration.** A `booked`
  classification comes from either the reply's wording (a prospect proposing
  a time in prose) or recognizing a scheduling tool's own confirmation
  email (Calendly, Google Calendar) by sender/subject shape — not from
  actually querying Calendly's API for the booking. There is also no
  per-booking event URL captured from that notification email, so the
  Dashboard's Bookings page links every row to the same account-wide
  Calendly scheduling page rather than the specific event.
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
