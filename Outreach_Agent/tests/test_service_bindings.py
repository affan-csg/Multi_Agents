from outreach.config import Config


def test_research_service_binding_takes_precedence(monkeypatch):
    monkeypatch.setenv("RESEARCH_SERVICE_URL", "https://research.internal/")
    monkeypatch.setenv("RESEARCH_API_BASE", "http://localhost:8000/research")
    config = Config(raw={"research": {"api_base": "http://fallback/research"}}, path=None)

    assert config.research_api_base == "https://research.internal/research"


def test_research_api_base_remains_fallback_without_binding(monkeypatch):
    monkeypatch.delenv("RESEARCH_SERVICE_URL", raising=False)
    monkeypatch.setenv("RESEARCH_API_BASE", "http://localhost:8000/research")
    config = Config(raw={"research": {"api_base": "http://fallback/research"}}, path=None)

    assert config.research_api_base == "http://localhost:8000/research"