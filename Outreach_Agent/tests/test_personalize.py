"""Personalization must never block a send on missing research data or a
missing LLM key -- falls back to a generic template deterministically, but
only as a true last resort. See personalize.py's module docstring for the
tiers this exercises: pain_summary/recommended_angle (qualify output) ->
other cached research data (industry/description/open_reqs/score
explanation) via a plain LLM call -> (redraft only) a live web-search-
augmented LLM call -> static template_only."""

from __future__ import annotations

import os

import pytest

from outreach.config import load_config
from outreach.models import ImportedProspect
from outreach.personalize import DraftedMessage, _render_open_reqs, draft_message


@pytest.fixture(autouse=True)
def _no_llm_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_falls_back_to_template_when_truly_no_research_data():
    """No pain_summary/recommended_angle AND no industry/description/open_reqs/
    score explanation -- nothing at all for an LLM to personalize from, and no
    LLM key configured anyway, so the static template is the honest result."""
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", contact_name="Jane",
        contact_email="jane@acme.com",
    )
    draft = draft_message(cfg, prospect)
    assert draft.personalized_from == "template_only"
    assert "Acme" in draft.body
    assert draft.subject


def test_falls_back_to_template_when_no_llm_key_even_with_pain_summary():
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", contact_name="Jane",
        pain_summary="hiring urgently for ML roles",
        recommended_angle="lead with nearshore delivery speed",
    )
    draft = draft_message(cfg, prospect)
    # No API key configured -> _call_gemini/_call_anthropic are never reached
    # (guarded by os.environ checks), so this must not raise and must still
    # produce a usable draft. With no LLM available, even the research-data
    # tier can't run, so this lands on the static template.
    assert draft.personalized_from == "template_only"
    assert draft.body


def test_uses_llm_on_research_data_when_qualification_missing(monkeypatch):
    """The regression this whole change fixes: an A/B-tier company with real
    research (industry, description, open reqs) but no pain_summary/
    recommended_angle yet (qualify hasn't run/finished) must still get a
    genuinely personalized, LLM-drafted email -- not fall straight to the
    generic template and land in needs_review."""
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="revivn.com", name="Revivn", tier="A", score=72,
        contact_name="Katie", contact_email="katie@revivn.com",
        industry="IT Asset Management",
        description="Revivn refurbishes and resells enterprise IT equipment.",
        open_reqs=[{"title": "Recruiter", "location": "Remote"}],
    )
    monkeypatch.setattr(
        "outreach.personalize._call_llm",
        lambda prompt: "SUBJECT: Scaling your recruiting\n\nBODY: Hi Katie, ... Best, Sig",
    )
    draft = draft_message(cfg, prospect)
    assert draft.personalized_from == "research_data"
    assert draft.subject == "Scaling your recruiting"


def test_falls_back_to_template_when_research_data_llm_call_fails(monkeypatch):
    """Research data is present, but every LLM call fails (network error,
    unparseable response, etc.) -- must still degrade to the static template
    rather than raising or leaving the item without a draft at all."""
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="revivn.com", name="Revivn", contact_name="Katie",
        industry="IT Asset Management",
    )
    monkeypatch.setattr("outreach.personalize._call_llm", lambda prompt: None)
    draft = draft_message(cfg, prospect)
    assert draft.personalized_from == "template_only"
    assert draft.body


def test_research_data_path_not_used_when_pain_summary_already_succeeds(monkeypatch):
    """When qualify data IS present and the LLM call succeeds, the research-
    data prompt must never even be tried -- pain_summary/recommended_angle is
    the richer, preferred source."""
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", contact_name="Jane",
        pain_summary="hiring urgently for ML roles",
        industry="Software",
    )
    calls = []

    def fake_call_llm(prompt):
        calls.append(prompt)
        return "SUBJECT: Hi\n\nBODY: body text"

    monkeypatch.setattr("outreach.personalize._call_llm", fake_call_llm)
    draft = draft_message(cfg, prospect)
    assert draft.personalized_from == "pain_summary"
    assert len(calls) == 1  # only the qualify-data prompt was ever rendered/called


