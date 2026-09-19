"""Tests for api._delete_from_outreach -- the best-effort cascade that runs
when DELETE /runs/{run_id} deletes a run, so Outreach doesn't keep
campaigns/prospects imported from a run that no longer exists (see
outreach_client.OutreachClient.delete_research_run and
Outreach_Agent's DELETE /research/runs/{run_id}).

Same style as test_discover_handoff.py's _handoff_to_outreach tests: no real
network, OutreachClient itself is monkeypatched so these lock in the WIRING
(skip when unconfigured, call with the right run_id, swallow failures)
rather than any HTTP behavior.
"""

from __future__ import annotations

import pytest

from csg import api
from csg.outreach_client import OutreachClientError


class _FakeOutreachClient:
    instances: list["_FakeOutreachClient"] = []

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.closed = False
        self.delete_calls: list[str] = []
        self._result: dict = {"status": "ok", "campaigns_removed": 1, "items_removed": 2, "prospects_removed": 3}
        self._raise: Exception | None = None
        type(self).instances.append(self)

    def delete_research_run(self, run_id: str):
        self.delete_calls.append(run_id)
        if self._raise:
            raise self._raise
        return self._result

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_outreach_client():
    _FakeOutreachClient.instances.clear()
    yield
    _FakeOutreachClient.instances.clear()


def _patch_outreach_client(monkeypatch, client_cls):
    import csg.outreach_client as outreach_client_mod
    monkeypatch.setattr(outreach_client_mod, "OutreachClient", client_cls)


def test_skipped_when_outreach_api_base_unset(monkeypatch):
    monkeypatch.delenv("OUTREACH_API_BASE", raising=False)

    result = api._delete_from_outreach("run1")

    assert result is None
    assert _FakeOutreachClient.instances == []


def test_calls_delete_research_run_with_the_deleted_run_id(monkeypatch):
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")
    _patch_outreach_client(monkeypatch, _FakeOutreachClient)

    result = api._delete_from_outreach("run1")

    client = _FakeOutreachClient.instances[0]
    assert client.delete_calls == ["run1"]
    assert client.closed is True
    assert result == {"status": "ok", "campaigns_removed": 1, "items_removed": 2, "prospects_removed": 3}


def test_failure_is_returned_as_dict_not_raised(monkeypatch):
    """The run's own data is already gone by the time this runs -- an
    unreachable/erroring Outreach Agent must not raise, only be reported."""
    monkeypatch.setenv("OUTREACH_API_BASE", "http://fake-outreach:8001")

    class FailingClient(_FakeOutreachClient):
        def __init__(self, base_url):
            super().__init__(base_url)
            self._raise = OutreachClientError("Outreach Agent API unreachable")

    _patch_outreach_client(monkeypatch, FailingClient)

    result = api._delete_from_outreach("run1")  # must not raise

    assert result == {"error": "Outreach Agent API unreachable"}
    assert FailingClient.instances[0].closed is True
