"""Message personalization: draft a subject + body for one outreach item from
its cached prospect data.

Same LLM-backend convention as the Research Agent's qualify.py, extended with
a third tier: Gemini tried first if GEMINI_API_KEY is set (rotating through
GEMINI_API_KEY_2, _3, ... on a daily-quota 429 -- see gemini_keys.py), then
Anthropic, then Groq (plain text, no search) as a last LLM resort before the
caller's own non-LLM fallback. See _call_llm.

`pain_summary`/`recommended_angle` come from the Research Agent's *optional*
`csg qualify` step, which runs separately from scoring/discovery and can
simply not have gotten to a domain yet (or hit a rate limit/error on it) even
though that domain scored A/B and has plenty of other research on file --
industry, description, region, open reqs, contacts, score explanation. That
data is real and LLM-usable on its own, so it is NOT treated as "not enough
information": draft_message uses an LLM to personalize from it directly (see
_render_research_prompt) whenever qualify data is missing but other research
fields are present. The static, non-LLM `_fallback_template` is now a
last-resort path, used only when a prospect has neither qualify data nor any
other usable research field, or every LLM/search method fails -- so
personalization never blocks a send on missing research data or a missing
LLM key, but also never sends a well-researched company to manual review
just because one optional step hasn't finished.

For the redraft "go find more information" tier specifically (see
draft_message's use_web_search param), three independent search-capable
backends are tried in order before giving up:
  1. Gemini + its built-in google_search grounding tool.
  2. Groq + Tavily -- same split the Research Agent's discovery/
     groq_tavily.py uses: Tavily runs the actual web search (compact
     snippets), Groq (a plain non-agentic model) turns those snippets into
     the email. Two cheap calls instead of one search-augmented one, and a
     fully independent provider pair from Gemini/Anthropic, so it survives
     either of those being down/exhausted/unconfigured.
  3. Anthropic + its server-side web_search tool.
Each is skipped if its required key(s) aren't set; only if all three either
skip or fail does drafting fall through to the static template.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config import PROJECT_ROOT, Config
from .gemini_keys import GeminiKeyPool, is_daily_quota_exhausted
from .models import ImportedProspect

_PROMPT_PATH = PROJECT_ROOT / "prompts" / "outreach_email.md"
_RESEARCH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "outreach_email_from_research.md"
_WEB_SEARCH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "outreach_email_web_search.md"
_REVISE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "outreach_email_revise.md"

_BODY_UNAVAILABLE = "BODY_UNAVAILABLE"

_GROQ_MODEL = "openai/gpt-oss-120b"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_TAVILY_URL = "https://api.tavily.com/search"

_DEFAULT_SENDER_SIGNATURE = (
    "Affan Jan\nCareer Source Group LLC\nAlpharetta, Georgia\nhttps://careersourcegroup.com/"
)


@dataclass
class DraftedMessage:
    subject: str
    body: str
    # 'pain_summary' | 'recommended_angle' -- LLM draft from Research's qualify output.
    # 'research_data' -- LLM draft from other cached research fields (qualify hasn't run/finished).
    # 'web_search' -- LLM draft from a live web search (redraft only, see draft_message's
    #   use_web_search) when nothing else -- cached or otherwise -- was enough.
    # 'template_only' -- static fallback, used only once every other tier above has failed. The
    #   only source that keeps a needs_review item stuck (see ingest.create_campaign_from_prospects).
    # 'revision' -- user-instructed edit of an existing draft.
    personalized_from: str


def _render_open_reqs(prospect: ImportedProspect, max_reqs: int) -> str:
    """Render the open-req list for an LLM prompt. When there are none, the
    placeholder text must not read as "confirmed not hiring" unless it
    actually is -- `hiring_signal == "unknown"` means Research never
    successfully read this company's job board/careers page at all, so an
    empty list here is an absence of data, not evidence of no hiring need
    (see Research_Agent's models.HiringSignal). Every prompt that includes
    this (outreach_email.md, outreach_email_from_research.md,
    outreach_email_web_search.md) instructs the model accordingly -- this
    placeholder is what makes that distinction visible to it in the first
    place."""
    lines = []
    for req in (prospect.open_reqs or [])[:max_reqs]:
        title = req.get("title") or "Open role"
        location = req.get("location")
        lines.append(f"- {title}" + (f" ({location})" if location else ""))
    if lines:
        return "\n".join(lines)
    if prospect.hiring_signal == "unknown":
        return "- (no job posting data available for this company -- do not assume they are not hiring)"
    return "- (no open roles listed)"


def _fallback_template(prospect: ImportedProspect, signature: str) -> DraftedMessage:
    """Last-resort, non-LLM fallback: used when the prospect has no research
    data at all to personalize from (see _has_research_signal), or every LLM
    call/parse attempt failed despite data being available (no API key
    configured, network/API error, or an unparseable response). Still
    tailored to whatever we do know about the company (name, industry, open
    roles) rather than a one-size-fits-all pitch -- but this is the one path
    that leaves an item in needs_review (see
    ingest.create_campaign_from_prospects)."""
    name = prospect.contact_name or "there"
    company = prospect.name or prospect.research_domain
    industry = prospect.industry or "your industry"

    role_titles = [r.get("title") for r in (prospect.open_reqs or []) if r.get("title")][:2]
    if role_titles:
        hook = f"I noticed {company} has open roles like {', '.join(role_titles)}"
    else:
        hook = f"I came across {company} and your work in {industry}"

    subject = f"Hiring support for {company}"
    body = (
        f"Hi {name},\n\n"
        f"{hook}. CSG works with {industry} companies to fill technical roles "
        f"faster, matching whichever engagement model fits best -- direct "
        f"staffing, an embedded delivery team, or recruiting support -- rather "
        f"than a one-size-fits-all approach.\n\n"
        f"Worth a quick 15-minute call to see if there's a fit for {company}?\n\n"
        f"Best,\n{signature}"
    )
    return DraftedMessage(subject=subject, body=body, personalized_from="template_only")


def _parse_llm_output(text: str) -> Optional[tuple[str, str]]:
    if _BODY_UNAVAILABLE in text:
        # The web-search prompt's explicit "nothing usable found" signal (see
        # outreach_email_web_search.md) -- treat exactly like a failed call
        # rather than parsing it as SUBJECT/BODY.
        return None
    subject_match = re.search(r"SUBJECT:\s*(.+)", text)
    body_match = re.search(r"BODY:\s*(.+)", text, re.S)
    if not subject_match or not body_match:
        return None
    return subject_match.group(1).strip(), body_match.group(1).strip()


def _append_learning_context(prompt: str, learning_context: Optional[str]) -> str:
    """Appends the Feedback Learning Agent's structured guidance blurb (see
    learning/context.py) as an extra instruction block, inserted just before
    the final "Return exactly two fields" footer every prompt template ends
    with. A no-op when learning_context is None (no active insight yet, or
    the learning layer is unavailable) -- prompt output is then byte-for-byte
    identical to before this parameter existed. Never hard-codes a specific
    learned sentence; the blurb itself is generated fresh from live insight
    rows -- see rl_prompt.md section 6."""
    if not learning_context:
        return prompt
    return (
        f"{prompt}\n\n"
        f"{learning_context}\n"
        "Apply this guidance only where it fits naturally -- do not force it, "
        "and do not mention that this guidance exists."
    )


def _render_prompt(prospect: ImportedProspect, max_reqs: int, signature: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{company_name}}", prospect.name or prospect.research_domain)
        .replace("{{domain}}", prospect.research_domain)
        .replace("{{industry}}", prospect.industry or "unknown")
        .replace("{{contact_name}}", prospect.contact_name or "there")
        .replace("{{contact_position}}", prospect.contact_position or "")
        .replace("{{pain_summary}}", prospect.pain_summary or "not available")
        .replace("{{recommended_angle}}", prospect.recommended_angle or "not available")
        .replace("{{open_reqs}}", _render_open_reqs(prospect, max_reqs))
        .replace("{{signature}}", signature)
    )


def _fit_explanation(prospect: ImportedProspect) -> str:
    """Human-readable reason this company was shortlisted, pulled from the
    Research Agent's own scoring explanation (raw_prospect["score"]
    ["explanation"], a list[str] -- see Research_Agent's ScoreResult) when
    cached, falling back to the bare tier/score. This is deterministic
    scoring output, not an LLM guess, so it's always safe to hand to the
    prompt as grounding."""
    score_row = (prospect.raw_prospect or {}).get("score") or {}
    reasons = score_row.get("explanation")
    if reasons:
        return "\n".join(f"- {r}" for r in reasons)
    if prospect.tier or prospect.score:
        return f"Scored tier {prospect.tier or 'unknown'} ({prospect.score if prospect.score is not None else 'n/a'}/100)."
    return "not available"


def _has_research_signal(prospect: ImportedProspect) -> bool:
    """True when there is at least one real, LLM-usable research field to
    personalize from, even without the optional qualify step's
    pain_summary/recommended_angle. Mirrors what _render_research_prompt
    actually fills in -- if none of these are present there is nothing for
    the LLM to ground a personalized email in, and the static fallback
    template is the honest choice."""
    return bool(
        prospect.description
        or prospect.industry
        or prospect.open_reqs
        or (prospect.raw_prospect or {}).get("score", {}).get("explanation")
    )


def _render_research_prompt(prospect: ImportedProspect, max_reqs: int, signature: str) -> str:
    """Prompt used when qualify hasn't produced a pain_summary/recommended_angle
    for this domain yet, but other research data is available -- see module
    docstring. Lets the LLM infer the angle itself instead of leaving the
    email unpersonalized."""
    template = _RESEARCH_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{company_name}}", prospect.name or prospect.research_domain)
        .replace("{{domain}}", prospect.research_domain)
        .replace("{{industry}}", prospect.industry or "unknown")
        .replace("{{region}}", prospect.region or "unknown")
        .replace("{{contact_name}}", prospect.contact_name or "there")
        .replace("{{contact_position}}", prospect.contact_position or "")
        .replace("{{description}}", prospect.description or "not available")
        .replace("{{fit_explanation}}", _fit_explanation(prospect))
        .replace("{{open_reqs}}", _render_open_reqs(prospect, max_reqs))
        .replace("{{signature}}", signature)
    )


def _render_web_search_prompt(prospect: ImportedProspect, max_reqs: int, signature: str) -> str:
    """Prompt used only by the redraft "go find more information" tier: no
    qualify data AND no other cached research field, so the model is told to
    use its web-search tool to look the company up rather than guess -- see
    _call_llm_with_search and draft_message's use_web_search param."""
    template = _WEB_SEARCH_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{company_name}}", prospect.name or prospect.research_domain)
        .replace("{{domain}}", prospect.research_domain)
        .replace("{{industry}}", prospect.industry or "not available")
        .replace("{{region}}", prospect.region or "not available")
        .replace("{{description}}", prospect.description or "not available")
        .replace("{{contact_name}}", prospect.contact_name or "there")
        .replace("{{contact_position}}", prospect.contact_position or "")
        .replace("{{open_reqs}}", _render_open_reqs(prospect, max_reqs))
        .replace("{{signature}}", signature)
    )


