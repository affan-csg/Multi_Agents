"""Multi-key failover for the Gemini API.

The free tier's daily quota is small enough that a single key can run dry
mid-session. Rather than fail the whole run, we hold a small ordered pool of
keys (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...) and rotate to
the next one whenever the current key reports its quota exhausted for the
day. A per-minute rate limit (the same 429 status, different cause) is NOT a
reason to burn through the pool -- that recovers in seconds, so the caller
should back off and retry the same key instead. Distinguishing the two is
what `is_daily_quota_exhausted` is for.

This module owns key *selection*; callers still create their own client per
call (via `make_gemini_client`, below) so existing retry/backoff logic (see
qualify.py) is unaffected. `make_gemini_client` also pins the connection to
IPv4 to route around networks where IPv6 routes to Google but the TLS
handshake silently fails; `is_connection_error` lets callers tell that kind
of transport failure apart from a real application-layer error when logging
a fallback, so a systemic connectivity problem is diagnosable from the logs
instead of looking identical to an ordinary quota/parse failure. Usage:

    from .gemini_keys import GeminiKeyPool, make_gemini_client

    pool = GeminiKeyPool.from_env()
    for attempt in pool.attempts():
        try:
            client = make_gemini_client(attempt.key)
            ... make the call ...
            break
        except Exception as exc:
            if not pool.should_advance(exc):
                raise
            # loop continues with the next key, if any
    else:
        raise RuntimeError(pool.exhausted_message())
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

_KEY_VAR_PATTERN = re.compile(r"^GEMINI_API_KEY(?:_(\d+))?$")

# Fail fast on a dead connect path instead of hanging on the default (much
# longer) httpx timeout -- a broken IPv6 route to Google can otherwise stall
# each key attempt for tens of seconds before the caller's fallback kicks in.
_CONNECT_TIMEOUT_S = 10.0


def make_ipv4_httpx_clients() -> tuple:
    """Build a sync+async httpx.Client pair pinned to IPv4.

    Some networks have a broken/blackholed IPv6 path to a given host that
    still passes ICMP (ping) but fails the TLS handshake for real HTTPS
    traffic -- httpx (via the stdlib resolver) prefers the AAAA record when
    both are present, so every call times out or drops mid-handshake on
    those networks even though the key/quota/target are all fine. Pinning
    the outbound connection to IPv4 sidesteps that broken route entirely.
    Shared by make_gemini_client and the Tavily+Groq fallback so neither
    path is a single point of failure for the other.
    """
    import httpx

    timeout = httpx.Timeout(30.0, connect=_CONNECT_TIMEOUT_S)
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    async_transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return (
        httpx.Client(transport=transport, timeout=timeout),
        httpx.AsyncClient(transport=async_transport, timeout=timeout),
    )


def make_gemini_client(api_key: str):
    """Build a genai.Client that connects to Google over IPv4.

    See make_ipv4_httpx_clients for why. Pinning the outbound connection to
    IPv4 sidesteps a broken/blackholed IPv6 route entirely.
    """
    from google import genai
    from google.genai import types

    sync_client, async_client = make_ipv4_httpx_clients()
    http_options = types.HttpOptions(
        httpx_client=sync_client,
        httpx_async_client=async_client,
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def is_connection_error(exc: Exception) -> bool:
    """True for a transport-layer failure (DNS, TCP connect, TLS handshake,
    connect/read timeout) as opposed to an application-layer error (bad
    request, quota 429, safety block, malformed response). Distinguishing
    the two matters for logging/diagnosis: a connection error means the
    network path to the API is broken (e.g. the IPv6/TLS issue
    make_gemini_client works around) and is worth flagging distinctly from
    "Gemini rejected this call", since the former points at the local
    network/runtime rather than at the request itself.
    """
    import httpx

    if isinstance(exc, httpx.TransportError):
        # Covers ConnectError, ConnectTimeout, ReadTimeout, PoolTimeout, etc.
        return True
    # The google-genai SDK can wrap the underlying httpx exception rather
    # than let it propagate directly -- check the cause chain too.
    cause = exc.__cause__
    if isinstance(cause, httpx.TransportError):
        return True
    message = str(exc)
    return "SSL" in message and ("EOF" in message or "handshake" in message.lower())


def _discover_keys() -> list[str]:
    """Collect GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ... from the
    environment, in that numeric order. Duplicates are dropped so retrying
    with the same key twice under two different var names is a no-op."""
    numbered: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        if not value:
            continue
        m = _KEY_VAR_PATTERN.match(name)
        if not m:
            continue
        index = int(m.group(1)) if m.group(1) else 1
        numbered.append((index, value))
    numbered.sort(key=lambda pair: pair[0])

    seen: set[str] = set()
    keys: list[str] = []
    for _, value in numbered:
        if value not in seen:
            seen.add(value)
            keys.append(value)
    return keys


@dataclass
class KeyAttempt:
    key: str
    index: int          # 1-based position in the pool, for logging
    total: int


@dataclass
class GeminiKeyPool:
    keys: list[str] = field(default_factory=list)
    _exhausted: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "GeminiKeyPool":
        return cls(keys=_discover_keys())

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    def attempts(self) -> Iterator[KeyAttempt]:
        """Yield each not-yet-exhausted key in order. A caller that breaks out
        of this loop on success simply stops iterating; a caller that
        exhausts every key falls through to the `else` clause of its `for`
        loop (see module docstring)."""
        total = len(self.keys)
        for i, key in enumerate(self.keys, start=1):
            if key in self._exhausted:
                continue
            yield KeyAttempt(key=key, index=i, total=total)

    def mark_exhausted(self, key: str) -> None:
        self._exhausted.add(key)

    def should_advance(self, exc: Exception) -> bool:
        """True if `exc` means "this key's daily quota is gone, try the next
        one"; False if the caller should re-raise (a real error) or retry the
        same key (a transient/per-minute limit -- caller's own backoff
        handles that, this pool is not involved)."""
        if not is_daily_quota_exhausted(exc):
            return False
        return True

    def exhausted_message(self) -> str:
        n = len(self.keys)
        if n == 0:
            return ("GEMINI_API_KEY is not set -- autonomous discovery cannot run. "
                    "Set it in .env (see .env.example).")
        return (f"all {n} configured Gemini key(s) have exhausted their daily quota. "
                f"Add another GEMINI_API_KEY_{n + 1} to .env, or wait for quota reset "
                f"(free-tier quotas typically reset ~24h after first use).")


def is_daily_quota_exhausted(exc: Exception) -> bool:
    """True for a 429 whose violation is specifically a per-day quota (as
    opposed to a per-minute rate limit, which is worth retrying instead of
    burning a key). Handles both the google-genai SDK's APIError shape and
    the raw httpx/ClientError shape web_search.py currently sees.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc)
    if status != 429 and "RESOURCE_EXHAUSTED" not in message and "429" not in message:
        return False

    # `exc.details` on google.genai's APIError is the raw parsed response
    # body, which the SDK itself accesses two different ways in different
    # places (see APIError._get_code/_get_status/_get_message): sometimes
    # top-level, sometimes nested under "error". Match that same lookup here
    # instead of assuming one shape.
    details = getattr(exc, "details", None)
    body = details if isinstance(details, dict) else {}
    error_body = body.get("error", body) if isinstance(body.get("error"), dict) else body

    for d in error_body.get("details", []) or []:
        for violation in d.get("violations", []) or []:
            if "PerDay" in (violation.get("quotaId") or ""):
                return True
            if "PerMinute" in (violation.get("quotaId") or ""):
                # An explicit per-minute violation is the one case worth
                # trusting over the message text: it recovers in seconds, so
                # retrying the same key (the caller's own backoff) is right.
                return False

    # No explicit per-minute violation ruled it out above, so fall back to
    # the message text. Google's 429 responses don't reliably include a
    # QuotaFailure/violations block even for genuine daily-quota errors --
    # the `details` list is sometimes just a generic rpc.Help link -- so
    # "resource_exhausted" / "exceeded your current quota" in the message is
    # the more reliable signal in practice (confirmed against a live 429).
    lowered = message.lower()
    if "resource_exhausted" in lowered or "exceeded your current quota" in lowered:
        return True
    return False
