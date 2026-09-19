"""Tests for the Groq+Tavily search-augmented drafting backend
(personalize._call_groq_with_tavily / _tavily_search) -- the fallback used
between Gemini+google_search and Anthropic+web_search in the redraft "go
find more information" tier. See test_personalize.py for the tier-ordering
tests against _call_llm_with_search; this module tests the HTTP-level
mechanics of the Tavily search + Groq drafting calls themselves."""

from __future__ import annotations

import httpx
import pytest

from outreach import personalize as p
from outreach.models import ImportedProspect


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")


def _mock_response(json_body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=json_body, request=httpx.Request("POST", "https://example.com"))


def test_tavily_search_returns_results(monkeypatch):
    monkeypatch.setattr(
        httpx.Client, "post",
        lambda self, url, **kwargs: _mock_response({"results": [{"title": "Acme", "url": "https://acme.com", "content": "Acme makes widgets"}]}),
    )
    results = p._tavily_search("Acme acme.com company")
    assert len(results) == 1
    assert results[0]["title"] == "Acme"


def test_tavily_search_returns_empty_without_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert p._tavily_search("anything") == []


def test_tavily_search_returns_empty_on_http_error(monkeypatch):
    def raise_error(self, url, **kwargs):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("POST", url), response=_mock_response({}, 500))

    monkeypatch.setattr(httpx.Client, "post", raise_error)
    assert p._tavily_search("anything") == []


def test_groq_with_tavily_drafts_from_search_results(monkeypatch):
    monkeypatch.setattr(p, "_tavily_search", lambda query: [
        {"title": "Acme Robotics", "url": "https://acme.com", "content": "Acme Robotics builds warehouse automation."},
    ])
    monkeypatch.setattr(
        httpx.Client, "post",
        lambda self, url, **kwargs: _mock_response({
            "choices": [{"message": {"content": "SUBJECT: Automating your warehouse\n\nBODY: Hi there, ..."}}],
        }),
    )
    prospect = ImportedProspect(research_domain="acme.com", name="Acme Robotics")
    text = p._call_groq_with_tavily("base prompt", prospect)
    assert text is not None
    assert "Automating your warehouse" in text


def test_groq_with_tavily_returns_body_unavailable_when_no_search_results(monkeypatch):
    """No Tavily results at all -- Groq is never even called; the function
    reports BODY_UNAVAILABLE directly so the caller treats it as a failed
    attempt rather than drafting from nothing."""
    monkeypatch.setattr(p, "_tavily_search", lambda query: [])
    groq_calls = []
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: groq_calls.append(1))
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")
    text = p._call_groq_with_tavily("base prompt", prospect)
    assert text == p._BODY_UNAVAILABLE
    assert groq_calls == []  # Groq was never called -- nothing to draft from


def test_groq_with_tavily_returns_none_when_groq_call_fails(monkeypatch):
    monkeypatch.setattr(p, "_tavily_search", lambda query: [{"title": "X", "url": "https://x.com", "content": "..."}])

    def raise_error(self, url, **kwargs):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("POST", url), response=_mock_response({}, 500))

    monkeypatch.setattr(httpx.Client, "post", raise_error)
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")
    assert p._call_groq_with_tavily("base prompt", prospect) is None


def test_groq_with_tavily_returns_none_without_keys(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    prospect = ImportedProspect(research_domain="ghost.com", name="Ghost Co")
    assert p._call_groq_with_tavily("base prompt", prospect) is None
