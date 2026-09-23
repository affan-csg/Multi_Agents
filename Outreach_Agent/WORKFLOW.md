# How the agent works

A Research Agent run goes in; sent, personalized emails come out. Import and
campaign creation are **two separate function calls** —
`import_prospects()` never implicitly creates a campaign, and
`create_campaign_from_prospects()` only ever reads the local cache, never
re-hits the Research API — but **the two calls themselves can be triggered
either by hand or automatically**:

- **By hand**: `outreach import-prospects` / `campaign create` (CLI), or the
  Dashboard's Prospects/Campaigns pages — a person decides when to pull fresh
  data and when to build a campaign from it.
- **Automatically**: the Research Agent calls this agent's own
  `POST /import` then `POST /campaigns` right after it qualifies + finds
  contacts for a run's Tier A/B companies — see
  [Research_Agent/WORKFLOW.md's "4. Outreach hand-off"](../Research_Agent/WORKFLOW.md#4-outreach-hand-off).
  It is calling the *exact same routes and code* a person would; there is no
  separate "automatic" import/campaign-creation logic in this codebase to
  keep in sync.

Either way, sending is never automatic — a campaign always stops at a
drafted, reviewable state (`pending`/`needs_review`); a person (or `campaign
send`/the Send button) still has to trigger step 4.

```
 RESEARCH AGENT (separate service, :8000)
        |
        v  (by hand: outreach import-prospects / Prospects page
        |   automatic: Research's _handoff_to_outreach, after qualify+contacts)
 [1] IMPORT ──> prospects table (cached snapshot, keyed by research_domain)
        |
        v  (by hand: outreach campaign create / Campaigns page
        |   automatic: same _handoff_to_outreach call, right after [1])
 [2] CREATE CAMPAIGN (top-N by Research's own score/tier)
        |
        v
 [3] DRAFT (once, upfront) ──> pending | needs_review
                                            |
                                   [manual: approve / redraft]
                                            |
                                            v
 [4] SEND (email, pending only, ALWAYS a manual trigger) ──> sent | failed
                                            |
                    [Conversation Agent, when enabled -- else manual]
                                            v
                          waiting -> replied -> booked
                                            |
                              [clear rejection reply]
                                            v
                                    not_interested ("Failed" in the UI)
```

Everything this agent decides on its own lives in
[config/outreach.yaml](config/outreach.yaml) — selection limit/tiers,
sending rate limit, dry-run, personalization prompts, sender identity. Two
Supabase projects exist side by side and are never joined at the DB level:
Research Agent's and this agent's own (see [schema.sql](schema.sql)). The
only connection between the two agents is HTTP:
[research_client.py](src/outreach/research_client.py) (this agent pulling
from Research, used by import) and, in the other direction,
[Research_Agent/src/csg/outreach_client.py](../Research_Agent/src/csg/outreach_client.py)
(Research pushing the automatic hand-off into this agent's own `/import` and
`/campaigns` routes) — no shared database either direction, no fallback if
either agent's API is down.

## Quick summary

```
A Research run happens → Import + campaign (by hand, or automatic) → review drafts → send
```

1. **Import** — pull qualified Tier A/B prospects (tier + score + contact)
   from a Research Agent run into this agent's own `prospects` cache. A
   company with no contact email is excluded here, not carried through as a
   contact-less row — Research's `/companies/{domain}` response reports
   `outreach_ready=false` for it, and that's checked before caching (see
   `require_contact_email`/`qualified_prospects` in step 1 below). By hand
   (`outreach import-prospects`/Prospects page), or automatically right after
   Research finds contacts for a run's Tier A/B companies. Either way, every
   excluded company is reported back as `excluded_no_contact`, e.g. "No
   contacts found for Acme Co, so it was not added to prospects."