def test_web_search_not_attempted_unless_requested(monkeypatch):
    """draft_message must default to use_web_search=False (campaign creation's
    behavior) -- a prospect with nothing cached must NOT trigger a live search
    call unless the caller explicitly opts in (redraft only)."""
    cfg = load_config()
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost", contact_name="Sam")
    search_calls = []
    monkeypatch.setattr(
        "outreach.personalize._call_llm_with_search",
        lambda prompt, p: search_calls.append(prompt) or ("SUBJECT: Hi\n\nBODY: body", "gemini_search"),
    )
    draft = draft_message(cfg, prospect)  # use_web_search defaults to False
    assert draft.personalized_from == "template_only"
    assert search_calls == []


def test_redraft_web_search_personalizes_when_nothing_cached(monkeypatch):
    """The fix this covers: a redraft (use_web_search=True) for a prospect
    with no qualify data and no other cached research field must try a live
    web search before giving up to the static template."""
    cfg = load_config()
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co", contact_name="Sam")
    monkeypatch.setattr(
        "outreach.personalize._call_llm_with_search",
        lambda prompt, p: ("SUBJECT: Found you\n\nBODY: Hi Sam, ... Best, Sig", "gemini_search"),
    )
    draft = draft_message(cfg, prospect, use_web_search=True)
    assert draft.personalized_from == "web_search"
    assert draft.subject == "Found you"


def test_redraft_falls_back_to_template_when_web_search_finds_nothing(monkeypatch):
    """The model's explicit 'nothing usable found' signal (BODY_UNAVAILABLE)
    must be treated as a failed attempt, degrading to the static template --
    not parsed as a real (empty/garbage) draft."""
    cfg = load_config()
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co", contact_name="Sam")
    monkeypatch.setattr(
        "outreach.personalize._call_llm_with_search", lambda prompt, p: ("BODY_UNAVAILABLE", "groq_tavily"),
    )
    draft = draft_message(cfg, prospect, use_web_search=True)
    assert draft.personalized_from == "template_only"
    assert draft.body


def test_redraft_falls_back_to_template_when_web_search_call_fails(monkeypatch):
    """Same as above, but every search-augmented backend fails/is unconfigured
    outright (network error, no keys at all, etc.) -- must still degrade
    cleanly."""
    cfg = load_config()
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co", contact_name="Sam")
    monkeypatch.setattr("outreach.personalize._call_llm_with_search", lambda prompt, p: None)
    draft = draft_message(cfg, prospect, use_web_search=True)
    assert draft.personalized_from == "template_only"
    assert draft.body


def test_call_llm_with_search_tries_groq_tavily_when_gemini_unconfigured(monkeypatch):
    """The specific fallback chain requested: no Gemini key -> Groq+Tavily is
    tried next (not skipped straight to Anthropic), and its result is used
    when it succeeds."""
    import outreach.personalize as p

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    monkeypatch.setattr(
        p, "_call_groq_with_tavily",
        lambda prompt, prospect: "SUBJECT: Via Groq\n\nBODY: drafted from Tavily snippets",
    )
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")

    result = p._call_llm_with_search("prompt text", prospect)

    assert result is not None
    text, backend = result
    assert backend == "groq_tavily"
    assert "Via Groq" in text


def test_call_llm_with_search_falls_through_groq_tavily_to_anthropic(monkeypatch):
    """If Gemini is unconfigured and Groq+Tavily fails (or is unconfigured),
    Anthropic+web_search must still be tried as the last search-capable
    backend before giving up."""
    import outreach.personalize as p

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # unconfigured -> skipped, not tried-and-failed
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")

    monkeypatch.setattr(p, "_call_anthropic_with_search", lambda prompt: "SUBJECT: Via Claude\n\nBODY: drafted")
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")

    result = p._call_llm_with_search("prompt text", prospect)

    assert result is not None
    text, backend = result
    assert backend == "anthropic_search"
    assert "Via Claude" in text


