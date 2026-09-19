"""Tests for OutreachClient -- the HTTP client Research uses to hand off
Tier A/B prospects to the Outreach Agent (see discover.py's
_handoff_to_outreach). Uses httpx.MockTransport (part of httpx itself, no
network) so these exercise the real request/response/polling logic, not a
mocked-away version of it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from csg.outreach_client import OutreachClient, OutreachClientError


def _client(handler) -> OutreachClient:
    client = OutreachClient("http://fake-outreach:8001")
    client._client = httpx.Client(
        base_url="http://fake-outreach:8001", transport=httpx.MockTransport(handler),
    )
    return client


def test_import_prospects_polls_until_done():
    calls = {"job_status_hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/import":
            return httpx.Response(200, json={"job_id": "job-1"})
        if request.method == "GET" and request.url.path == "/jobs/job-1":
            calls["job_status_hits"] += 1
            if calls["job_status_hits"] < 3:
                return httpx.Response(200, json={"status": "running"})
            return httpx.Response(200, json={"status": "done", "result": {"imported": 4, "domains": ["a.com"]}})
        return httpx.Response(404)

    client = _client(handler)
    result = client.import_prospects("run1", tiers=["A", "B"], poll_interval=0)

    assert result == {"imported": 4, "domains": ["a.com"]}
    assert calls["job_status_hits"] == 3


def test_import_prospects_raises_on_job_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/import":
            return httpx.Response(200, json={"job_id": "job-1"})
        return httpx.Response(200, json={"status": "error", "error": "Research API unreachable"})

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="Research API unreachable"):
        client.import_prospects("run1", tiers=["A", "B"], poll_interval=0)


def test_import_prospects_raises_on_missing_job_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": "no job_id here"})

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="did not return a job_id"):
        client.import_prospects("run1", tiers=["A", "B"], poll_interval=0)


def test_import_prospects_times_out_if_job_never_finishes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/import":
            return httpx.Response(200, json={"job_id": "job-1"})
        return httpx.Response(200, json={"status": "running"})

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="did not finish within"):
        client.import_prospects("run1", tiers=["A", "B"], poll_interval=0, timeout=0)


def test_post_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="500"):
        client.create_campaign(research_run_id="run1", tiers=["A", "B"])


def test_post_raises_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="unreachable"):
        client.create_campaign(research_run_id="run1", tiers=["A", "B"])


def test_create_campaign_sends_expected_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"campaign": {"campaign_id": "camp-1"}, "item_count": 3})

    client = _client(handler)
    result = client.create_campaign(name="My Campaign", research_run_id="run1", tiers=["A", "B"], limit=10)

    assert captured["body"] == {
        "research_run_id": "run1", "tiers": ["A", "B"], "name": "My Campaign", "limit": 10,
    }
    assert result == {"campaign": {"campaign_id": "camp-1"}, "item_count": 3}


def test_create_campaign_omits_optional_fields_when_not_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"campaign": {}, "item_count": 0})

    client = _client(handler)
    client.create_campaign(research_run_id="run1", tiers=["A", "B"])

    assert captured["body"] == {"research_run_id": "run1", "tiers": ["A", "B"]}


def test_delete_research_run_calls_expected_path_and_returns_counts():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={
            "status": "ok", "campaigns_removed": 1, "items_removed": 2, "prospects_removed": 3,
        })

    client = _client(handler)
    result = client.delete_research_run("run1")

    assert captured == {"method": "DELETE", "path": "/research/runs/run1"}
    assert result == {"status": "ok", "campaigns_removed": 1, "items_removed": 2, "prospects_removed": 3}


def test_delete_research_run_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="500"):
        client.delete_research_run("run1")


def test_delete_research_run_raises_on_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(OutreachClientError, match="unreachable"):
        client.delete_research_run("run1")
