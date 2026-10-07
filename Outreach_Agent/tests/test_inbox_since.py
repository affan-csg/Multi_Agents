"""IMAP incremental fetch (`since`), timeout and connect retries -- with a
fake imaplib, so no mailbox is touched."""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta, timezone

import pytest

from outreach.conversation import inbox


class _FakeIMAP:
    searches: list = []

    def __init__(self, host, port, timeout=None):
        _FakeIMAP.timeout = timeout

    def login(self, u, p): return ("OK", [])
    def select(self, folder, readonly=True): return ("OK", [b"0"])

    def search(self, charset, *criteria):
        _FakeIMAP.searches.append(criteria)
        return ("OK", [b""])

    def logout(self): ...


@pytest.fixture(autouse=True)
def _imap_env(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    _FakeIMAP.searches = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    monkeypatch.setattr(inbox, "_sleep", lambda s: None)


def _today(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%d-%b-%Y")


def test_default_search_uses_the_lookback_window():
    inbox.fetch_replies(lookback_days=14)
    assert _FakeIMAP.searches[-1] == ("SINCE", _today(14))


def test_since_narrows_the_search():
    inbox.fetch_replies(lookback_days=14, since=datetime.now(timezone.utc) - timedelta(days=2))
    assert _FakeIMAP.searches[-1] == ("SINCE", _today(2))


def test_since_accepts_naive_utc_datetimes():
    inbox.fetch_replies(lookback_days=14, since=datetime.utcnow() - timedelta(days=3))
    assert _FakeIMAP.searches[-1] == ("SINCE", _today(3))


def test_since_never_reaches_past_the_lookback_window():
    inbox.fetch_replies(lookback_days=5, since=datetime.now(timezone.utc) - timedelta(days=40))
    assert _FakeIMAP.searches[-1] == ("SINCE", _today(5))


def test_socket_timeout_is_set():
    inbox.fetch_replies(lookback_days=1)
    assert _FakeIMAP.timeout == inbox._IMAP_TIMEOUT


def test_transient_connect_errors_are_retried(monkeypatch):
    attempts = []

    def flaky(host, port, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("timed out")
        return _FakeIMAP(host, port, timeout)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", flaky)
    inbox.fetch_replies(lookback_days=1)
    assert len(attempts) == 3


def test_persistent_connect_failure_raises_inbox_error(monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(inbox.InboxError, match="could not connect"):
        inbox.fetch_replies(lookback_days=1)