def _render_revise_prompt(
    prospect: ImportedProspect, current_subject: str, current_body: str,
    instruction: str, signature: str,
) -> str:
    template = _REVISE_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{company_name}}", prospect.name or prospect.research_domain)
        .replace("{{domain}}", prospect.research_domain)
        .replace("{{contact_name}}", prospect.contact_name or "there")
        .replace("{{contact_position}}", prospect.contact_position or "")
        .replace("{{current_subject}}", current_subject)
        .replace("{{current_body}}", current_body)
        .replace("{{instruction}}", instruction)
        .replace("{{signature}}", signature)
    )


def _call_gemini(prompt: str) -> Optional[str]:
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return resp.text


def _call_gemini_with_key_pool(prompt: str) -> Optional[str]:
    """Try every configured Gemini key (GEMINI_API_KEY, GEMINI_API_KEY_2,
    ...) in order, advancing only when a key's *daily* quota is exhausted --
    see gemini_keys.py's module docstring. Re-raises anything else (a real
    error, or a per-minute rate limit worth backing off on rather than
    burning through the pool) so _call_llm's own except/fallback handles it
    the same way a single-key failure always has.

    Returns None only if every configured key's quota is exhausted; raises
    (rather than silently returning None) for the first non-quota failure,
    same contract as _call_gemini."""
    from google import genai

    pool = GeminiKeyPool.from_env()
    last_quota_exc: Optional[Exception] = None
    for attempt in pool.attempts():
        try:
            client = genai.Client(api_key=attempt.key)
            resp = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
            return resp.text
        except Exception as exc:
            if not is_daily_quota_exhausted(exc):
                raise
            last_quota_exc = exc
            continue  # this key's daily quota is gone -- try the next one

    if last_quota_exc is not None:
        return None  # every configured key exhausted -- caller falls through to Anthropic
    return None  # no Gemini key at all


