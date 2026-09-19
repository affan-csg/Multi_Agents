"""Shared parsing helpers for ATS adapters."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw: Optional[str], limit: int = 6000) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(str(raw)))
    return _WS_RE.sub(" ", text).strip()[:limit]


def parse_dt(value: Any) -> Optional[datetime]:
    """Accept ISO strings and epoch millis/seconds; return naive UTC."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def first_of(d: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among keys."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None
