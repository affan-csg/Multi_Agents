"""Multi-key failover for the Gemini API.

The free tier's daily quota is small enough that a single key can run dry
mid-session -- exactly what happened while testing the Conversation Agent's
drafting (see personalize.py, responder.py). Rather than let every draft
fail for the rest of the day, this holds a small ordered pool of keys
(GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...) and advances to the
next one specifically when the current key's *daily* quota is exhausted -- a
per-minute rate limit (same 429 status, different cause) is NOT a reason to
burn through the pool, since that recovers in seconds and the caller should
just retry the same key.

This is a small, standalone counterpart to the Research Agent's
csg/discovery/gemini_keys.py (same quota-vs-rate-limit distinction, same
GEMINI_API_KEY_N convention), not a shared import -- Outreach never imports
Research's code directly, only its HTTP API (see research_client.py), and
this module is a fraction of the size since Outreach only ever calls Gemini
for short drafting prompts, not the Research Agent's higher-volume discovery
work.

Usage, inside _call_llm:

    pool = GeminiKeyPool.from_env()
    for attempt in pool.attempts():
        try:
            return _call_gemini_with_key(prompt, attempt.key)
        except Exception as exc:
            if not is_daily_quota_exhausted(exc):
                raise
            # loop continues with the next key, if any
    # every key exhausted (or none configured) -- caller falls through to
    # its own next tier (Anthropic, then the static template)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterator

_KEY_VAR_PATTERN = re.compile(r"^GEMINI_API_KEY(?:_(\d+))?$")


def _discover_keys() -> list[str]:
    """Collect GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3, ... from
    the environment, in that numeric order. Duplicates are dropped so
    retrying with the same key twice under two different var names is a
    no-op."""
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
    index: int  # 1-based position in the pool, for logging
    total: int


@dataclass
class GeminiKeyPool:
    keys: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "GeminiKeyPool":
        return cls(keys=_discover_keys())

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    def attempts(self) -> Iterator[KeyAttempt]:
        total = len(self.keys)
        for i, key in enumerate(self.keys, start=1):
            yield KeyAttempt(key=key, index=i, total=total)


def is_daily_quota_exhausted(exc: Exception) -> bool:
    """True for a 429 whose violation is specifically a per-day quota (as
    opposed to a per-minute rate limit, which is worth retrying on the same
    key instead of burning through the pool).

    Same lookup as the Research Agent's version: google-genai's APIError can
    carry its parsed body top-level or nested under "error" depending on the
    SDK path that raised it, and the details/violations block is sometimes
    just a generic rpc.Help link even for a genuine daily-quota 429 -- so the
    message text is checked as a fallback, confirmed against a live 429
    response during this project's own testing."""
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc)
    if status != 429 and "RESOURCE_EXHAUSTED" not in message and "429" not in message:
        return False

    details = getattr(exc, "details", None)
    body = details if isinstance(details, dict) else {}
    error_body = body.get("error", body) if isinstance(body.get("error"), dict) else body

    for d in error_body.get("details", []) or []:
        for violation in d.get("violations", []) or []:
            if "PerDay" in (violation.get("quotaId") or ""):
                return True
            if "PerMinute" in (violation.get("quotaId") or ""):
                return False

    lowered = message.lower()
    return "resource_exhausted" in lowered or "exceeded your current quota" in lowered