def _call_anthropic(prompt: str) -> Optional[str]:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-opus-5", max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if hasattr(block, "text"))


def _call_gemini_with_search(prompt: str) -> Optional[str]:
    """Same as _call_gemini but with Gemini's built-in google_search grounding
    tool enabled -- same {"tools": [{"google_search": {}}]} config the
    Research Agent's discovery/web_search.py uses for grounded company
    lookup. Lets the model look the company up instead of answering from
    training data alone."""
    from google import genai
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model="gemini-3.6-flash", contents=prompt,
        config={"tools": [{"google_search": {}}]},
    )
    return resp.text


def _call_anthropic_with_search(prompt: str) -> Optional[str]:
    """Same as _call_anthropic but with Anthropic's server-side web_search
    tool enabled so the model can look the company up before drafting."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-opus-5", max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if hasattr(block, "text"))


def _tavily_search(company_query: str) -> list[dict[str, Any]]:
    """Raw web search via Tavily -- compact snippets, not full pages. Same
    endpoint/shape the Research Agent's discovery/groq_tavily.py uses for
    company discovery. Returns [] (not raises) on any error/empty result, so
    _call_groq_with_tavily can cleanly fall through to "nothing found"
    instead of the caller needing its own try/except around this."""
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        return []
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(_TAVILY_URL, json={
                "api_key": tavily_key,
                "query": company_query,
                "max_results": 8,
                "search_depth": "basic",
            })
            r.raise_for_status()
            return r.json().get("results", [])
    except Exception:
        return []


def _call_groq_plain(prompt: str) -> Optional[str]:
    """Plain text completion via Groq, no Tavily/search attached -- a third
    independent LLM tier for _call_llm, alongside Gemini and Anthropic.
    Unlike _call_groq_with_tavily (used only by the redraft "go find more
    information" tier, where a live web search is the point), this is for
    prompts that are already fully self-contained -- a conversation reply
    answering a thread the caller has already assembled, for instance --
    where no search is needed or wanted, just a third provider to try when
    Gemini and Anthropic both fail (quota exhausted, no credit, an outage).
    Requires GROQ_API_KEY; returns None (the caller's next/last fallback)
    if it's unset or the call fails for any reason."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        return None

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(_GROQ_URL, headers={
                "Authorization": f"Bearer {groq_key}",
                "Content-Type": "application/json",
            }, json={
                "model": _GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
            })
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def _call_groq_with_tavily(prompt: str, prospect: ImportedProspect) -> Optional[str]:
    """Search-augmented drafting via Tavily + Groq, independent of both
    Gemini and Anthropic -- see module docstring. Tavily searches for the
    company by name/domain; Groq (a plain, non-agentic model -- no built-in
    search of its own) is handed the same prompt used for the other
    search-capable backends, plus Tavily's snippets appended as its only
    source of "search results", and asked to draft from them under the same
    rules (ground only in what's given, BODY_UNAVAILABLE if nothing usable).
    Requires both GROQ_API_KEY and TAVILY_API_KEY; returns None (falls
    through to the next backend) if either is missing or any call fails."""
    groq_key = os.environ.get("GROQ_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not groq_key or not tavily_key:
        return None

    company = prospect.name or prospect.research_domain
    query = f"{company} {prospect.research_domain} company"
    results = _tavily_search(query)
    if not results:
        return _BODY_UNAVAILABLE  # nothing found -- treated as a failed draft, not an error

    snippets = "\n\n".join(
        f"[{r.get('title', '')}]({r.get('url', '')})\n{r.get('content', '')[:2000]}"
        for r in results
    )
    instruction = (
        f"{prompt}\n\n"
        f"Web search results for {company} (from Tavily -- this IS your search; "
        f"do not say you cannot search, just use these results):\n\n{snippets}"
    )
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(_GROQ_URL, headers={
                "Authorization": f"Bearer {groq_key}",
                "Content-Type": "application/json",
            }, json={
                "model": _GROQ_MODEL,
                "messages": [{"role": "user", "content": instruction}],
                "temperature": 0.4,
            })
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def _sender_signature(cfg: Config) -> str:
    """The full sign-off block every prompt's {{sender_name}} placeholder is
    filled with -- name, company, location, site. Not just the bare name."""
    sig = cfg.personalization.get("sender_signature")
    return str(sig).strip("\n") if sig else _DEFAULT_SENDER_SIGNATURE


def _call_llm(prompt: str) -> Optional[str]:
    """Three independent tiers, in order -- a genuine fallback chain, not
    just "whichever key happens to be set":

      1. Every configured Gemini key (GEMINI_API_KEY, GEMINI_API_KEY_2, ...)
         -- see gemini_keys.py for the key-pool/quota-rotation logic.
      2. Anthropic.
      3. Groq (plain text completion, no search attached).

    Each tier can fail for reasons that have nothing to do with the prompt
    itself -- a free-tier quota exhausted, an account out of credit, a
    transient outage -- while a later tier is configured and healthy.
    Previously a Gemini failure returned None immediately without ever
    trying anything else, silently dropping to the caller's non-LLM
    fallback (a static template, for a cold-outreach draft; an error
    surfaced to the user, for a conversation reply) even when a working
    provider was sitting right there unused. Groq specifically matters here
    because it is billed and rate-limited completely independently of
    Google/Anthropic, so it is the one tier likely to still be up when both
    of the others are simultaneously exhausted (observed during this
    project's own testing)."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            text = _call_gemini_with_key_pool(prompt)
            if text:
                return text
        except Exception:
            pass  # try Anthropic next, if configured, rather than giving up here

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            text = _call_anthropic(prompt)
            if text:
                return text
        except Exception:
            pass  # try Groq next, if configured, rather than giving up here

    if os.environ.get("GROQ_API_KEY"):
        return _call_groq_plain(prompt)

    return None


def _call_llm_with_search(prompt: str, prospect: ImportedProspect) -> Optional[tuple[str, str]]:
    """Three independent search-capable backends, tried in order, each
    skipped (not just failed-and-stopped) if its required key(s) are absent:

      1. Gemini + google_search grounding.
      2. Groq + Tavily (see _call_groq_with_tavily) -- fully independent of
         Gemini/Anthropic, so this still gets tried even if Gemini's key is
         missing/exhausted or Anthropic's is missing.
      3. Anthropic + web_search tool.

    Used only for the redraft "go find more information" tier (see
    draft_message's use_web_search param and api.redraft_item/
    redraft_generic_items), never at bulk campaign-creation time, so a search
    call is only ever spent on an item a person is actively redrafting.
    Returns (text, backend_name), or None only once every configured backend
    has been tried and either produced nothing or errored -- that's what
    tells draft_message to fall through to the static template."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            text = _call_gemini_with_search(prompt)
            if text:
                return text, "gemini_search"
        except Exception:
            pass  # try the next backend rather than giving up here

    if os.environ.get("GROQ_API_KEY") and os.environ.get("TAVILY_API_KEY"):
        try:
            text = _call_groq_with_tavily(prompt, prospect)
            if text:
                return text, "groq_tavily"
        except Exception:
            pass

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            text = _call_anthropic_with_search(prompt)
            if text:
                return text, "anthropic_search"
        except Exception:
            pass

    return None


def draft_message(
    cfg: Config, prospect: ImportedProspect, *, use_web_search: bool = False,
    learning_context: Optional[str] = None,
) -> DraftedMessage:
    """Draft a subject+body for one prospect. Never raises on missing
    research data or a missing API key -- falls back to a generic template
    so a send pipeline is never blocked by personalization.

    Tiers, richest/cheapest data first:
    1. pain_summary/recommended_angle (Research Agent's `csg qualify` LLM
       output) -- the ideal case, drives _render_prompt.
    2. No qualify data yet, but other research is on file (industry,
       description, region, open reqs, score explanation) -- drives
       _render_research_prompt so the LLM still writes a genuinely
       personalized email instead of a static one. This is what keeps a
       well-researched A/B-tier company out of manual review just because
       the optional qualify step hasn't run or finished for it yet.
    3. `use_web_search=True` only, and neither of the above produced a draft
       (no qualify data, no other research fields, or every plain-text LLM
       call failed): _render_web_search_prompt plus _call_llm_with_search,
       which tries THREE independent search-capable backends in order --
       Gemini+google_search, then Groq+Tavily, then Anthropic+web_search --
       so it can actively look the company up instead of giving up, and
       survives any one (or two) of those providers being unconfigured,
       exhausted, or down. Only api.redraft_item/redraft_generic_items pass
       this; campaign creation never does, so a bulk import of N prospects
       never fires N live search calls -- search is spent only on an item a
       person is actively redrafting.
    4. Nothing usable even after all of the above -- every plain-text call
       AND all three search-capable backends either skipped (no key
       configured) or failed to produce a parseable, non-empty draft -- the
       static _fallback_template. This is the true last resort, and the only
       path that still earns 'needs_review'.

    Called once per item at campaign-creation time (see
    ingest.create_campaign_from_prospects, always with use_web_search=False)
    -- the drawer preview and the actual send both reuse that stored draft
    rather than calling this again, so what a user reviews is guaranteed to
    be what gets sent, until an explicit redraft regenerates it.

    `learning_context`: optional structured guidance blurb from the Feedback
    Learning Agent (see learning/context.py), appended to whichever prompt
    tier actually fires. None (the default -- and what every existing caller
    still passes) leaves every prompt byte-for-byte unchanged from before
    this parameter existed.
    """
    signature = _sender_signature(cfg)
    max_reqs = int(cfg.personalization.get("max_open_reqs_cited", 3))

    if prospect.pain_summary or prospect.recommended_angle:
        prompt = _append_learning_context(_render_prompt(prospect, max_reqs, signature), learning_context)
        text = _call_llm(prompt)
        if text:
            parsed = _parse_llm_output(text)
            if parsed:
                subject, body = parsed
                source = "pain_summary" if prospect.pain_summary else "recommended_angle"
                return DraftedMessage(subject=subject, body=body, personalized_from=source)
        # LLM unavailable/failed even though we had qualify data -- still try
        # the research-signal path below before giving up, since industry/
        # description/open_reqs may still let us produce a real draft.

    if _has_research_signal(prospect):
        prompt = _append_learning_context(_render_research_prompt(prospect, max_reqs, signature), learning_context)
        text = _call_llm(prompt)
        if text:
            parsed = _parse_llm_output(text)
            if parsed:
                subject, body = parsed
                return DraftedMessage(subject=subject, body=body, personalized_from="research_data")

    if use_web_search:
        prompt = _append_learning_context(_render_web_search_prompt(prospect, max_reqs, signature), learning_context)
        result = _call_llm_with_search(prompt, prospect)
        if result:
            text, _backend = result
            parsed = _parse_llm_output(text)
            if parsed:
                subject, body = parsed
                # personalized_from stays a single 'web_search' value regardless of
                # which of the three backends actually answered -- from a user's
                # or the dashboard's perspective it's the same thing: an LLM went
                # and found information rather than working from what was cached.
                return DraftedMessage(subject=subject, body=body, personalized_from="web_search")

    return _fallback_template(prospect, signature)


def revise_message(
    cfg: Config, prospect: ImportedProspect, *,
    current_subject: str, current_body: str, instruction: str,
    learning_context: Optional[str] = None,
) -> DraftedMessage:
    """Edit an already-drafted email per a user's free-text instruction,
    instead of generating a new one from scratch. Requires an LLM key --
    raises if none is configured, since there's no sensible template fallback
    for an arbitrary edit instruction.

    `learning_context`: see draft_message's docstring -- same optional,
    additive guidance blurb, same no-op behavior when None."""
    signature = _sender_signature(cfg)
    prompt = _append_learning_context(
        _render_revise_prompt(prospect, current_subject, current_body, instruction, signature),
        learning_context,
    )
    text = _call_llm(prompt)
    if not text:
        raise RuntimeError("no LLM available to revise this message (GEMINI_API_KEY / ANTHROPIC_API_KEY not set, or the call failed)")

    parsed = _parse_llm_output(text)
    if not parsed:
        raise RuntimeError("LLM response could not be parsed into SUBJECT/BODY")

    subject, body = parsed
    return DraftedMessage(subject=subject, body=body, personalized_from="revision")