def test_call_llm_with_search_skips_groq_tavily_when_only_one_key_set(monkeypatch):
    """Groq+Tavily requires BOTH keys -- having only one configured must not
    attempt a half-configured call, and must fall through to the next
    backend cleanly."""
    import outreach.personalize as p

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)  # missing -- Groq+Tavily tier must be skipped
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")

    groq_calls = []
    monkeypatch.setattr(p, "_call_groq_with_tavily", lambda prompt, prospect: groq_calls.append(1))
    monkeypatch.setattr(p, "_call_anthropic_with_search", lambda prompt: "SUBJECT: Via Claude\n\nBODY: drafted")
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")

    result = p._call_llm_with_search("prompt text", prospect)

    assert groq_calls == []  # never attempted -- only one of the two required keys was set
    assert result is not None
    assert result[1] == "anthropic_search"


def test_redraft_prefers_cached_research_over_web_search(monkeypatch):
    """When cached research data (not just qualify output) is already enough
    to produce a draft, the web-search tier must never be tried, even with
    use_web_search=True -- search is a last resort, not a first choice."""
    cfg = load_config()
    prospect = ImportedProspect(
        research_domain="revivn.com", name="Revivn", contact_name="Katie",
        industry="IT Asset Management",
    )
    search_calls = []
    monkeypatch.setattr(
        "outreach.personalize._call_llm",
        lambda prompt: "SUBJECT: Hi\n\nBODY: body text",
    )
    monkeypatch.setattr(
        "outreach.personalize._call_llm_with_search",
        lambda prompt: search_calls.append(prompt) or "SUBJECT: Hi\n\nBODY: body",
    )
    draft = draft_message(cfg, prospect, use_web_search=True)
    assert draft.personalized_from == "research_data"
    assert search_calls == []


# --- hiring-signal wording (open_reqs placeholder) --------------------------
# Regression cover: an empty open_reqs list must never be rendered into a
# prompt as flatly "(no open roles listed)" when hiring_signal is "unknown"
# -- that phrasing reads as "confirmed not hiring" to an LLM, which is not
# what "we never found their careers site/ATS" means. See
# Research_Agent's models.HiringSignal / scoring.py's module docstring for
# the same distinction enforced upstream.

def test_render_open_reqs_confirmed_empty_says_no_roles_listed():
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", hiring_signal="confirmed", open_reqs=[],
    )
    assert _render_open_reqs(prospect, 3) == "- (no open roles listed)"


def test_render_open_reqs_unknown_signal_does_not_claim_no_roles():
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", hiring_signal="unknown", open_reqs=[],
    )
    rendered = _render_open_reqs(prospect, 3)
    assert "no open roles listed" not in rendered
    assert "not assume" in rendered or "unavailable" in rendered


def test_render_open_reqs_missing_signal_defaults_to_confirmed_wording():
    """No hiring_signal at all (e.g. the synthetic test-campaign fixtures, or
    a prospect imported before this field existed) must not be treated as
    'unknown' by default -- only an explicit 'unknown' changes the wording."""
    prospect = ImportedProspect(research_domain="acme.com", name="Acme", open_reqs=[])
    assert _render_open_reqs(prospect, 3) == "- (no open roles listed)"


def test_render_open_reqs_lists_real_reqs_regardless_of_signal():
    prospect = ImportedProspect(
        research_domain="acme.com", name="Acme", hiring_signal="unknown",
        open_reqs=[{"title": "Backend Engineer", "location": "Remote"}],
    )
    rendered = _render_open_reqs(prospect, 3)
    assert rendered == "- Backend Engineer (Remote)"