2. **Create campaign** — select the top N *contactable* cached prospects
   (ranked by Research's score — see step 1 below), draft every message up
   front, and file each item into `pending` (genuinely personalized) or
   `needs_review` (generic template only — held for review, never dropped
   from the campaign). A cached prospect can only reach this step already
   having a contact (import already filtered on that), so nothing here gets
   excluded for lacking one — but `limit` is still a ceiling on however many
   contactable prospects exist for this run/tier, never padded past that with
   anything else. Same by-hand-or-automatic split as import.
3. **Review** — a person reads `needs_review` items in the Dashboard kanban,
   and either approves the generic/no-contact draft as-is, asks for a
   redraft, or edits it with a free-text revise instruction.
4. **Send** — **always a manual trigger**, regardless of how the campaign was
   created. `campaign send` emails every `pending` item, rate-limited,
   skipping anything already sent so a retried run is safe.
5. **Track** — `waiting → replied → booked`, and the terminal `not_interested`
   stage ("Failed" in the Dashboard) reached from a clear rejection reply, are
   set by the Conversation Agent when it's switched on for a campaign (see
   [6. Conversation Agent](#6-conversation-agent--conversation) below), or by
   a person otherwise; this agent (steps 1–4) never sets them itself.

The sections below go through each stage in full detail.

## 1. Import — [ingest.py](src/outreach/ingest.py), [research_client.py](src/outreach/research_client.py)

`outreach import-prospects <run_id>` (or `POST /import` — called either by
hand or by the Research Agent's automatic hand-off, see above) is step one,
and it creates no campaign — it only populates the local `prospects` cache.

1. **Fetch.** [ResearchClient.qualified_prospects()](src/outreach/research_client.py)
   hits the Research Agent's own API (`GET /runs/{run_id}/companies`, then
   `GET /runs/{run_id}/companies/{domain}` per company) — the *same* routes
   the Research frontend uses. It filters to the wanted tiers, sorts by
   Research's own `score` descending, and **over-fetches** (`limit * 2`
   candidate rows when `require_contact_email` is set) before pulling
   per-company detail.
2. **Contact gate.** `require_contact_email` (config, default `true`) controls
   *this* step — whether a tier-matching company without a verified email is
   skipped from import entirely, or cached anyway. It defaults to `true`
   deliberately: a company Hunter never found a contact for can't be emailed,
   so by default it never reaches the `prospects` cache (or a campaign,
   since campaign creation only ever selects from what's cached) — it's
   reported back as `excluded_no_contact` (see step 1 in
   [Create campaign](#2-create-campaign--ingestpy) below) instead of silently
   never appearing. Set it to `false` if you specifically want a no-contact
   Tier A/B company cached anyway, visible on the Prospects page for manual
   follow-up — it will still never be selected into a campaign on its own
   (campaign creation filters on `has_contact` independently of this flag).
3. **Cache.** Each result is upserted into `prospects`, keyed by
   `research_domain` (`db.upsert_prospect`, `on_conflict="research_domain"`),
   so a re-import of the same run is idempotent and cheap.

If the Research API is unreachable, import fails loudly (`ResearchClientError`)
— there is no local fallback read. This agent must never guess at Research's
data.

**Qualification failure status is carried through, not just qualify's
success output.** `GET /runs/{run_id}/companies/{domain}` on Research now
also returns `qualification_attempt` (see
[Research_Agent/WORKFLOW.md](../Research_Agent/WORKFLOW.md#4-qualify--srccsgqualifypy-costs-tokens)) —
`_to_imported_prospect` sets `ImportedProspect.qualification_failed = true`
when that attempt's status is `"failed"` and there is no successful
`qualification` for the same domain/run. A domain that was simply never
qualified yet (`qualification_attempt is None`) is left `false` — "failed"
and "not attempted" must stay visually distinct on the Prospects page, not
both collapse to the same blank cell.

### Picking the one contact to email

A company can have several Hunter.io contacts on record; only one is ever
emailed. [`_best_contact()`](src/outreach/ingest.py) ranks them by
`(verification_rank, seniority_rank, confidence)` —
`valid` > `accept_all` on verification, `executive` > `senior` on seniority —
and takes the top one. This ranking *is* the send decision: whichever contact
wins here is the only one that will ever receive outreach for that company.

## 2. Create campaign — [ingest.py](src/outreach/ingest.py)

`outreach campaign create` (or `POST /campaigns` — called either by hand or
by the Research Agent's automatic hand-off) is step two, and only ever reads
from the local `prospects` cache — it never calls the Research API.

One research run backs at most one real campaign — the Dashboard's
campaign-creation form disables/blocks re-selecting a run that already has a
campaign, checked client-side against the loaded campaign list (no new
backend constraint). `research_run_id` is left `null` on the Test Campaign
below, which is what exempts it from this rule.

### Test Campaign — `create_test_campaign()`, fixed QA fixture

`POST /campaigns/test` is a separate creation path that bypasses both the
prospects cache and the Research API entirely: it upserts two hardcoded
synthetic prospects (`test-pending.csg-internal.invalid` /
`test-review.csg-internal.invalid`, industry `"Internal QA"`) and creates
their `outreach_items` directly in fixed stages —
`affanjan007@gmail.com` → `pending`, `affanjan008@gmail.com` →
`needs_review` — instead of going through the select/draft flow above. Its
subject/body still call the real `personalize._sender_signature()` helper
(not a hand-written sign-off) so a test send matches a real one exactly,
website link included. Re-running it creates a fresh `Test Campaign` each
time rather than upserting into an existing one — it's meant to be disposable
QA scaffolding, deleted via the normal `DELETE /campaigns/{id}` when done.

1. **Select — contact required, up front, not just at stage time.** Filter
   cached prospects to `tier in tiers` (default `A,B`) **and** `has_contact`
   — a company Hunter never found a contact for is dropped from selection
   entirely, before ranking, before `limit` is even applied. The surviving
   contactable pool is then sorted by the *cached* score descending
   (Research's ranking is reused verbatim; this agent never re-scores) and
   the top `limit` (default 25) are selected. `limit` is a **ceiling on the
   contactable pool**, never a target to fill regardless of contactability —
   if only 2 of 3 tier-matching companies have a contact, the campaign gets
   exactly those 2, even though `limit` allows more; the third is reported in
   `excluded_no_contact` (`{"domain", "name", "tier"}` per company), not
   silently missing. This is a hard behavior change from an earlier version
   of this agent, which let a contact-less Tier A/B prospect spill into a
   `limit` slot and land in Manual Review with nobody to send to — that
   wasted a slot and buried a "no contact" case inside a stage a person had
   to click into to discover. Now it never occupies a slot at all, and the
   exclusion is reported directly (see **excluded_no_contact** below) instead
   of requiring anyone to notice its absence. Tier and score themselves are
   never touched by any of this — a company disqualified from *this
   campaign's selection* for lacking a contact can still be selected into a
   future campaign the moment Hunter finds one (a later `contacts.discover_run`
   re-run on the Research side, or `--force`).
2. **Draft up front.** For every selected prospect, `personalize.draft_message()`
   runs **once, immediately** — not deferred to send time (see below for why
   this matters).
3. **Stage decision — one remaining needs_review gate.** Every selected
   prospect already has a contact (step 1), so the only thing that can still
   hold an item in `needs_review` instead of `pending` is personalization:
   `draft.personalized_from == "template_only"`. Because `draft_message()`
   now tries several tiers before giving up (see §3 below), this only
   happens when a prospect has neither `csg qualify` output nor any other
   usable research field (industry/description/open reqs/score
   explanation) *and* no LLM key is configured to draft from it — genuinely
   nothing to personalize from, not just "qualify hasn't run yet." The item
   still exists and is visible, just not send-ready; the only ways out are a
   person approving it, or a redraft that earns real personalization
   (below).

**excluded_no_contact.** Both `POST /import` (via its job result) and
`POST /campaigns` (in its response body) return an `excluded_no_contact`
list — every tier-matching company left out for lacking a contact, as
`{"domain", "name", "tier"}`. The Research Agent's own hand-off
(`discover.py`'s `_handoff_to_outreach`) merges the import-time and
campaign-time lists (de-duplicated by domain) into `stats.outreach`, so a
fully-automatic Tier A/B → qualify → contacts → Outreach run still surfaces
exactly which companies never made it to a prospect or a campaign item, with
a message shaped like "No contacts found for Acme Co, so it was not added to
prospects/campaign" — never just a smaller number with no explanation.
4. **Persist.** Each item upserts into `outreach_items` on
   `(campaign_id, research_domain)` — so re-running campaign creation against
   the same prospect set never duplicates rows.

## 3. Drafting — [personalize.py](src/outreach/personalize.py)

Same LLM-backend convention as the Research Agent's `qualify.py`: Gemini
first if `GEMINI_API_KEY` is set, else Anthropic. Any LLM or network
failure — auth, rate limit, malformed output — is swallowed and falls
through to the next tier (ultimately the template fallback) rather than
raising, **except** for `revise_message()` (below), where there's no
sensible fallback for an arbitrary free-text edit.

- **`draft_message()`** is the core entrypoint, called once per item at
  campaign-creation time (`use_web_search=False`), and again on demand from
  Redraft (`use_web_search=True`). Both the drawer preview and the actual
  send reuse the stored draft rather than calling this again — what a
  person reviews is guaranteed to be what gets sent, unless they explicitly
  revise or redraft. It tries, in order, stopping at the first success:
  1. **Qualify data** — if `pain_summary`/`recommended_angle` are present
     (from Research's `csg qualify` step), render
     [prompts/outreach_email.md](prompts/outreach_email.md) and call the
     LLM. Tagged `personalized_from="pain_summary"` or
     `"recommended_angle"`.
  2. **Cached research data** — if qualify hasn't run/finished yet but the
     prospect has other usable fields (industry, description, open reqs, or
     the scorer's explanation), render
     [prompts/outreach_email_from_research.md](prompts/outreach_email_from_research.md)
     and call the LLM instead of giving up. This is the tier that fixed the
     original bug where a real, well-researched Tier A/B company landed in
     `needs_review` just because `csg qualify` hadn't gotten to it yet.
     Tagged `personalized_from="research_data"`.
  3. **Live web search (redraft only, `use_web_search=True`)** — if neither
     of the above produced anything, render
     [prompts/outreach_email_web_search.md](prompts/outreach_email_web_search.md)
     and try three independent search-capable backends in order, each
     skipped (not treated as a failure) if its key(s) aren't configured:
     Gemini + its built-in `google_search` grounding tool, then Groq +
     Tavily (Tavily runs the actual search, Groq turns the snippets into a
     draft — same split as the Research Agent's
     `discovery/groq_tavily.py`), then Anthropic + its server-side
     `web_search` tool. An explicit `BODY_UNAVAILABLE` response (nothing
     usable found) is treated as a failed attempt, not a garbage draft.
     Tagged `personalized_from="web_search"`. Never attempted at campaign
     creation — only a deliberate redraft pays for a live search.
  4. **Static template** — last resort, when every tier above was either
     inapplicable or failed outright. Tagged `personalized_from=
     "template_only"` — the *only* value campaign creation's `needs_review`
     gate checks for.
- **Template fallback** is still tailored with what's free — company name,
  industry, open role titles — just without a pain point or angle to hook on.
- **`revise_message()`** (`/items/{id}/revise`) takes a free-text instruction
  and asks the LLM for the smallest edit that satisfies it, preserving
  everything else — tagged `personalized_from="revision"`. Raises rather than
  silently falling back if no LLM key is configured or the call fails, since
  there's no template that could honor an arbitrary instruction.
- **`redraft_item`** (`/items/{id}/redraft`) fully regenerates from the
  prospect's *current* cached data, with `use_web_search=True` — meant for a
  `needs_review` item stuck on the template, giving it every tier above
  (including a live search) one more chance to personalize. If the new draft
  is no longer `template_only`, the item is auto-promoted
  `needs_review → pending` — the same "earn your way into Pending" rule as
  campaign creation, just re-checked on demand.
  `POST /campaigns/{id}/redraft-generic` does this in bulk across every
  `template_only` item in a campaign.

## 4. Review — needs_review, approve, revise

A `needs_review` item is *never* auto-promoted just by waiting — only three
things move it to `pending`:

1. **`POST /items/{id}/approve`** — a person reads the generic draft in the
   Dashboard and decides it's fine to send as-is. Deliberate, manual, no
   redraft happens.
2. **`redraft`** / **`redraft-generic`** — succeeds in producing a
   non-`template_only` draft (see above).
3. Never automatically from import or campaign creation itself.

`revise` is allowed on both `pending` and `needs_review` items (editing a
draft doesn't require it to already be approved).

## 5. Sending — [pipeline.py](src/outreach/pipeline.py), [channels/](src/outreach/channels/)

`send_campaign()` owns the *only* stage transitions this agent ever makes on
its own: `pending → sent` (success) or `pending → failed` (send error).

- **Idempotent by construction.** It selects only items currently in
  `pending` — `needs_review` items can never be accidentally sent, and
  re-running a partially-failed campaign never re-sends an already-`sent`
  item.
- **Reuses the upfront draft.** The stored `subject`/`body` on the item is
  what gets sent, not a fresh `draft_message()` call — a second draft call
  only happens as a legacy fallback for items created before upfront-drafting
  existed. What a person approved is what goes out.
- **Rate limited.** `sending.rate_limit_per_minute` (default 20) enforces a
  minimum interval between sends — a politeness cap this agent imposes on
  itself, not something the email provider requires.
- **No contact → immediate fail**, counted as `skipped` rather than `failed`,
  if the cached prospect or its contact email is missing at send time.
- Dispatch goes through [channels/](src/outreach/channels/)'s single
  `send()` — today only `"email"` is implemented, plain SMTP
  ([channels/email.py](src/outreach/channels/email.py)), provider-agnostic
  (Gmail relay, SendGrid, Postmark, SES — whatever `SMTP_HOST` points at).

**`sending.dry_run`** short-circuits before touching SMTP and returns a fake
`dry-run-{uuid}` message id — this agent's equivalent of the Research Agent's
Hunter.io quota gate, except the cost being protected against is deliverability
/ sender-reputation risk, not a metered API bill.

## Stage tracking — the only write path

[`db.set_stage()`](src/outreach/db.py) is the **sole** legal way `outreach_items.stage`
ever changes. It reads the item first to capture `from_stage`, updates
`stage` + `stage_updated_at`, and writes an `outreach_events` audit row in the
same call — callers are told to never `PATCH outreach_items.stage` directly,
so every transition is guaranteed an audit trail. `mark_sent`/`mark_failed`
both call through it.

`waiting`, `replied`, `booked`, `not_interested` are the only stages the core
pipeline (this section) never sets itself (`POST /items/{id}/stage` enforces
that — 400 on anything else). They exist so the pipeline board has somewhere
to show a reply, a booked meeting, or a clear rejection ("Failed" in the
Dashboard) — set by the Conversation Agent
([section 6](#6-conversation-agent--conversation)) when it's enabled for a
campaign, or by a person through this same endpoint otherwise. Unlike the
other three, `not_interested` is terminal for the Conversation Agent's own
automated moves (see `CONVERSATION_TRANSITIONS`); a person can still re-open
it through this same endpoint, since the manual path is not bound by that
whitelist.

In the Dashboard, a person can trigger this same endpoint two ways — the item
drawer's stage buttons, or dragging a card between kanban columns (Sent
onward) — both are equally manual, both call `POST /items/{id}/stage`, and
neither is treated as more authoritative than the other.

## Scheduling — informational only today

`POST /campaigns/{id}/schedule` and `POST /items/{id}/schedule` (the kanban's
"Schedule campaign" kebab action) only set a `scheduled_at` timestamp on
`pending` items. **Nothing polls this column and auto-sends** — `campaign
send` (CLI or the Send button) still has to be triggered by a person. This is
a real, UI-visible gap, not an oversight to route around: treat
`scheduled_at` as a note to self, not a promise.

## Analytics — `_aggregate_analytics()` in [api.py](src/outreach/api.py)

`GET /analytics?campaign_id=all|<id>` computes the stage funnel across all
eight stages, reply/booking/failure rates (`booked` and `not_interested` both
count as a reply — booking or rejection both imply a reply happened; only
`booked` counts toward `booking_rate`, only `not_interested` toward
`failure_rate`), tier/industry/personalization-source breakdowns, and
sends-over-time. Deliberately mirrors the Research Agent's own
`_aggregate_analytics` shape so the two agents' analytics pages read as the
same product in the Dashboard.

## Jobs — import and send progress

`import` and `send` both run in a thread-pool executor (`_MAIN_LOOP.run_in_executor`),
not as asyncio tasks, since the underlying `db`/`ingest`/`pipeline` code is
synchronous `httpx`. Progress streams to the UI via an in-process `Job`
snapshot (`job_id`, `status`, `done`/`total`/`current_domain`) fanned out over
a `WS /jobs/{job_id}/ws` socket, same pattern as the Research Agent. This
state is in-memory only — a server restart loses progress tracking, never the
underlying data, since every mutation already landed in Supabase before the
job snapshot updates.

## Two ways to run it

- **CLI** ([cli.py](src/outreach/cli.py)) — `outreach import-prospects <run_id>`,
  `campaign create`, `campaign send <id>`, `campaign list`, `campaign delete <id>`,
  `campaign pipeline <id>` (Rich table of the board), `item set-stage <id> <stage>`
  (`waiting`/`replied`/`booked` only — no `approve`/`redraft`/`schedule` from
  the CLI; those are API/Dashboard-only actions).
- **Web app** — [api.py](src/outreach/api.py) (FastAPI) serves the same
  `ingest`/`personalize`/`pipeline` modules over HTTP + the jobs WebSocket,
  consumed by `Dashboard/src/pages/outreach/` (Campaigns list, Campaign
  Detail kanban, Analytics). Outreach has no frontend of its own — it's a
  set of pages inside the shared Dashboard app, talking to this agent's API
  on port 8001 while the Research Agent's own API runs on 8000; both must be
  running for the Dashboard's Outreach pages to work.

Both paths call the same `ingest`/`personalize`/`pipeline` functions, so the
CLI and the API can never drift apart.

## Storage — Supabase (Postgres via PostgREST), separate project

A **different** Supabase project from the Research Agent's — never joined at
the DB level (Postgres can't FK across projects anyway). `research_domain`
and `research_run_id` are plain copied reference strings, not foreign keys.

Four tables ([schema.sql](schema.sql)): `prospects` (PK `research_domain`,
no campaign ownership — a cache, reusable across campaigns), `campaigns` (PK
`campaign_id`), `outreach_items` (PK `item_id`, FKs into both `campaigns` and
`prospects` with `ON DELETE CASCADE`, `UNIQUE(campaign_id, research_domain)`
— this is what makes a prospect appear at most once per campaign and makes
campaign creation idempotent), `outreach_events` (PK `event_id`, FK to
`outreach_items`, cascade) — pure append-only audit log, one row per
`set_stage` call.

`delete_campaign` cascades by hand (events → items → campaign) even though
the FKs would also cascade — belt and suspenders, since PostgREST has no
application-level cascade logic of its own. Deleting a campaign never touches
the `prospects` cache — those rows belong to no campaign and are reusable by
a future one.

**`DELETE /research/runs/{run_id}`** (`db.delete_by_research_run`) is the one
exception to that "prospects are reusable, never deleted" rule above — it
*does* delete `prospects` rows, scoped to `research_run_id`, along with every
`campaigns` row for that run (and their items/events, same hand-cascade as
`delete_campaign`). Not called by this agent's own frontend; it exists
purely as the teardown counterpart the Research Agent calls when it deletes
a run (see [Research_Agent/WORKFLOW.md](../Research_Agent/WORKFLOW.md#deleting-a-run)),
so an Outreach prospect/campaign never outlives the research run it was
imported from. Idempotent — a `run_id` with nothing imported returns
`{"campaigns_removed": 0, "items_removed": 0, "prospects_removed": 0}`
rather than erroring, since the Research Agent calls it unconditionally on
every run delete regardless of whether a hand-off ever happened. See
[test_delete_research_run.py](tests/test_delete_research_run.py).

RLS is intentionally left disabled: this agent is accessed only via the
service-role key from a trusted backend, same posture as the Research Agent.

## Config — [config/outreach.yaml](config/outreach.yaml)

- `research` — `api_base` (overridable via `RESEARCH_API_BASE` env var, so a
  dev/prod split never needs a YAML edit), `qualified_tiers`.
- `selection` — `limit` (25), `tiers` (A,B), `min_score`, `require_contact_email`.
- `sending` — `channel` (`email` only today), `from_name`/`from_email`/`reply_to`
  (also env-overridable), `rate_limit_per_minute`, `dry_run`.
- `personalization` — prompt template paths, `max_open_reqs_cited`,
  `sender_name`, and a full multi-line `sender_signature` block (not just a
  name).
- `stages` — a human-readable `order` list and an `owned_by_this_agent` /
  `owned_by_conversation_agent` split, mirroring (but shorter than) the
  authoritative `Stage`/`STAGE_ORDER` in [models.py](src/outreach/models.py),
  which is the one that actually governs behavior — treat the YAML list as
  documentation, not a second source of truth to keep byte-for-byte in sync.
- `conversation` — the Conversation Agent's defaults for a campaign that has
  never been configured (`enabled`/`auto_reply` both off, `followup_after_days`).
  The live per-campaign switch lives in the database, not here — see
  [section 6](#6-conversation-agent--conversation).

## 6. Conversation Agent — [conversation/](src/outreach/conversation/)

Owns everything after Sent: `sent → waiting → replied → booked`. It runs
inside this same backend process and Supabase project, mounted onto this
agent's FastAPI app by one `include_router` line in
[api.py](src/outreach/api.py) — removing that line removes the Conversation
Agent from the API entirely and leaves steps 1–5 above exactly as they were.

**Off by default, per campaign** — two independent switches (`enabled` and
`auto_reply`), not one, because "watch the inbox and keep the board current"
and "reply to prospects unattended" are different levels of trust:

| Switch | Off (default) | On |
|---|---|---|
| `enabled` | No inbox reads, no card moves, no sends. | Detects replies, classifies them, moves cards, drafts replies, handles follow-ups. |
| `auto_reply` | Drafts every outgoing reply and waits for approval. | Sends them itself. |

**A tick** (`outreach conversation tick <campaign_id>`, or the Dashboard's
"Check replies" button) does, in order: (1) refuses unless `enabled`; (2)
loads only conversation-stage items — `pending`/`needs_review` items are
never even queried; (3) fetches recent mail and matches it to an item via
four strategies (threading headers → threading headers against a prior
outbound message → sender address → `Reply-To`, the last one specifically
for scheduling-tool notifications like Calendly, where the actual invitee's
address is in `Reply-To`, not `From`/`To`); (4) classifies each new message —
a scheduling tool's own confirmation is recognized deterministically as
`booked` before any LLM call runs; (5) moves items silent 24h to `waiting`
and nudges `waiting` items within the follow-up cap.

**Safety properties**: idempotent (`UNIQUE(item_id, provider_message_id)`,
IMAP `\Seen` flags are not trusted); forward-only transitions
(`models.CONVERSATION_TRANSITIONS` whitelist); `enabled` re-read immediately
before every send, not once per tick; an advisory lock
(`conversation_locked_at`, reclaimable after 10 minutes) prevents two
overlapping ticks from double-processing; autoresponders/bounces are
detected deterministically and never move a card or count as engagement;
every reply/follow-up carries real `In-Reply-To`/`References` headers so it
threads in the prospect's mail client.

Reply checking is manual today (button or CLI tick) — there is no background
poller yet, by design, until the flow has been exercised against real mail.
See [README.md's Conversation Agent section](README.md#conversation-agent-agent-3)
for full setup (IMAP credentials, schema) and CLI reference.

## 7. Learning — [learning/](src/outreach/learning/)

The Learning Agent is fully orthogonal to the above: it observes **every** draft
creation, revision, and redraft; tracks the outcomes from existing `outreach_items`
and `conversation_messages` data; learns patterns; and feeds active insights back
into new drafts as structured prompt guidance. It is **completely additive** — if
it fails for any reason, the core pipeline (1–5 above) runs unchanged.

**Observation:** Every `/revise`, `/redraft`, `/redraft-generic`, and
conversation-reply operation snapshots the before/after subject+body into
`draft_versions`, computes observable diffs (subject/opening/CTA/length/
personalization/formatting changed), and stores them. Writes are wrapped in
independent `try/except` blocks so a failure never blocks the actual draft
response.

**Tracking:** `draft_versions` rows are linked to real outcomes (replies,
positive replies, bookings, stage progression) via live reads from `outreach_items`
and `conversation_messages` — no duplicated outcome storage. The outcome cache
on `draft_versions` is refreshed on-demand.

**Learning:** A fixed catalog of pattern detectors runs on-demand (no background
job):
- Outcome-linked patterns: "shorter CTAs → higher reply rate," "more
  personalization → more bookings," etc. (splits draft_versions by a flag, compares
  linked outcomes).
- Frequency-only patterns: "72% of human edits shorten the CTA," etc. (no
  outcome-linking, just counts).
- Follow-up timing: buckets by follow-up count, compares reply/booking rates.

**Confidence:** Patterns only become "active" when **both** gates pass:
1. Sample size: ≥20 total emails, ≥10 per group.
2. Effect size: ≥1.5 percentage-point gap between groups.

Five emails never trigger a high-confidence rule, even with a 50pp difference.

**Guidance:** When drafting a new email, `learning.context.get_learning_context_summary()`
retrieves active insights and appends them to the personalization prompt as
structured guidance (e.g., "Based on prior outreach performance, the following
patterns have been observed: - Shorter CTAs have shown a higher observed reply
rate (evidence: 184 emails)"). This is fed into every tier of `draft_message()`
and `revise_message()`, never hard-coded sentences.

**API:** 9 read-only `/learning/*` endpoints expose overview, insights, history,
human-edits, performance metrics (before/after, campaign comparison, follow-up
buckets), and recommendations. All wrap database access and pass no errors to the
caller — missing data renders as empty/insufficient-data states rather than 500s.

**Dashboard:** The **Learning & Optimization** tab (under Outreach Agent) shows
all of the above: overview card counts, before/after performance windows with
explicit "Not enough data yet" messaging (never fabricated metrics), detected
patterns, confidence tiers, human-edit trends, and clickable drill-down into
the data backing each insight. Timestamps and sample counts make every row
traceable.

## 8. Campaign-Level Optimization — [optimization/](src/outreach/optimization/)

A second, independent additive layer, sibling to Learning rather than built
on top of it — it does not modify, subclass, or route through anything in
`learning/`. Where the Learning Agent diffs a human's edit against the prior
draft version, Optimization compares whole *campaigns and strategies*
against each other: ICP/company type, email style, subject/opening style, CTA
style, length, personalization level, and follow-up count/timing, each
against reply rate, positive-reply rate, booking rate, and no-reply rate.
Read-only against everything but its own two tables (plus one opt-in write —
recording an adoption); every call site wraps it in `try/except` so a failure
here can never block drafting, sending, or any Research/Outreach/Conversation/
Learning behavior.

**Strategy extraction** ([strategy.py](src/outreach/optimization/strategy.py)):
pure, rule-based classification of a sent item's stored subject/body into
`length_bucket`, `subject_style`, `opening_style`, `cta_style`,
`personalization_level`, `email_style` — no LLM calls, recomputed on-demand
from the current `outreach_items` snapshot, never stored as its own row, so it
can never drift out of sync with an edited draft. This is the one genuinely
new signal source this layer needs: the Learning Agent only ever diffs a
human edit against its prior version, so it has nothing to say about a
campaign that was never edited at all — strategy extraction lets every sent
item be classified and compared, edited or not.

**Insight computation** ([engine.py](src/outreach/optimization/engine.py)):
for each dimension, splits sent items into two comparable groups (e.g. direct
vs. soft CTA) and compares their outcome rates, picking whichever of
booking/positive-reply/reply rate the data can actually support as the
primary metric. Reuses the Learning Agent's exact confidence thresholds
(imported, not re-declared) — "how much evidence is enough" means the same
thing everywhere in this codebase. Every dimension is computed both globally
and within every ICP segment (`prospects.tier`, `prospects.industry`) that
independently has enough data — a segment is only ever reported "active" on
its own evidence, so a real per-segment effect can't be flattened into a
misleading global number, and vice versa.

**Guidance** ([context.py](src/outreach/optimization/context.py)):
`fetch_optimization_context_safely()` builds a short bullet-list blurb from
active insights and is appended alongside (never merged with) the Learning
Agent's own blurb in `revise_item`/`redraft_item`/`redraft_generic_items`
(see [api.py](src/outreach/api.py)) — two separate, independently-optional
guidance sections in the same prompt.

**Adoption tracking** ([tracking.py](src/outreach/optimization/tracking.py)):
`POST /optimization/adoptions` is a purely human-initiated marker — "this
campaign was built following pattern X" — never inferred or auto-tagged.
`GET /optimization/adoptions/{pattern_key}/outcome` then reports the
adopting campaign(s)' observed outcome rates side by side with all other
campaigns' rates over the same data, always with an explicit
`causality_note`: this is an observed association, not a controlled
experiment, since campaigns are chosen by whoever creates them, not assigned
by this layer.

**Every insight is worded as a correlation, never a cause** ("associated
with", "observed" — never "causes" or "improves"), and a pattern with too
little evidence stays `insufficient_data` rather than being labeled with a
confidence tier its sample size doesn't support.

**API:** `GET /optimization/overview`, `/insights`, `/insights/{pattern_key}`,
`/recommendations`, `/adoptions`, `/adoptions/{pattern_key}/outcome`, plus
`POST /optimization/adoptions` — see
[routes.py](src/outreach/optimization/routes.py).

**Dashboard:** the Learning & Optimization tab's **Strategy Insights** and
**Adoption Tracking** sections.

**Setup:** no additional setup beyond the main schema. New tables are in
[schema.sql](schema.sql); the migration
[006_campaign_optimization.sql](migrations/006_campaign_optimization.sql) is
idempotent and purely additive (both new tables only FK *out* to `campaigns`,
so dropping them is always a clean, isolated removal).

## Known limitations

- **Scheduling doesn't send.** `scheduled_at` is stored, never acted on.
- **One channel.** Only email is implemented; `channels.send()` is written to
  dispatch by name so a second channel is a new module + dispatch entry, not
  a rearchitecture.
- **Reply checking is manual.** The Conversation Agent runs on a button click
  or a CLI tick — there is no background poller yet, by design, until the
  flow has been exercised against real mail.
- **Learning and Optimization recomputation are synchronous per-request.**
  Acceptable at current volumes but could move to frontend caching or
  event-driven if `draft_versions`/`outreach_items` grow very large.
  Confidence thresholds are tunable constants in `learning/engine.py`
  (Optimization imports the same ones, not a separate set), not yet exposed
  as config.
- **Optimization segmentation is fixed to two ICP dimensions.** Only
  `prospects.tier` and `prospects.industry` are enumerable segments today
  (`engine.py`'s `SEGMENT_DIMENSIONS`) — adding a new segment axis means
  adding it there explicitly, not something the data auto-discovers.
