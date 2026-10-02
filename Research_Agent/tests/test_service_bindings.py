from pathlib import Path

from csg.config import Config


def test_outreach_service_binding_takes_precedence(monkeypatch):
    monkeypatch.setenv("OUTREACH_SERVICE_URL", "https://outreach.internal/")
    monkeypatch.setenv("OUTREACH_API_BASE", "http://localhost:8000/outreach")
    config = Config(raw={"outreach": {"api_base": "http://fallback/outreach"}}, path=Path("."))

    assert config.outreach_api_base == "https://outreach.internal/outreach"


def test_outreach_api_base_remains_fallback_without_binding(monkeypatch):
    monkeypatch.delenv("OUTREACH_SERVICE_URL", raising=False)
    monkeypatch.setenv("OUTREACH_API_BASE", "http://localhost:8000/outreach")
    config = Config(raw={"outreach": {"api_base": "http://fallback/outreach"}}, path=Path("."))

    assert config.outreach_api_base == "http://localhost:8000/outreach"