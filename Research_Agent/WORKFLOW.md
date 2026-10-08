# How the agent works

A region goes in; a drafted, personalized outreach campaign comes out.
Discovery, ATS detection, and scoring are deterministic-or-Gemini and always
run. Qualify, contacts, and the Outreach hand-off cost real API quota (LLM
tokens, Hunter.io's 25/month) and are gated by tier + a per-run cap, not by a
flag you have to remember — **when the run is started via `POST /discover`
(the Dashboard), every Tier A/B company automatically gets qualified,
contact-searched, and (if `OUTREACH_API_BASE` is configured) handed off to
the Outreach Agent**, capped at `icp.yaml`'s `outreach.auto_qualify_top_n`
(default 25, highest-scored first) so an arbitrarily large run can't
silently exhaust Hunter's monthly quota in one shot. The CLI keeps this
opt-in (`--auto-qualify-top N`, behind a cost confirmation) rather than
automatic, since a person running raw commands should consciously choose to
spend quota — see **Two ways to run it** below for the full CLI-vs-API split.

```
          REGION  (the only required per-run input)
             |
             v
 [0] DISCOVER + RESEARCH ──> [1] JOB DISCOVERY ──> [2] CLASSIFY REQS ──> [3] SCORE + TIER
     (ATS -> careers page -> web search -> unknown)
     (Gemini, grounded)              |                                      |
             |                       v                                      v
      companies table         job_postings table                      scores table
             |                                                              |
             |                                                              v
             |                              [4] QUALIFY Tier A/B, capped (LLM)
             |                                                              |
             v                                                              v
    discovery_sources                          CONTACTS Tier A/B, capped (Hunter.io)
                                                                             |
                                                                             v
                                              [5] OUTREACH HAND-OFF (if configured)
                                              import as prospects, draft a campaign
                                                    -- never sends anything
```

Every criterion except the region lives in the single
[config/icp.yaml](config/icp.yaml) — size band, financial thresholds,
practice areas, disqualifiers, scoring weights, tier cutoffs, and the
qualify/contacts cap. It is never re-entered per run, and there is no
profile selector anywhere in the app — one ICP, always in effect. Company
size and revenue are **soft signals, not disqualifiers**: `disqualify()`
only hard-fails on `size.hard_min`/`hard_max` (left wide open by default —
see the file's own comments), never on the `sweet_min`/`sweet_max`/
`revenue_sweet_min` band that scoring uses to rank. The Settings page's
**Built-in ICP** card shows every criterion in this file and can edit two of
them in place — Employee Size Band and Revenue Target — via
`PATCH /settings/icp` (`config.update_icp_basics()`); everything else
(region, tiers, weights, practice areas, disqualifiers) is display-only
there and stays a direct file edit.

## Quick summary

```
Pick a region → Agent does everything else, through a drafted campaign
```

1. **You pick a region** (GA / East Coast / Midwest / West Coast) — in the
   UI or `csg discover [region]`.
2. **Discover** — Gemini searches that region using queries generated live
   from `icp.yaml` (size band, revenue floor, practice areas); falls back to
   Tavily+Groq if every Gemini key is exhausted.
3. **Research** — each new company gets headcount/revenue/funding/industry,
   with source + confidence tracked. Already-known companies are skipped, not
   re-billed. Same Tavily+Groq fallback as discovery if Gemini is exhausted.
4. **Job discovery** — tries ATS detection first (Greenhouse, Lever, Ashby,
   Workday, SmartRecruiters, Workable, BambooHR, Rippling); if no board is
   found, falls back to scraping the company's own careers page, then an
   external web search — never straight to "zero jobs" — before classifying
   whatever reqs were found by practice area and US/LATAM/Pakistan delivery
   fit. See **Job discovery fallback chain** below.
5. **Score + tier** — six weighted dimensions (hiring need, fill pain,
   firmographic fit, financial strength, buying signals, delivery fit) →
   A/B/C/D. Every company is scored, even one with no confirmed job signal
   at all — it just scores low on the dimensions that need job-posting data,
   and that "unknown" is recorded, never mistaken for a confirmed zero.
6. **Export** — CSV + JSONL, ranked. Each company is flagged `outreach_ready`
   once it has an email contact (LinkedIn is a bonus, never required).
7. **Qualify + contacts, automatically, for Tier A/B** — every Tier A/B
   company (capped, see above) is qualified (`csg qualify`'s LLM
   pain-summary logic) and looked up on Hunter.io — no extra click or
   command needed on the Dashboard path. A lower-tier company is still
   scored and shown, just not qualified/contacted.
8. **Hand off to Outreach, automatically** — if `OUTREACH_API_BASE` is set,
   the same Tier A/B companies are imported into the Outreach Agent as
   prospects and drafted into a campaign, but only the ones Hunter actually
   found a contact for — a contact-less company is excluded outright and
   reported via `excluded_no_contact` (see **Outreach hand-off** below).
   **Never sends anything** — a drafted item lands in `pending` (ready, a
   person still clicks Send) or `needs_review` (Manual Review — genuinely
   personalized draft not yet possible; every selected item already has a
   contact by this point). `csg qualify`/`csg contacts` remain available to run by hand
   afterwards for any run, with full tier/force control, independent of what
   a given run's automatic cap covered.

The sections below go through each stage in full detail.

## 0. Discovery — [src/csg/discover.py](src/csg/discover.py), [src/csg/discovery/](src/csg/discovery/)

`csg discover [region]` (or `POST /discover`) is the main entrypoint. With no
region it uses `geo.search_regions.active` from the ICP.

1. **Search.** [build_discovery_prompts()](src/csg/discovery/web_search.py)
   generates queries *from the ICP config* — the region's hubs, the size band,
   the highest-weighted practice areas, and the revenue floor. Editing
   icp.yaml changes what gets searched; no code edit. Two complementary
   queries run (one hub-anchored, one hiring-anchored) against Gemini with
   grounded search.
2. **Resolve.** Results are cleaned, blocklisted domains (job boards, social
   networks, staffing competitors) dropped, and deduplicated by normalized
   domain (scheme, `www.`, port, path, case all stripped — see
   [src/csg/dedupe.py](src/csg/dedupe.py)).
3. **Global de-duplication.** All existing companies (from any prior run) are
   loaded once. Each discovered candidate is checked against them by normalized
   domain (primary key) or normalized name (fallback, for exact name matches ≥5
   characters). A match is classified as a true duplicate (and excluded from
   the rest of the pipeline) only if it has a successful qualification in any
   prior run, or is suppressed/disqualified. All other matches are **re-eligible**:
   they remain in the pipeline, reuse their stored domain (and region), and
   compete in this run's own scoring and top-N pool — they are enriched, scored,
   and handed to Outreach anew if they rank high enough in this run.
   
   Truly duplicated companies are recorded in `run_company_matches` and shown
   with an "Already exists" badge in the run, never re-enriched or re-scored.
   Re-eligible companies do not appear in `run_company_matches` and flow through
   the normal pipeline. A concurrent run that claims the company first wins
   atomically, and the race loser treats it correctly (as duplicate or re-eligible)
   based on its actual qualification/suppression status.
4. **Research.** Each *new* (genuinely discovered, not matched) company gets
   one Gemini call for headcount, revenue, funding, industry, and description.
   Companies already researched in a previous run are skipped — firmographics
   don't change run to run — and the rest run concurrently (`ENRICH_CONCURRENCY`),
   not serially.

Every researched figure is stored as a **provenance envelope**
(`models.ProvenanceValue`): value plus source, URL, date, confidence, and
`method` — `reported` vs `estimated`. Nothing is invented, and an estimate is
never presentable as a reported fact.

If discovery cannot run at all (no API key, every query failing), it raises
`DiscoveryError` rather than returning an empty list — a broken run and a run
that genuinely found nothing must not look the same.

## 1. ATS detection — [src/csg/ats/](src/csg/ats/)

For each discovered company, `ats.detect()` tries to identify which applicant
tracking system the company uses. Eight providers, one module each, all
public/unauthenticated APIs: **Greenhouse**, **Lever**, **Ashby**, **Workday**,
**SmartRecruiters**, **Workable**, **BambooHR**, **Rippling**.

Detection is three-pronged, highest-precision first (see
[src/csg/ats/\_\_init\_\_.py](src/csg/ats/__init__.py)'s module docstring for
the exact rationale behind each):

1. **Careers-page scan.** Fetch the site (apex domain + common `/careers`-style
   paths) and look for a real board link in the HTML — reliable because the
   token is *read*, not guessed.
   - If the top-level careers page is only a landing page (a big "Explore
     open positions" button with no board link on the page itself), same-domain
     links whose path looks like a listing (`open-positions`,
     `current-openings`, ...) are followed one hop deep and scanned the same
     way. This is what makes a company like DataRobot — whose `/careers/` page
     has nothing, but `/careers/open-positions/` links straight to a Workday
     board — detectable at all.
   - If nothing on the apex domain (or its one-hop subpages) has a board link,
     a few common careers subdomains (`careers.{domain}`, `jobs.{domain}`) are
     tried the same way, for companies that host their whole careers site
     there instead of under a path.
2. **Token probe.** Derive candidate tokens from the domain/name and probe
   each provider's API directly. Lower hit rate, catches sites that render
   careers via JS with no static link to find. Not applicable to Workday (its
   shard, e.g. `wd1`/`wd3`, cannot be guessed from the domain/name) or
   SmartRecruiters (its API returns `200` for *any* token, real or not — no
   reliable "not found" signal to probe against, see
   [smartrecruiters.py](src/csg/ats/smartrecruiters.py)'s module docstring).

Expect roughly 30–40% of an arbitrary company list to resolve on
GH/Lever/Ashby/Workable/BambooHR/Rippling alone; Workday and SmartRecruiters
add enterprise coverage but only via a real board link, never a guess. **iCIMS,
Taleo, and Jobvite remain unsupported** — no reliable public,
unauthenticated API was found for any of them on direct research (not
assumed). **Phenom** (now common among large enterprises — CVS Health, United
Airlines) renders entirely via client-side JS with no static signal, which
this project's fetcher deliberately doesn't render (no headless browser).

**Rippling** (`ats.rippling.com/{token}/jobs`) was added after investigating
why several real, actively-hiring companies (e.g. Copado) were scoring
D-tier despite being good ICP fits: `ats.detect()` returned `None` for them
not because detection failed, but because Rippling — a fast-growing ATS
among mid-size tech/SaaS companies — had no adapter at all. Its board page is
server-rendered Next.js with the job list embedded as a `__NEXT_DATA__`
JSON blob (`dehydratedState.queries[].state.data.items`, keyed by a
`["board", token, "job-posts", ...]` query key) rather than a clean REST
endpoint; the adapter parses that directly instead of the `/_next/data/
{buildId}/...` route, since `buildId` changes on every Rippling deploy and
the rendered page always matches whatever build actually served it. Unlike
SmartRecruiters, a wrong Rippling token reliably 404s, so it's both
careers-page- and token-probe-detectable — added to `PROBEABLE_PROVIDERS`.

**Why this matters for scoring:** four of the six scoring dimensions (Fill
Pain, Hiring Need, Buying Signals, CSG Delivery Fit) are entirely
job-posting-derived. A company with zero *confirmed* reqs scores those
dimensions as `0 (unavailable)`, capping it near the Firmographic+Financial
ceiling — landing in D-tier regardless of how good the company actually is.
**A D-tier score is therefore ambiguous between "bad-fit company" and
"good-fit company whose jobs we can't see."** This is exactly why ATS
detection alone is never treated as the last word on a company's job
signal — see the fallback chain below.

If no board is found, the company is marked `no_ats` — but `no_ats` means
specifically "no ATS board found," **not** "no job signal." The resolved
`ats_provider` + `ats_token` (when one is found) are cached on the company
row so a re-run skips detection next time (unless `--force`/"Force re-detect"
is set).

## 2. Job discovery fallback chain — [src/csg/jobs_discovery.py](src/csg/jobs_discovery.py), [src/csg/pipeline.py](src/csg/pipeline.py)

ATS detection is the highest-precision *method* of finding a company's open
jobs, but it is one method, not a requirement for scoring one. Before this
module existed, an unsupported/undetectable ATS collapsed straight into
"zero reqs," which the four job-posting-derived dimensions above read as "no
hiring need" — this is what caused a real investigation (see git history:
several actively-hiring companies, e.g. Copado on Rippling, scored D-tier
purely because their ATS had no adapter, not because they were poor fits).
`jobs_discovery.discover_jobs()` is a bounded fallback chain that keeps
trying cheaper, reliable methods before ever giving up:

```
ATS  ->  careers-page job scrape  ->  external web search (Tavily)  ->  UNKNOWN
```

1. **ATS** (unchanged) — `ats.fetch_postings()` pulls every open req from a
   resolved board. Zero incremental cost over detection, exact structured
   data. On success: `job_discovery_method="ats"`, `hiring_signal="confirmed"`.
2. **Careers-page scrape** (`_scrape_careers_page_jobs`) — when no ATS was
   found (or the ATS fetch itself errored), re-scans the careers page(s) for
   things that look like individual job links (`/careers/senior-engineer`,
   `/jobs/12345-...`) or a `JobPosting` JSON-LD block (a common Google-for-
   Jobs SEO pattern many sites embed with no third-party ATS at all). Free,
   first-party, lower structure than a real ATS API.
3. **External search** (`_search_jobs_external`) — Tavily, same provider as
   discovery's and enrichment's own Tavily+Groq fallback (see below), plus
   its own two retry layers so this last-resort stage isn't sunk by a single
   blip: a 429 is retried up to `_TAVILY_RATE_LIMIT_RETRIES` times (existing
   behavior), and — since this is the *only* method left once ATS and the
   careers page have both failed — a transport-layer failure (DNS lookup,
   connect/read timeout, connection reset; `classify_error` in
   `"timeout"/"connection_error"`) is now *also* retried, up to
   `_TAVILY_CONNECTION_ERROR_RETRIES` times with exponential backoff
   (`_tavily_post_with_connection_retry`). The two budgets are independent
   per HTTP attempt, so a DNS blip doesn't eat into the rate-limit retry
   allowance or vice versa. Only runs once both free methods have failed to
   find anything; results are filtered to titles that actually look like a
   job ("hiring", "opening", "apply", ...), not a bare "Careers at Acme"
   link. Paid/quota-metered, and weaker evidence than a first-party page —
   recorded as such via `JobSource.web_search`.
4. **UNKNOWN** — every method failed or found nothing. The company is
   **not** scored as having zero hiring need: `hiring_signal="unknown"` is
   recorded (distinct from `"confirmed"`, which a genuinely-empty ATS board
   or careers page also gets — a source was actually read there, it was
   just empty). Scoring already treats "no postings" as *unavailable*, not
   *zero* (see scoring.py's module docstring), so this makes the provenance
   honest without needing a scoring-side change.

One failed stage never aborts the chain — an exception in any stage
(ATS fetch timeout, a careers-page fetch error, a Tavily rate limit/429 or
connection/DNS error once its own retries are exhausted) is caught,
classified via `usage_tracker.classify_error`, appended to
`job_discovery_notes`, and the chain moves to the next method. Every
company's `companies` row records `job_discovery_method`, `job_source`,
`hiring_signal`, and `job_discovery_notes` — the full provenance of how (or
whether) its job signal was found, exported to CSV/JSONL alongside the
score. Log lines during a run read like:

```
[jobs_discovery] acme.com: ATS detected (greenhouse) -> 18 jobs found
[jobs_discovery] acme.com: No ATS detected -> Careers page -> 12 jobs found
[jobs_discovery] acme.com: No ATS -> Careers page empty -> Web search -> 7 jobs found
[jobs_discovery] acme.com: All methods exhausted -> Job data unknown (ATS not detected; careers page reachable but no jobs found; TAVILY_API_KEY not set)
```

A run's summary (`RunResult.job_discovery_methods` / `hiring_signal_unknown`)
tallies these across the whole run, so "how much of this run actually has
confirmed job signal" is answerable without backend log spelunking — see
`stalled_companies`'s existing UNKNOWN-adjacent concept in **Resuming**
below for a related but different distinction (stalled = no ATS *and* no
firmographics at all, the signature of every provider being unreachable;
`hiring_signal="unknown"` is narrower — just the job side).

## 3. Classify reqs + score — [src/csg/pipeline.py](src/csg/pipeline.py), [src/csg/scoring.py](src/csg/scoring.py)

Once postings exist (from any of the three sources above), each req is
classified twice, deterministically and for free: by **practice area** (the
icp.yaml keyword patterns) and by **delivery geography** — US onshore,
LATAM nearshore, or Pakistan offshore, via
[delivery.py](src/csg/delivery.py). Delivery classification follows the role's
real constraints in priority order: a security clearance or citizenship
requirement forces US and can never be overridden; then an explicitly named
region; then role shape (leadership and client-facing stay onshore,
async-tolerant QA/support/data work offshores well, core engineering defaults
to nearshore). A req that indicates nothing returns `None` and is excluded
rather than guessed at.

**Every discovered company gets scored and lands in the pipeline** — a
`hiring_signal="unknown"` company (or one with confirmed zero reqs) is not a
disqualifier, it just has no job-posting signal to score `hiring_need`/
`fill_pain` from, so it naturally scores low on those dimensions and lands
in Tier C/D rather than being dropped before a score row exists. This means
every research run's Companies tab shows every company it discovered, not
just the ones with a confirmed job signal.

All companies then go through **deterministic scoring** — pure function of
`config/icp.yaml` + company + postings, no network, no LLM
([scoring.py](src/csg/scoring.py)):

1. **Hard disqualifiers checked first** (competitor-name match, mostly-agency
   req mix, zero reqs in an enabled practice area, all-foreign req
   footprint) — a disqualified company gets a `dq_reason` and stops here.
2. **Six weighted dimensions** (weights must total 100 — enforced at config
   load, so an edit that doesn't balance fails fast):
   - `hiring_need` (35) — req count band + share of reqs matching a practice
     area (AI/ML, DevOps, data eng, etc.) + reqs-per-100-employees.
   - `fill_pain` (20) — median req age, share of stale reqs, urgency language
     in postings ("hiring urgently", "ASAP").
   - `firmographic_fit` (15) — company size vs. the configured sweet band,
     industry match, US geography match.
   - `financial_strength` (10) — revenue against the `$10M+` target (with
     partial credit below it) and whether funding is on record. Can they
     actually fund an engagement?
   - `buying_signals` (10) — multiple locations, contract/C2H language in
     postings (already buys contingent labor), a recent burst of new reqs,
     and growth language (funding rounds, new offices, expansion,
     hypergrowth) in postings or the company description — a forward-looking
     signal distinct from fill_pain's "already hiring urgently."
   - `csg_delivery_fit` (10) — what share of the req book CSG could staff, and
     from where. Nearshore/offshore capacity is weighted above onshore, since
     that is CSG's margin advantage over a purely domestic agency.
3. **Missing-data policy**, applied at two levels. Within a dimension, a
   component whose input is unknown (e.g. no `employee_count`) is marked
   *unavailable*, not scored zero, and the dimension rescales over what *is*
   available — capped at 1.35x so a thinly-known company can't out-score a
   well-known one on guesswork. Across dimensions, one that could not be
   evaluated at all (e.g. a company with no public financials) is excluded
   from the 100-point denominator entirely, so "we don't know" never reads as
   "confirmed zero" — an unresearched company must not rank below one known to
   be weak.
4. Final score maps to a **tier** via `icp.yaml` cutoffs: A ≥ 70, B ≥ 50,
   C ≥ 35, below C is dropped from reports (still stored, status `scored`).
   Inside the top band, CSG's accessibility preference applies: startups and
   SMBs stay at A because they are materially easier to penetrate, while a
   **known** large enterprise without standout hiring urgency settles at B.
   Unknown signals never demote — missing headcount means "not established as
   hard to penetrate", not "assume the worst".

Every point awarded is logged as a `ScoreReason` string — `csg show <domain>`
prints the full arithmetic, so a company's tier is always explainable.

Each pipeline run is idempotent and resumable: company `status` is the
checkpoint (`discovered → no_ats | ats_found → no_reqs | scored | disqualified`),
so killing a run and restarting skips work already done, and the HTTP fetch
layer ([src/csg/http.py](src/csg/http.py)) caches responses
(`cache_ttl_hours` in `icp.yaml`, default 168h) so re-fetching is free within
that window.

## 4. Qualify — [src/csg/qualify.py](src/csg/qualify.py) (costs tokens)

Takes each qualifying company's score breakdown + a sample of open reqs,
sends it to an LLM using the prompt template in
[prompts/qualify.md](prompts/qualify.md), and gets back two fields:
`pain_summary` and `recommended_angle`. Stored in `qualifications`, keyed by
`(domain, run_id)` — kept separate from the deterministic score so
qualification can be re-run with a different model/prompt without
re-scoring.

**Every attempt is recorded, success or failure — a failure is never
silently indistinguishable from "never tried."** Alongside `qualifications`
(success-only), each domain's outcome is upserted into
`qualification_attempts` (`domain, run_id, status, error_reason,
error_detail, attempt_count, last_attempted_at` — see
[migrations/004_qualification_attempts.sql](migrations/004_qualification_attempts.sql)),
whether the LLM call actually succeeded or errored out. This is what lets
the Company Detail page distinguish "not qualified yet" (no attempt row)
from "qualification failed" (an attempt row with `status="failed"`), and
what `failed_rows()`/`retry_failed_run()` (below) query against.
Persisting the attempt is wrapped in its own try/except inside the worker,
so a DB write failure on the attempts table can never crash the qualify job
itself.

**Retrying a failure doesn't require re-qualifying the whole run.**
`qualify.failed_rows(conn, run_id)` returns exactly the domains whose most
recent attempt failed and haven't succeeded since; `qualify.retry_failed_run(conn,
run_id)` re-runs `qualify_run` scoped to just those domains
(`force=True, domains=[...]`), or is a zero-cost no-op (never calling the
LLM backend at all) when nothing has failed. Exposed as
`GET /runs/{run_id}/qualify/failed` and `POST /runs/{run_id}/qualify/retry-failed`,
and surfaced in the Dashboard as a banner on the Companies page ("N
qualifications failed — Retry failed") that only renders when the count is
non-zero.

**A single company's "Qualify with AI" button is scoped to just that
company.** `POST /runs/{run_id}/qualify` accepts an optional `domains` list;
when given, `pending_rows`/`qualify_run` restrict to exactly those domains
instead of every pending company in the requested tiers.

Two ways to trigger it:
- **Automatically for Tier A/B, capped** — part of
  `discover_mod.run_discovery` (`discover.py`'s `_auto_qualify_and_contact`).
  On the API/Dashboard path this runs with no flag needed, for every Tier A/B
  company up to `icp.yaml`'s `outreach.auto_qualify_top_n` (default 25,
  highest score first — see **Two ways to run it** at the bottom for exactly
  how this differs on the CLI). Since tier is a monotonic function of score,
  "every Tier A/B company, capped at N" and "the top N scored companies,
  filtered to Tier A/B" are the same selection — a Tier C/D company can never
  outrank a Tier B one, so nothing is skipped that shouldn't be.
- **By hand, any time, any scope** — `csg qualify` / the Qualify action in
  the UI / `POST /runs/{run_id}/qualify`, with its own `--tier`/`--force`
  controls, independent of what a given run's automatic cap covered.

Backend is chosen by whichever API key is set (Gemini tried first — it has a
free tier):
- `GEMINI_API_KEY` → Gemini (`gemini-3.6-flash`), serialized to 1 concurrent
  request (its free tier rate-limits aggressively).
- `ANTHROPIC_API_KEY` → Claude (`claude-opus-5`), up to 4 concurrent.

Retries transient failures (rate limits, 5xx) up to 4 times with backoff. The
CLI's standalone `csg qualify` and `--auto-qualify-top` ask for confirmation
before spending quota unless `--yes` is passed — the CLI's `csg discover`
never triggers qualify/contacts automatically on its own, only via that
explicit flag; the automatic-by-default behavior described above is specific
to `POST /discover`.

### Multi-key failover — [src/csg/discovery/gemini_keys.py](src/csg/discovery/gemini_keys.py)

Every Gemini call site (`_GeminiBackend` here, plus discovery and
enrichment below) reads `GEMINI_API_KEY`, `GEMINI_API_KEY_2`,
`GEMINI_API_KEY_3`, ... from `.env` via `GeminiKeyPool` and rotates to the
next key when the current one's **daily** quota is exhausted. A per-minute
rate limit is deliberately *not* a rotation trigger — that recovers in
seconds, so the existing retry/backoff handles it on the same key instead of
burning through the pool.

Getting the daily-vs-per-minute distinction right required matching Google's
actual error shape, not the documented one: `google.genai.errors.APIError`
exposes `.details` as the raw response body, but the SDK's own code reads it
two different ways in different places (`response_json.get(x)` *or*
`response_json.get("error", {}).get(x)`), and a genuine daily-quota 429
often carries no `QuotaFailure`/`violations` block at all — just a generic
`rpc.Help` link. An early version of this logic read "structured details
present, no PerDay violation" as proof of a per-minute limit and silently
never rotated on a real daily-exhaustion error. Fixed and covered by
[test_gemini_keys.py](tests/test_gemini_keys.py) against the exact error
shape captured from a live failed run.

**IPv4 pinning for broken IPv6 routes.** Some networks (especially
Windows/corporate environments) have a broken/blackholed IPv6 path to
Google's API domains — IPv6 routing exists (ICMP ping succeeds), but the TLS
handshake silently fails over that path. `httpx` and the stdlib resolver
prefer the AAAA (IPv6) record when both A and AAAA records exist, so every
Gemini call would fail with `[SSL: UNEXPECTED_EOF_WHILE_READING]` or
`ConnectTimeout` before ever reaching a usable connection. `make_gemini_client()`
(in [gemini_keys.py](src/csg/discovery/gemini_keys.py)) pins all outbound
Gemini API calls to IPv4 via `httpx.HTTPTransport(local_address="0.0.0.0")`,
bypassing the broken IPv6 route entirely. The same IPv4-pinned
`make_ipv4_httpx_clients()` transport is used by the Tavily+Groq fallback
(see below) for defense in depth — neither path is a single point of failure
for the other. Both connection-level timeouts (10s connect timeout, 30s read)
are set tight so a dead route fails fast rather than hanging. See
[test_gemini_keys.py](tests/test_gemini_keys.py) for the connection-error
classification that tells these apart from quota/parse errors in logs.

**Grounded search has its own quota**, separate from plain generation.
Discovery ([web_search.py](src/csg/discovery/web_search.py)) calls Gemini
with `tools: [{"google_search": {}}]` to find real companies, and that
tool's free-tier daily cap is much smaller than plain
`generate_content` — a key that answers a simple prompt fine can still be
fully out of grounded-search quota. `_generate()` also retries the same key
2x on transient network errors (connection drops, timeouts, TLS handshake
failures) before treating a query as failed, since those are unrelated to
quota and resolve on retry.

**Firmographic enrichment** ([discovery/research.py](src/csg/discovery/research.py))
retries a connection error once on the same Gemini key (2s backoff) before
giving up and falling back to Tavily+Groq — a single transient blip should
not immediately burn the fallback path. Then it falls back to Tavily+Groq
for *any* Gemini call failure, not only quota exhaustion — a non-quota
exception (network blip that outlasted the retry, an empty/malformed
grounded-search response, a transient 5xx, a safety-filter block, ...) stops
trying further keys (the failure isn't key-specific, so retrying the next key
would just repeat it) and falls straight through to the same Tavily+Groq
fallback the "all keys exhausted" path uses, rather than reporting the
company unenriched immediately. Only if the fallback also fails is the
company actually reported as `request_error`. This closed a real gap: earlier,
only quota-exhaustion triggered the fallback, so a single company hitting some
other transient Gemini error surfaced as "enrichment request failed" with no
retry via the alternate provider even though one was configured and working.
Logs now distinguish `"Gemini unreachable (connection/TLS error)"` from
`"all Gemini keys exhausted"` or `"Gemini request failed"` so connectivity
issues are diagnosable at a glance. See
[test_enrich_firmographics.py](tests/test_enrich_firmographics.py).

**The Tavily+Groq fallback itself now retries a 429**, matching
`GroqTavilyProvider`'s discovery-side retry (see below). Seen live as
"Enrichment skipped for 2 companies: enrichment request failed (2)" in a run
where Gemini was fully daily-quota-exhausted (confirmed by every other
company in that run coming back tagged `groq_tavily_search`) -- so both
failing companies hit the fallback with `ENRICH_CONCURRENCY` other companies
in flight at once, and Tavily's free tier is metered per-minute, not just
per-month. A transient 429 from either Tavily or Groq used to fail the
company on the spot; `_enrich_via_groq_tavily` now retries both calls with
the same `Retry-After`-aware backoff `GroqTavilyProvider._groq_parse` uses,
so a rate limit hit under concurrency is diagnosable-and-recoverable instead
of silently burning one of the two providers this fallback exists to use.

**Every enrichment/qualify failure now reports its real cause, not a generic
bucket.** `usage_tracker.classify_error()` (see below) is the single place
that turns any provider exception into one of `quota_exhausted`,
`rate_limited`, `connection_error`, `timeout`, `parse_error`, or the
catch-all `request_error` -- both `enrich_company_firmographics` /
`_enrich_via_groq_tavily` (discovery/research.py) and `discover.py`'s own
`except Exception` safety nets around them use it, so a run's
`enrich_failures` tally (what the frontend renders as "Enrichment skipped
for N companies: ...") and `qualify.py`'s per-domain `error_details` both
name the actual failure instead of collapsing everything that isn't a JSON
parse error into "enrichment request failed" -- which is exactly the vague
message that made the original 429-under-fallback bug (above) hard to
diagnose from the UI alone.

**API usage/quota tracking** ([usage_tracker.py](src/csg/usage_tracker.py))
instruments every outbound Gemini/Groq/Tavily/Hunter.io call (discovery
search, enrichment, its Tavily+Groq fallback, qualify, and contacts.py's one
Hunter Domain Search call site) with a `track()` context manager that
records request count, success/failure, status code, `error_kind` (via the
same `classify_error` above), retry count, latency, and token usage (from
Gemini's `usage_metadata` / Groq's OpenAI-compatible `usage` object; Tavily
and Hunter have none, both are search/lookup APIs, not token-metered) to a
new `api_usage` table. Hunter's 429 is always tagged `quota_exhausted`, never
`rate_limited` -- its free tier is a hard 25/month, not a per-minute window,
so a 429 means the month's quota is gone, matching `contacts.discover_run`'s
own "stop immediately" behavior on that error rather than implying a retry
would help. Purely observational and best-effort: `_record_async` writes on
a daemon thread and swallows every exception, so a DB outage or a slow write
can never add latency to or fail an actual research/qualify/contacts call --
see [test_usage_tracker.py](tests/test_usage_tracker.py) for the "must never
raise" guarantee. `GET /settings/usage` aggregates it into per-provider
totals plus a daily time series for the Settings > Usage & Quota tab. On a
database created before this feature, the table doesn't exist yet -- the
endpoint catches that and returns all-zero data rather than a 500; run
[migrations/002_api_usage.sql](migrations/002_api_usage.sql) once to start
collecting.

The endpoint takes an optional `hours` (window back from now) or `since` (an
explicit ISO timestamp, used verbatim) -- neither means **All Time**, no
lower bound at all (`db.api_usage_all`), which is the tab's default. The
Usage & Quota tab exposes exactly four filters over this: **Latest Research
Run** (the frontend passes that run's own `started_at` from its already-
loaded runs list as `since` -- this endpoint knows nothing about runs
itself), **Last 7 Days** / **Last 30 Days** (`hours=24*7` / `24*30`), and
**All Time** (no param). `since` wins if both are somehow passed. Every stat
card, the requests-over-time chart, and the per-provider fact grid all read
from the same one filtered response, so they always agree with each other.

## Contacts — [src/csg/contacts.py](src/csg/contacts.py) (costs quota)

Independent of qualify: looks up HR/recruiting contacts for scored companies
via Hunter.io's Domain Search API. Free tier is a hard 25 domain searches
**per month** total, so `contacts.discover_run()` is serial by design, stops
immediately on the first quota error rather than burning the rest of a batch,
and skips domains already searched unless `--force`.

It is **never** invoked from inside the scoring pipeline itself.
`pipeline.run()` processes companies concurrently under `asyncio.gather`, so
calling Hunter from there would fire parallel requests with no shared quota
accounting and could spend a month's allowance in a single run. Instead, like
qualify above, it runs either automatically for Tier A/B (after the pipeline
has fully finished, serially, same cap and same "automatic on the API path,
opt-in on the CLI" split as qualify) or by hand for any scope via
`csg contacts` / `POST /runs/{run_id}/contacts`.

**Email is required, LinkedIn is a bonus.** Hunter's `_parse_emails` discards
any returned entry without a verified email address at parse time, so a
`contacts` row is never saved without one — this is enforced in
`contacts.py`, not just assumed downstream. `linkedin_url` is carried through
per-contact when Hunter finds it, but never required.

A company becomes **outreach-ready** (`Prospect.compute_outreach_flags()` in
[models.py](src/csg/models.py)) as soon as it has one contact with an email;
`has_linkedin_contact` is recorded alongside as an informational bonus but
never gates readiness. Both flags are computed from the same `contacts` list
in one place and flow through to every consumer — the JSONL handoff (what the
[Outreach Agent](../Outreach_Agent) reads when hand-off is triggered by
hand), the CSV export, and the `/runs/{run_id}/companies/{domain}` API
response — so they can never drift out of sync with each other. See
**Outreach hand-off** below for how this connects to the Outreach Agent
automatically, not just via a manual export.

## Storage — Supabase (Postgres via PostgREST)

All state lives in one Supabase project, accessed through a thin PostgREST
client ([src/csg/db.py](src/csg/db.py)) using the service-role key — never
from a browser. Every table but `runs` has `domain` as (or referencing)
its key, with `ON DELETE CASCADE` declared in [schema.sql](schema.sql) and
applied to the live database (see
[migration_add_fk_cascades.sql](scratchpad/migration_add_fk_cascades.sql)),
so deleting a `companies` row deletes everything that referenced it (job
postings, scores, qualifications, saved entries, contacts, suppression, and
`run_company_matches` rows) in one shot. Tables: `runs`, `companies`,
`discovery_sources`, `job_postings`, `scores`, `suppression`,
`qualifications`, `qualification_attempts`, `saved_companies`, `contacts`,
`api_usage`, `run_company_matches`. `companies.domain` is always the
canonical bare host (a `NOT VALID` CHECK from migration 005 enforces this for
new writes).

`runs` additionally carries an optional `label` column (see
[migration_add_run_label.sql](scratchpad/migration_add_run_label.sql)) — a
human-given name like "CSG research" set when a run is started, used
everywhere a run needs to be shown or picked in the UI instead of its raw
timestamp id.

Because Supabase is accessed only over its REST API (PostgREST) — not a
direct Postgres connection — schema changes need a hand-run `ALTER TABLE`
migration in the Supabase SQL editor; nothing in this codebase can run DDL on
its own. See the README's "Applying schema migrations" section.

Every Supabase call is a real network round trip (~0.4s each in practice).
`get_scores`/`all_scores`/`all_contacts`/`list_suppression` already batch
their per-row lookups into one `domain=in.(...)` query; `run_companies`'s
state/keyword filters and `list_saved` used to do one lookup per row instead
(an N+1 pattern) and have since been fixed to use the same batching
(`db.companies_by_domain()`, `db.postings_by_domain()`). Watch for the same
pattern (`for row in rows: db.get_x(conn, row["domain"])`) if you add a new
endpoint that enriches a list of scored companies.

## API routing — `/runs/{run_id}/...`

Every route that can be scoped to one research run is nested under
`/runs/{run_id}/...` in [src/csg/api.py](src/csg/api.py) — companies,
saved lists, contacts, suppression, analytics, and the qualify/contacts
actions. `run_id` is either a real run's id or the literal string `all`.

`saved_companies`, `contacts`, and `suppression` have no `run_id` column of
their own by design (see the schema comments — a save, a contact, or a
suppression decision outlives the run that surfaced it). For these, "scoped
to run X" means "domains that were scored in run X," computed by intersecting
with `db.get_scores(conn, run_id)` — see `_domains_scored_in()` in api.py.
`run_id=all` skips that filter entirely and returns everything.

For companies specifically, `run_id=all` needs one row per domain rather
than one row per (domain, run) pair — `db.latest_scores_per_domain()`
collapses `all_scores()` down to each domain's most recent score before the
usual tier/state/industry filters apply.

## Two ways to run it

- **CLI** ([src/csg/cli.py](src/csg/cli.py)) — `csg discover [region]`,
  `csg report`, `csg show <domain>`,
  `csg qualify`, `csg contacts`, `csg export`, `csg suppress <domain>`,
  `csg dedupe-report` (read-only: lists stored companies that look like the
  same company by normalized domain or name; changes nothing).
  Scriptable, no server needed.
- **Web app** — [src/csg/api.py](src/csg/api.py) (FastAPI) serves the same
  pipeline over HTTP + a WebSocket for live run progress
  (`/jobs/{job_id}/ws`), consumed by the React frontend that lives in the
  shared [Dashboard](../Dashboard) app, not in this repo (Runs, Companies
  table with tier/score/state/industry filters, Company Detail, Analytics,
  Saved Lists, Contacts, Settings pages, plus Excel
  (`GET /runs/{run_id}/export/excel`, companies + contacts) and PDF-reports-
  in-a-ZIP (`GET /runs/{run_id}/export/pdf`) downloads — read-only, built from
  stored data, never triggering a new run). A Research selector at the top of
  every run-scoped page lets you switch between named runs or "All
  research." See the [README](README.md#running-it) for how to run both
  processes together.

Both paths call the same orchestration code —
[discover.run_discovery()](src/csg/discover.py) for autonomous runs,
[pipeline.run()](src/csg/pipeline.py) for manual ones — so the CLI and the API
can never drift apart, and a run started from either shows up in the other.
Both accept a `label` (`--label` on the CLI, "Name this research" in the UI);
without one, a run is identified by its timestamp run_id.

**Where they deliberately behave differently**: `run_discovery()` takes an
`auto_pipeline` flag (default `True`). The API route (`api.py`'s
`_run_discover_job`) never overrides it, so every Dashboard-started run gets
the automatic Tier A/B qualify + contacts + Outreach hand-off described
above, no flag needed. The CLI's `discover` command explicitly passes
`auto_pipeline=False` and instead exposes `--auto-qualify-top N` (with its
own cost confirmation) as the only way to trigger that same work — see
`cli.py`'s `discover()` docstring and its call to `run_discovery`. This is
the one place CLI and API intentionally diverge in default behavior, not an
inconsistency to fix.

## Suppression and saved lists

- **Suppression** (`csg suppress <domain>`, or the Settings page in the UI)
  marks a domain as never-process (existing client, do-not-contact, open
  deal) — checked before ATS detection on every run, so suppressed domains
  get a `suppressed` status and are skipped entirely, at zero HTTP cost.
  `suppression.domain` has an FK into `companies`, so suppressing a domain
  that was never seeded/scored (the common case when adding one by hand in
  Settings) has `add_suppression()` ([src/csg/db.py](src/csg/db.py)) stub a
  minimal `companies` row first (`status: "suppressed"`, no other fields) —
  it only does this when no row for that domain already exists, so it never
  overwrites real pipeline data for a domain that's been through a run.
- **Saved companies** are a human judgement call made in the UI, keyed by
  domain only (not run_id) — a save outlives the run that surfaced it and
  stays valid across re-scores, recording which run prompted it for
  provenance only.

## Export — [src/csg/export.py](src/csg/export.py)

`csg export` (also run automatically at the end of `csg discover`)
writes the current run's scored companies to `data/exports/` as timestamped
CSV and JSONL files. Both are produced from the same rows but serve different
readers:

- **CSV** — flat and spreadsheet-ready. Provenance envelopes are unwrapped to
  their value (`$25M`, not a JSON blob), one column per scoring dimension,
  plus a per-region breakdown of delivery fit.
- **JSONL** — the `Prospect` **v2 handoff contract** (`models.Prospect`),
  tiers A–C by default. This is what a manual Outreach import via CSV/JSONL
  would read; the automatic hand-off (see **Outreach hand-off** below) goes
  through Outreach's HTTP API instead and does not read this file, but the
  shape is the same idea: region, industry, description, headcount/revenue/
  funding **with provenance intact** (so a message citing a revenue figure
  knows whether it was reported or estimated), the delivery-fit breakdown,
  and per-req `practice_area` / `delivery_fit`.

`Prospect` is a frozen boundary: it reads this module and the
`scores`/`companies` tables, never `pipeline`, `scoring`, or `ats` — the
[Outreach Agent](../Outreach_Agent) is a fully separate service/codebase
with its own Supabase project, connected only over HTTP (this module's
export files for a manual/CSV workflow, or `outreach_client.py` for the
automatic one below) — never a shared database.

## 5. Outreach hand-off — [src/csg/outreach_client.py](src/csg/outreach_client.py)

The last automatic step, and the only place this codebase makes an HTTP call
*out* to another service. Fires from `discover.py`'s `_handoff_to_outreach`,
right after qualify + contacts finish for a run's Tier A/B companies (step 3
+ Contacts above), and only if `OUTREACH_API_BASE` is set. Two calls, both
against routes the Outreach Agent's own frontend already uses — this adds no
new surface area to Outreach's API:

1. **`POST /import`** on Outreach, with `run_id` + `tiers=["A","B"]` — a
   background job there; `OutreachClient.import_prospects()` polls
   `GET /jobs/{job_id}` to completion (`poll_interval=1s`, `timeout=120s`
   defaults) rather than firing-and-forgetting, since the campaign step needs
   the import to have actually finished first.
2. **`POST /campaigns`** on Outreach, once import reports at least one
   prospect imported (skipped entirely, not attempted, if it imported zero —
   there is nothing to draft a campaign from).

A third route, **`DELETE /research/runs/{run_id}`**, is the teardown
counterpart to the two above — called from `api.py`'s `delete_run` (not from
this hand-off) when a run is deleted, so Outreach doesn't keep a prospect or
campaign for a run that no longer exists. See **Deleting a run** below.

**This never sends anything** — it stops at a drafted campaign. What
"drafted" means, and which items land ready-to-send vs. held for review, is
entirely Outreach's own `create_campaign_from_prospects` logic (see
[Outreach_Agent/WORKFLOW.md](../Outreach_Agent/WORKFLOW.md#2-create-campaign--ingestpy)) —
this codebase has no opinion on it beyond triggering the call.

**A Tier A/B company with no contact never reaches Outreach at all.** Both
Outreach calls report back which tier-matching companies they left out for
lacking a contact email (`excluded_no_contact`, a list of `{"domain", "name",
"tier"}`); `_handoff_to_outreach` merges the two lists (de-duplicated by
domain, since the same company could in principle appear in both) onto
`stats.outreach["excluded_no_contact"]`. So a run summary can say precisely
"No contacts found for Acme Co, so it was not added to prospects/campaign"
for each one, rather than a person having to notice the campaign came up
smaller than the Tier A/B count implied. This is a hard exclusion, not a
demotion — an excluded company keeps its real tier/score in Research's own
data and can still be picked up by a later `csg contacts` re-run once Hunter
finds someone.

**Failure handling.** `OutreachClient`'s errors (`OutreachClientError` —
unreachable host, non-2xx response, a job that errors or times out while
polling) are caught in `_handoff_to_outreach` and recorded on
`stats.outreach = {"error": ...}`, never raised. By the time this step runs,
discovery, scoring, qualify, and contacts have all already succeeded and
written real data — an unreachable or misbehaving Outreach Agent must not
retroactively make the research run look failed. `client.close()` always
runs (in a `finally`), even on an exception mid-hand-off.

**Not reachable from `csg resume`** (see **Resuming** below) — a resume only
re-covers stalled companies' enrichment/scoring, it does not re-run the
qualify/contacts/hand-off step even if those stalled companies turn out to
land in Tier A/B after resuming. Run `csg qualify`/`csg contacts` by hand
afterwards, or start a fresh run, if you want that.

See [test_discover_handoff.py](tests/test_discover_handoff.py) (selection/cap
logic and the hand-off's call sequence) and
[test_outreach_client.py](tests/test_outreach_client.py) (the HTTP client
itself, against a mocked transport) for what's covered.

## Resuming — [discover.resume_stalled()](src/csg/discover.py)

A company can end up "stalled": scored, but with no ATS board found and no
firmographics — the signature of every provider (Gemini, and the Tavily+Groq
fallback for both discovery and enrichment) being exhausted or unreachable
for it specifically, not a company that was genuinely researched and just
came up thin (see `db.stalled_companies`). This can happen mid-run if a key
runs out of daily quota partway through a large company list.

Rather than a whole re-run, `POST /runs/{run_id}/resume` (the Settings page's
**Resume research (N)** action, shown only when a run actually has stalled
companies) re-processes just those domains: re-enrichment (skipped for any
company that already succeeded — same `_needs_enrichment` check a normal run
uses) followed by a re-run through ATS detection + scoring, under the same
`run_id`. It does **not** re-run discovery — the companies are already
known, and re-searching the region would just risk a second, possibly
different, set of search results instead of finishing the run that already
started.

A resume is only meaningful, and only offered, when the run genuinely has
stalled companies — `resume_stalled()` raises if none of the given domains
still exist (e.g. all deleted since the run finished) rather than silently
running the pipeline over nothing.

## Deleting a run

`DELETE /runs/{run_id}` (Settings page) tears down everything exclusively
scoped to that run (its `run_company_matches` rows are removed first and never
keep a company alive — a company with no remaining scores is deleted and can
be rediscovered as new) — companies, job postings, discovery sources, contacts,
saved entries, and suppression rows, via `ON DELETE CASCADE` in
[schema.sql](schema.sql) once `db.delete_run()` removes the `scores`/
`qualifications` rows. A domain also scored by another surviving run is left
completely untouched, so deleting run A can never break run B's Companies
tab.

**Also cascades to the Outreach Agent.** If this run was ever handed off (see
**Outreach hand-off** above), it has its own copy of that run's prospects and
possibly a campaign built from them — those don't disappear just because
Research's own tables were cleaned up. After `db.delete_run()` succeeds,
`api._delete_from_outreach()` calls `OutreachClient.delete_research_run()`,
Outreach's own `DELETE /research/runs/{run_id}`, which removes its
`campaigns` (and their items/events) and cached `prospects` for this
`run_id` (see [Outreach_Agent/WORKFLOW.md](../Outreach_Agent/WORKFLOW.md)).
Same failure-handling rule as the hand-off itself: this run's own data is
already gone by the time the call fires, so an unreachable Outreach Agent
(or `OUTREACH_API_BASE` unset) does not fail or roll back the delete — it's
reported as `{"outreach": {"error": ...}}` in the response instead of
raised. See [test_api_delete_run.py](tests/test_api_delete_run.py).

The Dashboard's delete handler (`Settings.tsx`) invalidates both sides'
caches in one go — Research's `companies`/`contacts`/`analytics`/
`suppression` and Outreach's `prospects`/`campaigns`/`campaign-items`/
`outreach-analytics` — so every open page reflects the deletion immediately,
with no manual refresh needed.
